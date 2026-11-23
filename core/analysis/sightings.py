"""Reviewable pairs from selected originals and exact retained comparisons."""

import json
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404
from django.utils.dateparse import parse_datetime

from core.analysis.errors import AnalysisError
from core.analysis.projection import identifier_key
from core.analysis.sources import Source, output, source_id
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import KINDS, LOCATION_KINDS
from core.models import AnalysisFinding, AnalysisReview, AnalysisRun
from geo.analysis.deterministic.locations import distance_m, number

MAX_CANDIDATES = 25
MAX_SELECTION = 12
ASSOCIATIONS = {"person-location": "person_id", "vehicle-location": "vehicle_id"}
SPATIAL_METHODS = {"spatial_groups", "colocation"}
REVIEW_DECISIONS = {
    AnalysisReview.Decision.ACCEPTED: "Accepted for review",
    AnalysisReview.Decision.REJECTED: "Rejected",
    AnalysisReview.Decision.UNRESOLVED: "Unresolved",
}


def sighting_reviews(case, result):
    return result.reviews.filter(case=case, item_index__isnull=True)


def review_outcome(review):
    return REVIEW_DECISIONS[
        review.decision if review else AnalysisReview.Decision.UNRESOLVED
    ]


def saved_group(case, run, *, cache=None):
    """Read a retained proposal only when its exact sources still resolve."""
    from core.analysis.services import SourceResolutionCache, resolve_inputs

    result = getattr(run, "result", None)
    if (
        not result
        or result.case_id != case.pk
        or run.status != AnalysisRun.Status.COMPLETE
    ):
        raise AnalysisError("source_unavailable")
    cache = cache or SourceResolutionCache(case, with_content=False)
    sources = resolve_inputs(run, case, require_current_review=False, cache=cache)
    originals = {
        str(source.id): (source.kind, str(source.item.pk)) for source in sources
    }
    try:
        references = {
            (ref["result_id"], ref["item_index"])
            for ref in run.parameters.get("finding_refs", ())
        }
        values = result.data["items"][0]["values"]
        links = values["links"]
        if not 1 <= len(links) <= MAX_SELECTION:
            raise ValueError
        linked = []
        for link in links:
            first, second = link["endpoints"]
            linked.extend((first, second))
        if set(values["member_identities"]) != {
            source["identity"] for source in linked
        }:
            raise ValueError
        for endpoint in [
            values["anchor"],
            *linked,
            *values["supporting"],
            *values["contrary"],
        ]:
            if not isinstance(endpoint["title"], str) or originals[
                endpoint["source_id"]
            ] != (endpoint["kind"], endpoint["evidence_id"]):
                raise ValueError
            if finding := endpoint.get("finding"):
                result_id, item_index = finding["result_id"], finding["item_index"]
                parent, _ = cache.finding_results[result_id]
                if (result_id, item_index) not in references or str(
                    parent.run_id
                ) != finding["run_id"]:
                    raise ValueError
            if endpoint.get("observed_at"):
                parse_datetime(endpoint["observed_at"])
    except IndexError, KeyError, TypeError, ValueError:
        raise AnalysisError("source_unavailable") from None
    return result, values, sources


def _choice(token):
    try:
        kind, identifier = token.split(":")
        identifier = str(UUID(identifier))
    except AttributeError, ValueError:
        raise ValidationError("Choose an available observation.") from None
    if kind not in KINDS:
        raise ValidationError("Choose an available observation.")
    return {"kind": kind, "id": identifier}


def _selection(source):
    return f"{source.kind}:{source.item.pk}"


def _endpoint(source, anchor=None, **details):
    anchor = anchor or source.anchor()
    if row := getattr(source.item, "source_row", None):
        anchor = anchor | {"source_row": row}
    identity = _selection(source)
    if region := anchor.get("region"):
        identity += ":" + json.dumps(region, separators=(",", ":"))
    return {
        "source_id": str(source.id),
        "kind": source.kind,
        "evidence_id": str(source.item.pk),
        "title": source.item.title,
        "anchor": anchor,
        "identity": identity,
        "selection": _selection(source),
        **details,
    }


