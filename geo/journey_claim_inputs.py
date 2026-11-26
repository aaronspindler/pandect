"""Exact retained findings and saved trip memberships for plan comparisons."""

from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q, Value

from core.analysis.catalogue import (
    finding_queryset,
    finding_rows,
    raw_finding_item,
)
from core.analysis.errors import AnalysisError
from core.analysis.source_resolution import SourceResolutionCache, resolve_inputs
from core.analysis.sources import source_id
from core.evidence.types import LOCATION_KINDS
from core.models import AnalysisFinding
from geo.journey_claim_checks import check_source_integrity
from geo.trip_models import VehicleTrip
from geo.trip_processing import saved_trips, versioned_trip_memberships
from text.analysis.synthesis import validate_references

CLAIM_STATUS = {
    "plan": "proposed",
    "commitment": "proposed",
    "cancellation": "cancelled",
    "correction": "corrected",
    "reported_event": "reported",
    "factual_claim": "reported",
}


def _eligible(role):
    if role == "claim":
        return Q(result__run__operation="content_events", label__in=CLAIM_STATUS)
    if role == "movement":
        return Q(
            result__run__operation="candidate_trips", label="Candidate journey"
        ) | Q(
            result__run__operation="candidate_visits",
            label__regex=r"^Candidate visit to place [0-9]+$",
        )
    raise ValidationError("Choose a claim or a movement record.")


def derived_page(case, role, *, query="", page=1, results=None):
    """Page just the IDs, then validate sources before displaying any excerpts."""
    findings = finding_queryset(
        case, {"q": query, "finding_ids": results.findings if results else ()}
    ).filter(_eligible(role))
    choices = (
        findings.order_by()
        .annotate(choice_type=Value("finding"), recorded=F("result__run__created_at"))
        .values_list("pk", "choice_type", "recorded")
    )
    trips = saved_trips(case)
    if role == "movement":
        if query:
            trips = trips.filter(pk__in=results.records["trip"] if results else ())
        trip_choices = (
            trips.order_by()
            .annotate(choice_type=Value("trip"), recorded=F("start_at"))
            .values_list("pk", "choice_type", "recorded")
        )
        choices = choices.union(trip_choices, all=True)
    result = Paginator(choices.order_by("-recorded", "pk", "choice_type"), 25).get_page(
        page
    )
    entries = list(result.object_list)
    finding_ids = [pk for pk, kind, _ in entries if kind == "finding"]
    labels = {}
    if finding_ids:
        rows = (
            findings.filter(pk__in=finding_ids)
            .select_related("result__run")
            .defer("result__data", "result__provenance")
            .annotate(run_id=F("result__run_id"), excerpt=F("preview"))
        )
        runs = {finding.result_id: finding.result.run for finding in rows}
        unavailable = set()
        cache = SourceResolutionCache(case, with_content=False)
        for result_id, run in runs.items():
            try:
                _check_prior_sources(case, run, cache)
            except AnalysisError:
                unavailable.add(result_id)
        for finding in finding_rows(case, rows):
            if not finding["unavailable"] and finding["result_id"] not in unavailable:
                label = finding["label"].replace("_", " ")
                detail = finding["preview"] or finding["sources"][0]["title"]
                labels[("finding", finding["id"])] = (
                    f"finding:{finding['result_id']}:{finding['item_index']}",
                    f"{label.capitalize()} · {detail}",
                )
    trip_ids = [pk for pk, kind, _ in entries if kind == "trip"]
    if trip_ids:
        for trip in trips.filter(pk__in=trip_ids):
            labels[("trip", trip.pk)] = (
                f"trip:{trip.pk}",
                f"Saved trip · {trip.vehicle} · {trip.start_at.isoformat()} · "
                f"{trip.source_count} observations",
            )
    result.object_list = [
        labels[kind, pk] for pk, kind, _ in entries if (kind, pk) in labels
    ]
    return result


