"""Pure closed-gallery ranking and explicit operator decision services."""

import hashlib
import json
import math
import struct
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityCandidateDecision,
    IdentityObservation,
    SubjectReference,
)

MAX_RANKED_CANDIDATES = 5
FACE_MINIMUM_REFERENCES = 2
VOICE_MINIMUM_REFERENCES = 3
VOICE_MINIMUM_DURATION_MS = 30_000
WRITING_MINIMUM_MESSAGES = 3
WRITING_MINIMUM_WORDS = 500
VIDEO_FACE_AGGREGATE_ROLE = "aggregate_face_track"
VIDEO_FACE_FRAME_ROLE = "frame_face_observation"


def _finite_vector(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if not vector:
        raise ValueError("An embedding must contain at least one value.")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("Embedding values must be finite numbers.")
    return vector


def encode_embedding(values: Sequence[float]) -> bytes:
    """Encode a feature vector as deterministic big-endian float32 bytes."""
    vector = _finite_vector(values)
    return struct.pack(f"!{len(vector)}f", *vector)


def decode_embedding(value: bytes, dimension: int) -> tuple[float, ...]:
    """Decode and validate an opaque float32 feature vector."""
    if dimension <= 0:
        raise ValueError("Embedding dimension must be positive.")
    raw_value = bytes(value)
    if len(raw_value) != dimension * 4:
        raise ValueError(
            "Embedding must contain one 32-bit float per declared dimension."
        )
    return _finite_vector(struct.unpack(f"!{dimension}f", raw_value))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity without treating it as a probability."""
    left_vector = _finite_vector(left)
    right_vector = _finite_vector(right)
    if len(left_vector) != len(right_vector):
        raise ValueError("Embedding dimensions must match.")
    left_norm = math.sqrt(sum(value * value for value in left_vector))
    right_norm = math.sqrt(sum(value * value for value in right_vector))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Cosine similarity is undefined for a zero vector.")
    similarity = sum(
        left_value * right_value
        for left_value, right_value in zip(left_vector, right_vector, strict=True)
    ) / (left_norm * right_norm)
    return max(-1.0, min(1.0, similarity))


@dataclass(frozen=True, kw_only=True)
class ObservationVector:
    """The pure inputs needed to match one observed feature."""

    observation_id: uuid.UUID
    catalog_item_id: uuid.UUID
    modality: str
    feature_space: str
    values: tuple[float, ...]
    quality_status: str
    unit_key: str = ""
    source_location: Mapping[str, object] = field(default_factory=dict)
    sample_duration_ms: int | None = None
    sample_word_count: int | None = None
    gallery_eligible: bool = True


@dataclass(frozen=True, kw_only=True)
class ReferenceVector(ObservationVector):
    """A gallery observation assigned to a local subject."""

    subject_id: uuid.UUID


@dataclass(frozen=True)
class RankedSubject:
    subject_id: uuid.UUID
    rank: int
    similarity_score: float
    reference_count: int


@dataclass(frozen=True)
class UnknownCandidate:
    """The explicit possibility that the observation is outside the gallery."""

    label: str = "Unknown / not in gallery"


@dataclass(frozen=True)
class RankingResult:
    modality: str
    feature_space: str
    gallery_fingerprint: str
    candidates: tuple[RankedSubject, ...]
    unknown: UnknownCandidate
    reason: str = ""


class StaleIdentityCandidateError(ValueError):
    """The suggestion is no longer the current gallery/source snapshot."""


def observation_is_gallery_eligible(observation: IdentityObservation) -> bool:
    """Return whether an observation may participate in the closed gallery.

    Video frame workers retain one face observation per keyframe as internal
    evidence.  Only the later correlated track is a distinct gallery sample;
    otherwise a single face repeated across frames could manufacture reference
    eligibility and duplicate candidate sets.  Standalone image faces and the
    other modalities are already one observation per source sample.
    """
    from pandect.catalog.models import CatalogItem

    if (
        observation.modality != IdentityObservation.Modality.FACE
        or observation.catalog_item.item_type != CatalogItem.ItemType.VIDEO
    ):
        return True
    return (
        observation.provenance.get("identity_evidence_role")
        == VIDEO_FACE_AGGREGATE_ROLE
    )


def current_source_observations(
    observations: Iterable[IdentityObservation],
    *,
    canonical_sources: Mapping[uuid.UUID, object] | None = None,
) -> tuple[IdentityObservation, ...]:
    """Keep observations whose originating local run matches live source bytes.

    An observation can hash a derivative rather than the catalogue item's
    canonical input, so ``source_sha256`` alone cannot establish freshness.
    The immutable originating-run reference binds that derivative back to the
    exact canonical SHA-256 and size from which it was produced.
    """
    from pandect.processing.models import ProcessingRun
    from pandect.processing.services import ProcessingError, source_fingerprint

    values = list(observations)
    origin_ids: dict[uuid.UUID, uuid.UUID] = {}
    for observation in values:
        provenance = observation.provenance
        raw_origin_id = (
            provenance.get("processing_run_id")
            if isinstance(provenance, dict)
            else None
        )
        try:
            origin_ids[observation.pk] = uuid.UUID(str(raw_origin_id))
        except (AttributeError, TypeError, ValueError):
            continue

    runs = {
        run_id: (catalog_item_id, source_sha256, source_size)
        for run_id, catalog_item_id, source_sha256, source_size in (
            ProcessingRun.objects.filter(
                pk__in=origin_ids.values(),
                scope=ProcessingRun.Scope.LOCAL,
            ).values_list(
                "pk",
                "catalog_item_id",
                "source_fingerprint",
                "source_size",
            )
        )
    }
    source_cache = dict(canonical_sources or {})
    current = []
    for observation in values:
        origin = runs.get(origin_ids.get(observation.pk))
        if origin is None or origin[0] != observation.catalog_item_id:
            continue
        if observation.catalog_item_id not in source_cache:
            try:
                source_cache[observation.catalog_item_id] = source_fingerprint(
                    observation.catalog_item_id
                )
            except ProcessingError:
                source_cache[observation.catalog_item_id] = None
        canonical_source = source_cache[observation.catalog_item_id]
        if canonical_source is None:
            continue
        if (
            origin[1] == canonical_source.sha256
            and origin[2] == canonical_source.size_bytes
        ):
            current.append(observation)
    return tuple(current)


def observation_source_is_current(
    observation: IdentityObservation,
    *,
    canonical_source: object | None = None,
) -> bool:
    """Return whether one observation is bound to the live canonical source."""
    sources = (
        {observation.catalog_item_id: canonical_source}
        if canonical_source is not None
        else None
    )
    return bool(
        current_source_observations(
            (observation,),
            canonical_sources=sources,
        )
    )


def observation_vector(observation: IdentityObservation) -> ObservationVector:
    """Copy a stored observation into the pure ranking representation."""
    return ObservationVector(
        observation_id=observation.pk,
        catalog_item_id=observation.catalog_item_id,
        modality=observation.modality,
        feature_space=observation.feature_space,
        values=decode_embedding(
            observation.embedding,
            observation.embedding_dimension,
        ),
        quality_status=observation.quality_status,
        unit_key=observation.unit_key,
        source_location=dict(observation.source_location),
        sample_duration_ms=observation.sample_duration_ms,
        sample_word_count=observation.sample_word_count,
        gallery_eligible=observation_is_gallery_eligible(observation),
    )


def reference_vector(reference: SubjectReference) -> ReferenceVector:
    """Copy a selected reference into the pure ranking representation."""
    observation = observation_vector(reference.observation)
    return ReferenceVector(
        **observation.__dict__,
        subject_id=reference.subject_id,
    )


def current_reference_vectors(
    references: Iterable[SubjectReference],
    *,
    canonical_sources: Mapping[uuid.UUID, object] | None = None,
) -> tuple[ReferenceVector, ...]:
    """Vectorize only references still bound to each item's canonical source."""
    values = list(references)
    current_ids = {
        observation.pk
        for observation in current_source_observations(
            (reference.observation for reference in values),
            canonical_sources=canonical_sources,
        )
    }
    return tuple(
        reference_vector(reference)
        for reference in values
        if reference.observation_id in current_ids
    )


def _normalise(values: Sequence[float]) -> tuple[float, ...]:
    vector = _finite_vector(values)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("An eligible gallery reference cannot be a zero vector.")
    return tuple(value / norm for value in vector)


def _centroid(references: Sequence[ReferenceVector]) -> tuple[float, ...]:
    normalised = [_normalise(reference.values) for reference in references]
    dimension = len(normalised[0])
    mean = tuple(
        sum(vector[index] for vector in normalised) / len(normalised)
        for index in range(dimension)
    )
    return _normalise(mean)


def _eligible_references(
    query: ObservationVector,
    references: Iterable[ReferenceVector],
) -> dict[uuid.UUID, tuple[ReferenceVector, ...]]:
    grouped: dict[uuid.UUID, list[ReferenceVector]] = defaultdict(list)
    dimension = len(query.values)
    for reference in references:
        if (
            reference.modality != query.modality
            or reference.feature_space != query.feature_space
            or reference.quality_status != IdentityObservation.QualityStatus.APPROVED
            or not reference.gallery_eligible
            or len(reference.values) != dimension
        ):
            continue
        grouped[reference.subject_id].append(reference)

    eligible: dict[uuid.UUID, tuple[ReferenceVector, ...]] = {}
    for subject_id, observations in grouped.items():
        subject_references = _distinct_source_references(
            query.modality,
            observations,
        )
        if query.modality == IdentityObservation.Modality.FACE:
            is_eligible = len(subject_references) >= FACE_MINIMUM_REFERENCES
        elif query.modality == IdentityObservation.Modality.VOICE:
            duration = sum(
                reference.sample_duration_ms or 0 for reference in subject_references
            )
            is_eligible = (
                len(subject_references) >= VOICE_MINIMUM_REFERENCES
                and duration >= VOICE_MINIMUM_DURATION_MS
            )
        elif query.modality == IdentityObservation.Modality.WRITING:
            message_count = len(
                {reference.catalog_item_id for reference in subject_references}
            )
            word_count = sum(
                reference.sample_word_count or 0 for reference in subject_references
            )
            is_eligible = (
                message_count >= WRITING_MINIMUM_MESSAGES
                and word_count >= WRITING_MINIMUM_WORDS
            )
        else:
            raise ValueError("Unsupported identity modality.")
        if is_eligible:
            eligible[subject_id] = subject_references
    return eligible


def _reference_order(reference: ReferenceVector) -> tuple[str, str, str]:
    return (
        str(reference.catalog_item_id),
        reference.unit_key,
        str(reference.observation_id),
    )


def _distinct_source_references(
    modality: str,
    references: Iterable[ReferenceVector],
) -> tuple[ReferenceVector, ...]:
    """Collapse reruns and overlapping windows into distinct source evidence."""
    ordered = sorted(references, key=_reference_order)
    if modality == IdentityObservation.Modality.WRITING:
        # The gallery threshold is expressed in selected messages, not processor
        # executions. One canonical message contributes its words and vector once.
        by_message: dict[uuid.UUID, list[ReferenceVector]] = defaultdict(list)
        for reference in ordered:
            by_message[reference.catalog_item_id].append(reference)
        distinct = []
        for message_id in sorted(by_message, key=str):
            message_references = by_message[message_id]
            representative = message_references[0]
            distinct.append(
                replace(
                    representative,
                    sample_word_count=max(
                        value.sample_word_count or 0 for value in message_references
                    ),
                )
            )
        return tuple(distinct)

    # A stable unit key identifies the same face/voice observation on a rerun.
    by_unit: dict[tuple[uuid.UUID, str], ReferenceVector] = {}
    without_unit: list[ReferenceVector] = []
    for reference in ordered:
        if reference.unit_key:
            by_unit.setdefault(
                (reference.catalog_item_id, reference.unit_key),
                reference,
            )
        else:
            without_unit.append(reference)
    deduplicated = [*by_unit.values(), *without_unit]
    if modality == IdentityObservation.Modality.FACE:
        return tuple(sorted(deduplicated, key=_reference_order))
    if modality != IdentityObservation.Modality.VOICE:
        raise ValueError("Unsupported identity modality.")

    # Chunk overlap must not manufacture extra clips or duration. Intersecting
    # source intervals on the same item/stream form one evidence observation.
    located: dict[tuple[uuid.UUID, object], list[ReferenceVector]] = defaultdict(list)
    unlocated: list[ReferenceVector] = []
    for reference in deduplicated:
        start = reference.source_location.get("start_ms")
        end = reference.source_location.get("end_ms")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end
        ):
            located[
                (
                    reference.catalog_item_id,
                    reference.source_location.get("stream_index", 0),
                )
            ].append(reference)
        else:
            unlocated.append(reference)

    distinct_voice = list(unlocated)
    for source_key in sorted(located, key=lambda value: (str(value[0]), str(value[1]))):
        intervals = sorted(
            located[source_key],
            key=lambda value: (
                int(value.source_location["start_ms"]),
                int(value.source_location["end_ms"]),
                str(value.observation_id),
            ),
        )
        cluster = [intervals[0]]
        cluster_start = int(intervals[0].source_location["start_ms"])
        cluster_end = int(intervals[0].source_location["end_ms"])
        for reference in intervals[1:]:
            start = int(reference.source_location["start_ms"])
            end = int(reference.source_location["end_ms"])
            if start < cluster_end:
                cluster.append(reference)
                cluster_end = max(cluster_end, end)
                continue
            representative = min(cluster, key=_reference_order)
            distinct_voice.append(
                replace(
                    representative,
                    sample_duration_ms=cluster_end - cluster_start,
                    source_location={
                        **dict(representative.source_location),
                        "start_ms": cluster_start,
                        "end_ms": cluster_end,
                    },
                )
            )
            cluster = [reference]
            cluster_start = start
            cluster_end = end
        representative = min(cluster, key=_reference_order)
        distinct_voice.append(
            replace(
                representative,
                sample_duration_ms=cluster_end - cluster_start,
                source_location={
                    **dict(representative.source_location),
                    "start_ms": cluster_start,
                    "end_ms": cluster_end,
                },
            )
        )
    return tuple(sorted(distinct_voice, key=_reference_order))