def _pair(token, basis, first, second, facts):
    return {
        "token": token,
        "basis": basis,
        "endpoints": [first, second],
        "facts": facts,
    }


def _reference(finding):
    run = finding.result.run
    return {
        "result_id": str(finding.result_id),
        "item_index": finding.item_index,
        "run_id": str(run.pk),
        "method": run.operation,
        "method_version": run.method_version,
    }


class Selection:
    """Load exact findings once and track the inputs needed to reproduce them."""

    def __init__(self, case, sources=None):
        from core.analysis.services import SourceResolutionCache

        self.case = case
        self.originals = {}
        self.findings = {}
        self.inputs = {}
        self.refs = {}
        self.resolved_runs = {}
        self.plate_items = {}
        self.source_cache = SourceResolutionCache(case, with_content=False)
        self.resolved = (
            {str(source.id): source for source in sources} if sources else None
        )

    def original(self, token):
        choice = _choice(token)
        key = source_id(choice)
        if key not in self.originals:
            if self.resolved is None:
                item = get_evidence(self.case, choice["kind"], choice["id"])
                source = Source(key, choice["kind"], item)
            else:
                source = self.resolved.get(key)
            if (
                source is None
                or source.item.case_id != self.case.pk
                or source.item.is_archived
            ):
                raise AnalysisError("source_unavailable")
            self.originals[key] = source
        return self.originals[key]

    def finding(self, result_id, item_index, methods):
        from core.analysis.catalogue import SUCCESSFUL, raw_finding_item
        from core.analysis.services import (
            require_current_geocode_reviews,
            resolve_inputs,
        )

        try:
            key = (str(UUID(str(result_id))), int(item_index))
        except ValueError, TypeError:
            raise AnalysisError("source_unavailable") from None
        if key not in self.findings:
            finding = (
                AnalysisFinding.objects.filter(
                    case=self.case,
                    result__case=self.case,
                    result__run__case=self.case,
                    result__run__status__in=SUCCESSFUL,
                    result__run__operation__in=methods,
                    result_id=key[0],
                    item_index=key[1],
                )
                .select_related("result__run")
                .defer("result__data", "result__provenance")
                .annotate(item=raw_finding_item())
                .first()
            )
            if finding is None or not isinstance(finding.item, dict):
                raise AnalysisError("source_unavailable")
            self.findings[key] = finding
        finding = self.findings[key]
        run = finding.result.run
        if run.operation not in methods:
            raise AnalysisError("source_unavailable")
        if run.pk not in self.resolved_runs:
            sources = self.resolved
            if sources is None:
                resolved = resolve_inputs(
                    run,
                    self.case,
                    cache=self.source_cache,
                    require_current_review=False,
                )
                require_current_geocode_reviews(run, resolved, exact_revision=True)
                sources = {str(source.id): source for source in resolved}
            if any(
                entry["id"] not in sources or sources[entry["id"]].item.is_archived
                for entry in run.inputs
            ):
                raise AnalysisError("source_unavailable")
            self.resolved_runs[run.pk] = sources
            self.inputs.update((entry["id"], entry) for entry in run.inputs)
        self.refs[key] = {"result_id": key[0], "item_index": key[1]}
        return finding, self.resolved_runs[run.pk]

    def association(self, anchor, other):
        field = ASSOCIATIONS.get(anchor.kind)
        if (
            not field
            or anchor.kind != other.kind
            or anchor.item.pk == other.item.pk
            or getattr(anchor.item, field) != getattr(other.item, field)
        ):
            raise AnalysisError("source_unavailable")
        subject = getattr(anchor.item, field.removesuffix("_id"))
        if subject.case_id != self.case.pk:
            raise AnalysisError("source_unavailable")
        return _pair(
            f"association:{_selection(other)}",
            "Explicit source association",
            _endpoint(anchor),
            _endpoint(other),
            {"Reported subject": str(subject)},
        )

    def plate(self, anchor, token):
        _, first_result, first_index, second_result, second_index = token.split(":")
        first = self._plate_endpoint(first_result, first_index)
        second = self._plate_endpoint(second_result, second_index)
        if (
            first[0]["selection"] != _selection(anchor)
            or first[0]["selection"] == second[0]["selection"]
            or first[1] != second[1]
        ):
            raise AnalysisError("source_unavailable")
        facts = {"Shared plate text": first[1]}
        if first[2] == second[2]:
            facts["Originals"] = (
                "Matching checksums indicate copies. Treat them as one source."
            )
        return _pair(token, "Shared identifier", first[0], second[0], facts)

    def _plate_endpoint(self, result_id, index):
        finding, sources = self.finding(result_id, index, ("plate_ocr",))
        item = finding.item
        match_kind, match_key = identifier_key("plate_ocr", item)
        anchors = item.get("anchors", [])
        if match_kind != "plate" or len(anchors) != 1 or not anchors[0].get("region"):
            raise AnalysisError("source_unavailable")
        anchor = anchors[0]
        source = sources.get(anchor.get("source_id"))
        if source is None or source.kind != "image":
            raise AnalysisError("source_unavailable")
        region_number = item["values"].get("Plate region number")
        alternatives = []
        if finding.result_id not in self.plate_items:
            self.plate_items[finding.result_id] = finding.result.data.get("items", [])
        for variant in self.plate_items[finding.result_id]:
            if variant.get("anchors") != anchors or (
                variant.get("values", {}).get("Plate region number") != region_number
            ):
                continue
            values = variant["values"]
            reading = (
                variant.get("text")
                or values.get("Short OCR output")
                or values.get("Weak region OCR output")
            )
            if reading and reading != "None" and reading != item["text"]:
                alternatives.append(reading)
        endpoint = _endpoint(
            source,
            anchor,
            finding=_reference(finding),
            reading=item["text"],
            alternatives=list(dict.fromkeys(alternatives)),
        )
        return endpoint, match_key, source.item.sha256

    def spatial_pairs(self, anchor, result_id, index):
        finding, sources = self.finding(result_id, index, SPATIAL_METHODS)
        item = finding.item
        run = finding.result.run
        keys = item.get("source_ids", [])
        anchors = {entry["source_id"]: entry for entry in item.get("anchors", [])}
        values = item.get("values", {})
        first_key = next(
            (
                key
                for key in keys
                if key in sources and _selection(sources[key]) == _selection(anchor)
            ),
            None,
        )
        if len(keys) < 2 or first_key is None:
            return
        if run.operation == "colocation":
            if len(keys) != 2 or item.get("label") != "Candidate nearby observations":
                return
            other_keys = [key for key in keys if key != first_key]
            basis = "Time and place proximity"
        else:
            if not item.get("label", "").startswith("Spatial group "):
                return
            other_keys = keys[1:] if first_key == keys[0] else [keys[0]]
            basis = "Place proximity"
        first = sources[first_key]
        if first.kind not in LOCATION_KINDS or first.coordinates is None:
            return
        for key in other_keys:
            other = sources.get(key)
            if (
                other is None
                or other.kind not in LOCATION_KINDS
                or other.coordinates is None
                or _selection(first) == _selection(other)
                or first_key not in anchors
                or key not in anchors
            ):
                continue
            if run.operation == "spatial_groups":
                facts = {
                    "Distance estimate (m)": round(
                        distance_m(first.coordinates, other.coordinates), 2
                    ),
                    "Anchor radius (m)": values.get("Anchor radius (m)"),
                }
            else:
                facts = {
                    "Distance estimate (m)": values.get("Distance estimate (m)"),
                    "Distance tolerance (m)": number(run.parameters, "distance_m"),
                    "Time difference (seconds)": values.get(
                        "Time difference (seconds)"
                    ),
                    "Time tolerance (minutes)": number(
                        run.parameters, "time_window_minutes"
                    ),
                }
            endpoints = [
                _endpoint(
                    source,
                    anchors[str(source.id)],
                    finding=_reference(finding),
                    position_origin=(
                        "Reviewed address estimate"
                        if source.item.latitude is None
                        else "Reported position"
                    ),
                    observed_at=source.item.observed_at.isoformat()
                    if source.item.observed_at
                    else "",
                )
                for source in (first, other)
            ]
            yield _pair(f"spatial:{result_id}:{index}:{key}", basis, *endpoints, facts)

    def link(self, anchor, token):
        try:
            if token.startswith("association:"):
                other = self.original(token.removeprefix("association:"))
                return self.association(anchor, other)
            if token.startswith("plate:"):
                return self.plate(anchor, token)
            if token.startswith("spatial:"):
                _, result_id, index, _ = token.split(":")
                return next(
                    pair
                    for pair in self.spatial_pairs(anchor, result_id, int(index))
                    if pair["token"] == token
                )
        except ValueError, StopIteration:
            pass
        raise AnalysisError("source_unavailable")

    def choices(self):
        choices = [
            {
                "kind": entry["kind"],
                "id": entry["evidence_id"],
                **{
                    key: entry.get(key)
                    for key in ("attachment_id", "result_id", "geocode_candidate")
                    if entry.get(key) is not None
                },
            }
            for entry in self.inputs.values()
        ]
        choices.extend(
            {"kind": source.kind, "id": str(source.item.pk)}
            for key, source in self.originals.items()
            if key not in self.inputs
        )
        if len(choices) > settings.PANDECT_ANALYSIS_MAX_SOURCES:
            raise ValidationError(
                "Choose fewer connections. Their retained sources exceed the "
                "analysis limit."
            )
        return choices


