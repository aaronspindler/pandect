"""Retain and reopen the exact originals of a person's writing analysis."""

from itertools import batched
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models.functions import Length

from core.analysis.errors import AnalysisError
from core.analysis.source_limits import payload_bytes
from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence

MAX_WRITING_MESSAGES = 10_000


def check_message_count(count):
    if count > MAX_WRITING_MESSAGES:
        raise ValidationError(
            f"Choose up to {MAX_WRITING_MESSAGES:,} messages per analysis. "
            "Narrow the date range."
        )


def writing_scope(operation, parameters):
    scope = parameters.get("writing_scope", {})
    if (
        operation in {"style_profiles", "style_change"}
        and isinstance(scope, dict)
        and scope.get("version") == 1
    ):
        return scope
    return None


def snapshot_messages(case, scope, selections):
    """Keep source order, original IDs and checksums in the existing run format."""
    snapshots = []
    seen = set()
    total_bytes = 0
    person_id = UUID(scope["person_id"])
    messages = scoped_evidence(case, "text-message").filter(
        sender_id=person_id, is_archived=False
    )
    for batch in batched(selections, 500, strict=False):
        check_message_count(len(snapshots) + len(batch))
        if any(
            set(choice) != {"kind", "id"} or choice["kind"] != "text-message"
            for choice in batch
        ):
            raise ValidationError("Choose active messages attributed to this person.")
        identifiers = [UUID(choice["id"]) for choice in batch]
        selected = messages.filter(pk__in=identifiers)
        checksums = dict(selected.values_list("pk", "sha256"))
        total_bytes += payload_bytes(selected, "body")
        if total_bytes > settings.PANDECT_ANALYSIS_MAX_BYTES:
            raise ValidationError(
                "The selected messages exceed the writing analysis size limit. "
                "Narrow the date range.",
                code="source_material_limit",
            )
        for choice, identifier in zip(batch, identifiers, strict=True):
            if identifier in seen or identifier not in checksums:
                raise AnalysisError("source_changed")
            seen.add(identifier)
            snapshots.append(
                {
                    "id": source_id(choice),
                    "kind": "text-message",
                    "evidence_id": str(identifier),
                    "attachment_id": None,
                    "result_id": None,
                    "review_id": None,
                    "geocode_candidate": None,
                    "sha256": checksums[identifier],
                }
            )
    period_split = scope.get("period_split")
    if not snapshots or (
        "period_split" in scope
        and (type(period_split) is not int or not 0 < period_split < len(snapshots))
    ):
        raise ValidationError("Choose a nonempty message selection for each period.")
    return snapshots


def source_batches(case, scope, inputs, *, with_content=True):
    """Resolve ordered pages, reading large messages individually."""
    from core.analysis.source_resolution import SourceResolutionCache, resolve_snapshots

    person_id = UUID(scope["person_id"])
    for page in batched(inputs, 128, strict=False):
        if with_content:
            originals = scoped_evidence(case, "text-message").filter(
                pk__in=[row["evidence_id"] for row in page]
            )
            lengths = dict(
                originals.annotate(characters=Length("body")).values_list(
                    "pk", "characters"
                )
            )
            groups = []
            group = []
            characters = 0
            for row in page:
                length = lengths.get(UUID(row["evidence_id"]), 0)
                if group and characters + length > 256 * 1024:
                    groups.append(group)
                    group = []
                    characters = 0
                group.append(row)
                characters += length
            groups.append(group)
        else:
            groups = (page,)
        for group in groups:
            cache = SourceResolutionCache(
                case, active_only=True, with_content=with_content
            )
            resolved = resolve_snapshots(group, case, cache=cache)
            if any(
                source.kind != "text-message"
                or source.item.sender_id != person_id
                or source.derived_result_id
                for source in resolved
            ):
                raise AnalysisError("source_changed")
            yield resolved


def verify_membership(case, scope, inputs):
    """Recheck attributed membership and the exact retained bytes."""
    for _ in source_batches(case, scope, inputs):
        pass


def result_batches(case, scope, inputs, data, *, with_content):
    references = {
        reference for item in data.get("items", []) for reference in item["source_ids"]
    }
    return source_batches(
        case,
        scope,
        (snapshot for snapshot in inputs if snapshot["id"] in references),
        with_content=with_content,
    )