def _choice(snapshot):
    return {
        "kind": snapshot["kind"],
        "id": snapshot["evidence_id"],
        **{
            key: snapshot[key]
            for key in ("attachment_id", "result_id", "geocode_candidate")
            if snapshot.get(key) is not None
        },
    }


def _check_prior_sources(case, run, cache):
    sources = resolve_inputs(run, case, require_current_review=False, cache=cache)
    check_source_integrity(case, sources)


def _finding_input(case, role, ref, *, validate_sources):
    if type(ref.get("item_index")) is not int or not 0 <= ref["item_index"] < 10_000:
        raise AnalysisError("source_unavailable")
    finding = (
        AnalysisFinding.objects.filter(
            _eligible(role),
            case=case,
            result__case=case,
            result__run__case=case,
            result__run__status__in=("complete", "partial"),
            result_id=ref["result_id"],
            item_index=ref["item_index"],
        )
        .annotate(item=raw_finding_item())
        .select_related("result__run")
        .defer("result__data", "result__provenance")
        .first()
    )
    if finding is None or not isinstance(finding.item, dict):
        raise AnalysisError("source_unavailable")
    run = finding.result.run
    item = finding.item
    if item.get("report_only") or item.get("label") != finding.label:
        raise AnalysisError("source_unavailable")
    if validate_sources:
        _check_prior_sources(case, run, SourceResolutionCache(case, with_content=False))
    prior = ref | {"run_id": str(run.pk), "method_version": run.method_version}
    snapshots = {entry["id"]: entry for entry in run.inputs}
    members = item.get("source_ids")
    if (
        not isinstance(members, list)
        or not members
        or not all(isinstance(key, str) and key in snapshots for key in members)
    ):
        raise AnalysisError("source_unavailable")
    claim = group = None
    if role == "claim":
        spans = []
        for anchor in item.get("anchors", []):
            if not isinstance(anchor, dict) or anchor.get("source_id") not in members:
                raise AnalysisError("invalid_anchor")
            if (
                type(anchor.get("start")) is not int
                or type(anchor.get("end")) is not int
                or not 0 <= anchor["start"] < anchor["end"]
            ):
                raise AnalysisError("invalid_anchor")
            spans.append({k: anchor[k] for k in ("source_id", "start", "end")})
        if not spans or {span["source_id"] for span in spans} != set(members):
            raise AnalysisError("invalid_anchor")
        claim = (
            prior
            | spans[0]
            | {
                "citations": spans[1:],
                "status": CLAIM_STATUS[finding.label],
                "original_suggestion_type": finding.label,
            }
        )
    else:
        if (
            len(members) < 2
            or len(set(members)) != len(members)
            or any(snapshots[key]["kind"] not in LOCATION_KINDS for key in members)
        ):
            raise AnalysisError("source_unavailable")
        group = prior | {
            "origin": "finding",
            "label": finding.label,
            "members": members,
        }
    return {
        "selections": [_choice(entry) for entry in run.inputs],
        "label": f"{finding.label.replace('_', ' ').capitalize()} · {finding.preview}",
        "finding_ref": ref,
        "claim": claim,
        "group": group,
    }