def candidate_links(case, anchor):
    """Discover a bounded list of available direct links for one selected original."""
    from core.analysis.catalogue import finding_queryset

    selection = Selection(case)
    anchor = selection.original(anchor)
    candidates = []
    if field := ASSOCIATIONS.get(anchor.kind):
        related = (
            scoped_evidence(case, anchor.kind)
            .filter(is_archived=False, **{field: getattr(anchor.item, field)})
            .exclude(pk=anchor.item.pk)
            .order_by("-observed_at", "pk")[:MAX_CANDIDATES]
        )
        for other in related:
            key = source_id({"kind": anchor.kind, "id": other.pk})
            candidates.append(
                selection.association(anchor, Source(key, anchor.kind, other))
            )
    if anchor.kind == "image":
        plates = finding_queryset(case, {"method": "plate_ocr"}).filter(
            match_kind="plate", match_version="identifier-strings.v1", weak=False
        )
        seen = set()
        for result_id, index, match_key, digest in plates.filter(
            sources__case=case,
            sources__evidence_id=anchor.item.pk,
            sources__kind="image",
        ).values_list("result_id", "item_index", "match_key", "match_digest")[
            :MAX_CANDIDATES
        ]:
            peers = (
                plates.exclude(sources__evidence_id=anchor.item.pk)
                .filter(match_digest=digest, match_key=match_key)
                .values_list("result_id", "item_index")[:MAX_CANDIDATES]
            )
            for peer_result, peer_index in peers:
                try:
                    pair = selection.plate(
                        anchor, f"plate:{result_id}:{index}:{peer_result}:{peer_index}"
                    )
                except Http404, AnalysisError:
                    continue
                identity = tuple(entry["identity"] for entry in pair["endpoints"]) + (
                    match_key,
                )
                if identity not in seen:
                    seen.add(identity)
                    candidates.append(pair)
                if len(candidates) >= MAX_CANDIDATES:
                    return candidates
    if anchor.kind in LOCATION_KINDS:
        comparisons = finding_queryset(
            case, {"kind": anchor.kind, "evidence": anchor.item.pk}
        ).filter(result__run__operation__in=SPATIAL_METHODS)
        count = 0
        for result_id, index in comparisons.values_list("result_id", "item_index")[
            :MAX_CANDIDATES
        ]:
            try:
                for pair in selection.spatial_pairs(anchor, result_id, index):
                    candidates.append(pair)
                    count += 1
                    if count >= MAX_CANDIDATES:
                        return candidates
            except Http404, AnalysisError:
                continue
    return candidates