def _gallery_fingerprint(
    query: ObservationVector,
    grouped_references: dict[uuid.UUID, tuple[ReferenceVector, ...]],
) -> str:
    digest = hashlib.sha256()
    digest.update(f"{query.modality}\0{query.feature_space}\0".encode())
    for subject_id in sorted(grouped_references, key=str):
        digest.update(subject_id.bytes)
        for reference in sorted(
            grouped_references[subject_id],
            key=_reference_order,
        ):
            digest.update(reference.catalog_item_id.bytes)
            if query.modality == IdentityObservation.Modality.WRITING:
                source_location = {"message": str(reference.catalog_item_id)}
            else:
                source_location = {
                    "unit_key": reference.unit_key,
                    "location": dict(reference.source_location),
                }
            digest.update(
                json.dumps(
                    source_location,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            )
            digest.update(encode_embedding(reference.values))
            digest.update(
                (
                    f"\0{reference.quality_status}"
                    f"\0{reference.sample_duration_ms or ''}"
                    f"\0{reference.sample_word_count or ''}\0"
                ).encode()
            )
    return digest.hexdigest()


def rank_closed_gallery(
    query: ObservationVector,
    references: Iterable[ReferenceVector],
) -> RankingResult:
    """Rank up to five eligible local subjects and always include unknown."""
    query_values = _finite_vector(query.values)
    if query.modality not in IdentityObservation.Modality.values:
        raise ValueError("Unsupported identity modality.")
    unknown = UnknownCandidate()
    if not query.gallery_eligible:
        return RankingResult(
            modality=query.modality,
            feature_space=query.feature_space,
            gallery_fingerprint=hashlib.sha256(
                f"{query.modality}\0{query.feature_space}\0ineligible\0".encode()
            ).hexdigest(),
            candidates=(),
            unknown=unknown,
            reason="query_not_gallery_eligible",
        )
    if query.quality_status != IdentityObservation.QualityStatus.APPROVED:
        return RankingResult(
            modality=query.modality,
            feature_space=query.feature_space,
            gallery_fingerprint=hashlib.sha256(
                f"{query.modality}\0{query.feature_space}\0".encode()
            ).hexdigest(),
            candidates=(),
            unknown=unknown,
            reason="query_quality_not_approved",
        )

    query = ObservationVector(
        observation_id=query.observation_id,
        catalog_item_id=query.catalog_item_id,
        modality=query.modality,
        feature_space=query.feature_space,
        values=query_values,
        quality_status=query.quality_status,
        unit_key=query.unit_key,
        source_location=dict(query.source_location),
        sample_duration_ms=query.sample_duration_ms,
        sample_word_count=query.sample_word_count,
        gallery_eligible=query.gallery_eligible,
    )
    grouped = _eligible_references(query, references)
    fingerprint = _gallery_fingerprint(query, grouped)
    ranked = sorted(
        (
            (
                subject_id,
                cosine_similarity(query.values, _centroid(subject_references)),
                len(subject_references),
            )
            for subject_id, subject_references in grouped.items()
        ),
        key=lambda item: (-item[1], str(item[0])),
    )[:MAX_RANKED_CANDIDATES]
    return RankingResult(
        modality=query.modality,
        feature_space=query.feature_space,
        gallery_fingerprint=fingerprint,
        candidates=tuple(
            RankedSubject(
                subject_id=subject_id,
                rank=rank,
                similarity_score=score,
                reference_count=count,
            )
            for rank, (subject_id, score, count) in enumerate(ranked, start=1)
        ),
        unknown=unknown,
        reason="" if ranked else "no_eligible_gallery_subjects",
    )


def current_identity_candidates(
    observation: IdentityObservation,
    *,
    candidates: Iterable[IdentityCandidate] | None = None,
    references: Iterable[SubjectReference] | None = None,
    reference_vectors: Iterable[ReferenceVector] | None = None,
    canonical_source=None,
) -> tuple[IdentityCandidate, ...]:
    """Return only the newest source- and gallery-compatible candidate set."""
    from pandect.processing.models import ProcessingRun, ProcessingStage
    from pandect.processing.services import ProcessingError, source_fingerprint

    if not observation_is_gallery_eligible(observation):
        return ()

    if candidates is None:
        candidates = IdentityCandidate.objects.filter(
            observation=observation,
        ).select_related("producing_run", "producing_stage", "subject")
    candidate_values = list(candidates)
    if canonical_source is None:
        try:
            canonical_source = source_fingerprint(observation.catalog_item_id)
        except ProcessingError:
            return ()
    if not observation_source_is_current(
        observation,
        canonical_source=canonical_source,
    ):
        return ()

    compatible = []
    for candidate in candidate_values:
        run = candidate.producing_run
        stage = candidate.producing_stage
        if run is None or stage is None:
            continue
        if (
            stage.run_id != run.pk
            or run.catalog_item_id != observation.catalog_item_id
            or stage.status != ProcessingStage.Status.SUCCEEDED
            or run.status
            in (ProcessingRun.Status.FAILED, ProcessingRun.Status.SUPERSEDED)
        ):
            continue
        if (
            run.source_fingerprint != canonical_source.sha256
            or run.source_size != canonical_source.size_bytes
        ):
            continue
        compatible.append(candidate)
    if not compatible:
        return ()

    by_set: dict[
        tuple[uuid.UUID, uuid.UUID, uuid.UUID],
        list[IdentityCandidate],
    ] = defaultdict(list)
    for candidate in compatible:
        by_set[
            (
                candidate.producing_run_id,
                candidate.producing_stage_id,
                candidate.candidate_set_id,
            )
        ].append(candidate)

    def candidate_set_order(
        item: tuple[
            tuple[uuid.UUID, uuid.UUID, uuid.UUID],
            list[IdentityCandidate],
        ],
    ) -> tuple[datetime, datetime, datetime, str]:
        key, values = item
        exemplar = values[0]
        return (
            exemplar.producing_run.created_at,
            exemplar.producing_stage.created_at,
            max(value.created_at for value in values),
            str(key[2]),
        )

    _latest_key, latest = max(by_set.items(), key=candidate_set_order)
    latest = sorted(latest, key=lambda value: (value.rank, str(value.pk)))
    if len(latest) > MAX_RANKED_CANDIDATES:
        return ()

    if reference_vectors is None:
        if references is None:
            references = SubjectReference.objects.select_related(
                "observation",
                "observation__catalog_item",
            ).all()
        reference_vectors = current_reference_vectors(
            references,
            canonical_sources={observation.catalog_item_id: canonical_source},
        )
    expected = rank_closed_gallery(
        observation_vector(observation),
        reference_vectors,
    ).gallery_fingerprint
    if any(candidate.gallery_fingerprint != expected for candidate in latest):
        return ()
    return tuple(latest)


def identity_candidate_is_current(candidate: IdentityCandidate) -> bool:
    """Check whether a candidate remains actionable against the live gallery."""
    current = current_identity_candidates(candidate.observation)
    return any(value.pk == candidate.pk for value in current)


@transaction.atomic
def decide_identity_candidate(
    candidate_id: uuid.UUID,
    decision: str,
    *,
    note: str = "",
    decided_at: datetime | None = None,
) -> IdentityAssertion | None:
    """Confirm or reject a candidate while retaining assertion history."""
    if decision not in {
        IdentityCandidate.Decision.CONFIRMED,
        IdentityCandidate.Decision.REJECTED,
    }:
        raise ValueError("A candidate decision must be confirmed or rejected.")
    decision_time = decided_at or timezone.now()
    candidate = (
        IdentityCandidate.objects.select_for_update()
        .select_related(
            "observation",
            "subject",
            "producing_run",
            "producing_stage",
        )
        .get(pk=candidate_id)
    )
    requires_current_set = (
        candidate.decision == IdentityCandidate.Decision.SUGGESTED
        or decision == IdentityCandidate.Decision.CONFIRMED
    )
    if requires_current_set and not identity_candidate_is_current(candidate):
        raise StaleIdentityCandidateError(
            "This candidate set is stale; review the newest compatible results."
        )
    active_assertion = (
        IdentityAssertion.objects.select_for_update()
        .filter(
            observation=candidate.observation,
            status=IdentityAssertion.Status.ACTIVE,
        )
        .select_related("candidate")
        .first()
    )

    if decision == IdentityCandidate.Decision.REJECTED:
        rejection = IdentityCandidateDecision.objects.create(
            candidate=candidate,
            decision=decision,
            note=note,
            decided_at=decision_time,
        )
        if active_assertion and active_assertion.candidate_id == candidate.pk:
            active_assertion.status = IdentityAssertion.Status.RETRACTED
            active_assertion.retracted_at = decision_time
            active_assertion.retraction_note = rejection.note
            active_assertion.save()
        candidate.decision = IdentityCandidate.Decision.REJECTED
        candidate.decided_at = decision_time
        candidate.decision_note = note
        candidate.save()
        return None

    IdentityCandidateDecision.objects.create(
        candidate=candidate,
        decision=decision,
        note=note,
        decided_at=decision_time,
    )
    if active_assertion and active_assertion.candidate_id == candidate.pk:
        if candidate.decision != IdentityCandidate.Decision.CONFIRMED:
            candidate.decision = IdentityCandidate.Decision.CONFIRMED
            candidate.decided_at = decision_time
            candidate.decision_note = note
            candidate.save()
        return active_assertion

    if active_assertion:
        supersession_note = "Superseded by another confirmed candidate."
        IdentityCandidateDecision.objects.create(
            candidate=active_assertion.candidate,
            decision=IdentityCandidate.Decision.REJECTED,
            origin=IdentityCandidateDecision.Origin.SUPERSESSION,
            note=supersession_note,
            decided_at=decision_time,
        )
        active_assertion.status = IdentityAssertion.Status.RETRACTED
        active_assertion.retracted_at = decision_time
        active_assertion.retraction_note = supersession_note
        active_assertion.save()

        previous_candidate = IdentityCandidate.objects.select_for_update().get(
            pk=active_assertion.candidate_id
        )
        previous_candidate.decision = IdentityCandidate.Decision.REJECTED
        previous_candidate.decided_at = decision_time
        previous_candidate.decision_note = active_assertion.retraction_note
        previous_candidate.save()

    candidate.decision = IdentityCandidate.Decision.CONFIRMED
    candidate.decided_at = decision_time
    candidate.decision_note = note
    candidate.save()
    return IdentityAssertion.objects.create(
        candidate=candidate,
        observation=candidate.observation,
        subject=candidate.subject,
        note=note,
        asserted_at=decision_time,
    )
