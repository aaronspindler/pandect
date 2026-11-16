"""Bounded, read-time positions and provenance for saved vehicle trips."""

import hashlib
from collections import defaultdict

from django.db.models import CharField, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, JSONObject
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from core.analysis.navigation import finding_url
from core.evidence.queries import scoped_evidence
from core.models import AnalysisResult, AnalysisReview, AnalysisRun
from geo.analysis.deterministic.locations import coordinate
from geo.map_geocoding import local_geocoder_ready

GEOMETRY_CHUNK_SIZE = 500
MERCATOR_LATITUDE = 85.05112878


def geometry_revision(case, trip):
    """A review or newer lookup invalidates an in-progress geometry replay."""
    identities = [str(trip.generation_id), str(trip.pk)]
    for queryset in (
        AnalysisRun.objects.filter(case=case, operation="geocode_locations"),
        AnalysisResult.objects.filter(case=case, run__operation="geocode_locations"),
        AnalysisReview.objects.filter(
            case=case, result__run__operation="geocode_locations"
        ),
    ):
        latest = (
            queryset.order_by("-created_at", "-pk")
            .values_list("pk", "created_at")
            .first()
        )
        identities.append(str(latest))
    return hashlib.sha256("\n".join(identities).encode()).hexdigest()


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
            "review_url": "",
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
                if abs(latitude) <= MERCATOR_LATITUDE:
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
            row["review_url"] = reverse("core:analysis:detail", args=(case.pk, run.pk))
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
                row["review_url"] = finding_url(case.pk, run.pk, matching[0])
                row["position_label"] = "Review address estimates"
            else:
                row["position_label"] = "No usable local address match"
            if len(accepted) > 1:
                row["position_label"] = "Multiple accepted estimates; choose one"
            elif len(accepted) == 1:
                index, position, values = accepted[0]
                row["review_url"] = finding_url(case.pk, run.pk, index)
                if abs(position[0]) > MERCATOR_LATITUDE:
                    row["position_label"] = (
                        "Reviewed estimate outside map display range"
                    )
                    continue
                row.update(
                    position=list(position),
                    position_label="Reviewed address estimate",
                    attribution=" · ".join(
                        dict.fromkeys(
                            str(values[key])
                            for key in ("Attribution", "licence")
                            if values.get(key)
                        )
                    ),
                )
    return rows