def _proposal(selection, *, anchor, links, supporting, contrary):
    if not 1 <= len(links) <= MAX_SELECTION or len(set(links)) != len(links):
        raise ValidationError(
            f"Choose between 1 and {MAX_SELECTION} distinct direct connections."
        )
    if len(supporting) > MAX_SELECTION or len(contrary) > MAX_SELECTION:
        raise ValidationError(
            f"Choose at most {MAX_SELECTION} observations in each context role."
        )
    original = selection.original(anchor)
    connections = [selection.link(original, token) for token in links]
    group_anchor = connections[0]["endpoints"][0]
    if any(
        pair["endpoints"][0]["identity"] != group_anchor["identity"]
        for pair in connections
    ):
        raise ValidationError(
            "Choose connections from one exact anchor region. "
            "Save other regions separately."
        )
    members = {
        endpoint["selection"] for pair in connections for endpoint in pair["endpoints"]
    }
    support_rows = [_endpoint(selection.original(token)) for token in supporting]
    contrary_rows = [_endpoint(selection.original(token)) for token in contrary]
    context = [entry["selection"] for entry in (*support_rows, *contrary_rows)]
    if len(set(context)) != len(context) or members.intersection(context):
        raise ValidationError(
            "Choose each context observation once. Use observations outside the "
            "connections as context."
        )
    return {
        "anchor": group_anchor,
        "links": connections,
        "supporting": support_rows,
        "contrary": contrary_rows,
        "member_identities": list(
            dict.fromkeys(
                endpoint["identity"]
                for pair in connections
                for endpoint in pair["endpoints"]
            )
        ),
    }


