"""Bounded, read-time positions and provenance for saved vehicle trips."""

import hashlib
from collections import defaultdict

from django.db.models import CharField, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, JSONObject
from django.http import Http404
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from cases.models import CaseActivity
from core.analysis.navigation import finding_url
from core.evidence.queries import scoped_evidence
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from geo.analysis.deterministic.locations import WEB_MERCATOR_MAX_LATITUDE, coordinate
from geo.map_geocoding import local_geocoder_ready
from geo.trip_models import VehicleTrip

GEOMETRY_CHUNK_SIZE = 500


def geometry_revision(case, trip, *, reported_only=False):
    """Identify the current geometry after the caller verifies trip sources."""
    evidence = (
        CaseActivity.objects.filter(case=case)
        .values_list("evidence", flat=True)
        .first()
    )
    if evidence is None:
        raise Http404("Trip not found.")
    if reported_only:
        return hashlib.sha256(
            f"{trip.generation_id}\n{trip.pk}\n{evidence}".encode()
        ).hexdigest()

    # Reviews and new lookups can change an estimate during a replay.
    changes = {
        "latest_lookup": AnalysisRun.objects.filter(
            case=case, operation="geocode_locations"
        ),
        "latest_result": AnalysisResult.objects.filter(
            case=case, run__operation="geocode_locations"
        ),
        "latest_review": AnalysisReview.objects.filter(
            case=case, result__run__operation="geocode_locations"
        ),
    }
    revision = (
        VehicleTrip.objects.filter(pk=trip.pk, case=case)
        .annotate(
            **{
                name: Subquery(query.order_by("-created_at", "-pk").values("pk")[:1])
                for name, query in changes.items()
            }
        )
        .values_list("generation_id", "pk", *changes)
        .first()
    )
    if revision is None:
        raise Http404("Trip not found.")
    scope = "\n".join(map(str, (*revision, evidence)))
    return hashlib.sha256(scope.encode()).hexdigest()


def _latest_local_lookup(case):
    original = JSONObject(
        kind=Value("vehicle-location"),
        evidence_id=Cast(OuterRef("id"), CharField()),
        sha256=OuterRef("sha256"),
    )
    return Subquery(
        AnalysisRun.objects.filter(
            case=case,
            operation="geocode_locations",
            method_version="2",
            external_processing=False,
            parameters___execution__backend="local",
            parameters___execution__location="local",
            parameters___execution__purpose="geocode",
            parameters___execution__has_keys=["url", "version"],
            inputs__contains=Func(original, function="jsonb_build_array"),
        )
        .exclude(parameters___execution__url="")
        .exclude(parameters___execution__version="")
        .filter(Q(parameters__reverse=False) | Q(parameters__reverse__isnull=True))
        .order_by("-created_at", "id")
        .values("pk")[:1]
    )


def source_positions(case, inputs):
    """Resolve only this bounded source slice; one accepted candidate is required."""
    rows = []
    addresses = []
    for item in inputs:
        original = item.snapshot
        row = {
            "ordinal": item.ordinal,
            "id": str(item.observation_id),
            "title": original["title"],
            "place": original["address"]
            or f"{original['latitude']}, {original['longitude']}",
            "time": parse_datetime(original["observed_at"])
            if original.get("observed_at")
            else None,
            "speed_kmh": original.get("speed_kmh"),
            "url": reverse(
                "core:evidence:detail",
                args=(case.pk, "vehicle-location", item.observation_id),
            ),
            "row": original.get("source_row"),
            "import_url": reverse(
                "geo:vehicle_import_review", args=(case.pk, original["import_id"])
            )
            if original.get("import_id")
            else "",
            "position": None,
            "position_label": "Address awaiting local lookup",
            "estimate_url": "",
            "attribution": "",
        }
        if original.get("latitude") is not None:
            try:
                latitude, longitude = coordinate(
                    original["latitude"], original["longitude"]
                )
            except ValueError:
                row["position_label"] = "Reported coordinates are invalid"
            else:
                if abs(latitude) <= WEB_MERCATOR_MAX_LATITUDE:
                    row.update(
                        position=[latitude, longitude],
                        position_label="Reported coordinates",
                    )
                else:
                    row["position_label"] = "Outside map display range"
        else:
            addresses.append((item, row))
        rows.append(row)
    if not addresses:
        return rows

    lookup_ids = dict(
        scoped_evidence(case, "vehicle-location")
        .filter(pk__in=[item.observation_id for item, _ in addresses])
        .annotate(lookup_id=_latest_local_lookup(case))
        .values_list("pk", "lookup_id")
    )
    by_run = defaultdict(list)
    local_ready = local_geocoder_ready()
    for item, row in addresses:
        run_id = lookup_ids.get(item.observation_id)
        if run_id:
            by_run[run_id].append((item, row))
        elif not local_ready:
            row["position_label"] = "Local address lookup is not configured"
    reviews = defaultdict(dict)
    for result_id, index, decision in (
        AnalysisReview.objects.filter(
            case=case,
            result__case=case,
            result__run_id__in=by_run,
            item_index__isnull=False,
        )
        .order_by("result_id", "item_index", "-created_at", "id")
        .distinct("result_id", "item_index")
        .values_list("result_id", "item_index", "decision")
    ):
        reviews[result_id][index] = decision
    # Repeated addresses share one bounded retained result.
    for run in (
        AnalysisRun.objects.filter(case=case, pk__in=by_run)
        .select_related("result")
        .iterator(chunk_size=1)
    ):
        result = getattr(run, "result", None)
        for item, row in by_run[run.pk]:
            row["estimate_url"] = reverse(
                "core:analysis:detail", args=(case.pk, run.pk)
            )
            if run.status in {"queued", "running"}:
                row["position_label"] = "Local address lookup in progress"
                continue
            if (
                run.status not in {"complete", "partial"}
                or not result
                or result.case_id != case.pk
            ):
                row["position_label"] = "Local address lookup needs attention"
                continue
            source_ids = {
                source["id"]
                for source in run.inputs
                if source.get("kind") == "vehicle-location"
                and source.get("evidence_id") == str(item.observation_id)
                and source.get("sha256") == item.sha256
            }
            accepted = []
            matching = []
            for index, candidate in enumerate(result.data.get("items", [])):
                if not source_ids.intersection(candidate.get("source_ids", [])):
                    continue
                values = candidate.get("values", {})
                if values.get("latitude") is None:
                    continue
                try:
                    position = coordinate(values["latitude"], values.get("longitude"))
                except ValueError:
                    continue
                matching.append(index)
                if reviews[result.pk].get(index) == "accepted":
                    accepted.append((index, position, values))
            if matching:
                row["estimate_url"] = finding_url(case.pk, run.pk, matching[0])
                row["position_label"] = "Choose an address estimate"
            else:
                row["position_label"] = "No usable local address match"
            if len(accepted) > 1:
                row["position_label"] = (
                    "Multiple estimates selected. Leave one selected."
                )
            elif len(accepted) == 1:
                index, position, values = accepted[0]
                row["estimate_url"] = finding_url(case.pk, run.pk, index)
                if abs(position[0]) > WEB_MERCATOR_MAX_LATITUDE:
                    row["position_label"] = "Address estimate outside map display range"
                    continue
                row.update(
                    position=list(position),
                    position_label="Selected address estimate",
                    attribution=" · ".join(
                        dict.fromkeys(
                            str(values[key])
                            for key in ("Attribution", "licence")
                            if values.get(key)
                        )
                    ),
                )
    return rows