def _trip_input(case, trip_id, *, historical=False):
    if historical:
        # Completed generations remain inspectable after replacement. Archived-state
        # changes can rebuild the current generation without changing its originals.
        trips = VehicleTrip.objects.select_related("generation", "vehicle").filter(
            case=case,
            vehicle__case=case,
            kind="trip",
            generation__case=case,
            generation__vehicle_id=F("vehicle_id"),
            generation__state__case=case,
            generation__state__vehicle_id=F("vehicle_id"),
            generation__status="complete",
        )
    else:
        trips = saved_trips(case)
    trip = trips.filter(pk=trip_id).first()
    if trip is None:
        raise AnalysisError("source_unavailable")
    if trip.source_count > settings.PANDECT_ANALYSIS_MAX_SOURCES:
        raise ValidationError(
            "This trip exceeds the comparison source limit. "
            "Choose individual observations instead."
        )
    rows = list(
        versioned_trip_memberships(case, trip)
        .filter(
            input__observation__case=case,
            input__observation__vehicle=trip.vehicle_id,
        )
        .values_list("input__observation_id", flat=True)[: trip.source_count + 1]
    )
    if len(rows) != trip.source_count or len(rows) < 2 or len(set(rows)) != len(rows):
        raise AnalysisError("source_changed")
    choices = [{"kind": "vehicle-location", "id": str(pk)} for pk in rows]
    group = {
        "origin": "trip",
        "label": "Saved vehicle trip",
        "trip_id": str(trip.pk),
        "generation_id": str(trip.generation_id),
        "revision": trip.generation.revision,
        "method_version": trip.generation.method_version,
        "vehicle_id": str(trip.vehicle_id),
        "evidence_state": trip.evidence_state,
        "members": [source_id(choice) for choice in choices],
    }
    return {
        "selections": choices,
        "label": f"Saved vehicle trip · {trip.vehicle} · {trip.start_at.isoformat()}",
        "finding_ref": None,
        "claim": None,
        "group": group,
    }


def expand_input(case, token, role):
    """Expand one exact choice; no lookup substitutes a newer run or trip."""
    try:
        kind, key, *index = token.split(":")
        key = str(UUID(key))
        if (
            kind == "finding"
            and len(index) == 1
            and index[0].isascii()
            and index[0].isdecimal()
        ):
            ref = {"result_id": key, "item_index": int(index[0])}
            return _finding_input(case, role, ref, validate_sources=True)
        if kind == "trip" and role == "movement" and not index:
            return _trip_input(case, key)
    except ValueError, AttributeError:
        pass
    raise ValidationError("Choose an available retained finding or trip in this case.")


def validate_dependencies(run, sources, case, *, cache=None, depth=0):
    """Verify the exact antecedents, membership and citation of each derived input."""
    validate_references(run, sources, case, cache=cache, depth=depth)
    parameters = run.parameters
    expected_refs = set()
    checked_findings = {}
    try:

        def selected_finding(entry, role):
            ref = {
                "result_id": str(UUID(entry["result_id"])),
                "item_index": entry["item_index"],
            }
            key = (ref["result_id"], ref["item_index"])
            expected_refs.add(key)
            if (key, role) not in checked_findings:
                checked_findings[key, role] = _finding_input(
                    case, role, ref, validate_sources=False
                )
            return checked_findings[key, role]

        for claim in parameters["claims"]:
            if {
                "result_id",
                "item_index",
                "run_id",
                "method_version",
                "original_suggestion_type",
            }.intersection(claim):
                pinned = selected_finding(claim, "claim")["claim"]
                # Status is an investigator interpretation. Its original inferred
                # type and every source citation stay pinned to the prior finding.
                for key in pinned.keys() - {"status"}:
                    if claim.get(key) != pinned[key]:
                        raise AnalysisError("source_changed")
        observations = parameters["observations"]
        input_ids = {str(source.id) for source in sources}
        for group in parameters.get("groups", []):
            if group["origin"] == "finding":
                selected = selected_finding(group, "movement")["group"]
            elif group["origin"] == "trip":
                historical = not run._state.adding or bool(run.previous_run_id)
                selected = _trip_input(case, group["trip_id"], historical=historical)[
                    "group"
                ]
            else:
                raise AnalysisError("source_unavailable")
            if any(
                type(i) is not int or not 0 <= i < len(observations)
                for i in group["members"]
            ):
                raise AnalysisError("source_changed")
            members = [observations[i] for i in group["members"]]
            if (
                not set(members) <= input_ids
                or group | {"members": members} != selected
            ):
                raise AnalysisError("source_changed")
        refs = parameters.get("finding_refs", [])
        if len(refs) != len(expected_refs) or expected_refs != {
            (ref["result_id"], ref["item_index"]) for ref in refs
        }:
            raise AnalysisError("source_unavailable")
    except TypeError, IndexError, KeyError, AttributeError, ValueError, ValidationError:
        raise AnalysisError("source_unavailable") from None