def prepare_proposal(case, anchor, links, supporting, contrary):
    selection = Selection(case)
    selected = {
        "anchor": anchor,
        "links": links,
        "supporting": supporting,
        "contrary": contrary,
    }
    _proposal(selection, **selected)
    return selection.choices(), {
        "finding_refs": list(selection.refs.values()),
        "_sighting_selection": selected,
    }


def build_sighting_group(sources, parameters):
    selection = Selection(sources[0].item.case, sources)
    values = _proposal(selection, **parameters["_sighting_selection"])
    if list(selection.refs.values()) != parameters["finding_refs"]:
        raise AnalysisError("source_changed")
    endpoints = (
        [endpoint for pair in values["links"] for endpoint in pair["endpoints"]]
        + values["supporting"]
        + values["contrary"]
    )
    anchors = {
        json.dumps(endpoint["anchor"], sort_keys=True): endpoint["anchor"]
        for endpoint in endpoints
    }
    return output(
        "A proposal to review the selected direct connections.",
        items=[
            {
                "label": "Sighting group",
                "text": "Candidate connections from selected observations.",
                "source_ids": list(
                    dict.fromkeys(endpoint["source_id"] for endpoint in endpoints)
                ),
                "anchors": list(anchors.values()),
                "values": values,
            }
        ],
        metrics={"Direct connections": len(values["links"])},
        warnings=[
            "Connections record their stated basis. Identity, ownership, driver "
            "presence and shared events remain unresolved.",
            "Visual similarity comparisons are unavailable.",
        ],
    )
