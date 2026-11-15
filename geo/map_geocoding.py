"""Local map estimates backed by retained analysis runs, never evidence edits."""

from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import CharField, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, JSONObject

from core.analysis.navigation import finding_url
from core.analysis.services import start_run
from core.evidence.queries import scoped_evidence
from core.models import AnalysisReview, AnalysisRun
from geo.analysis.deterministic.locations import coordinate
from geo.analysis.external.geocoding import MAX_LOOKUPS, Geocoder


def local_geocoder_ready():
    if not settings.PANDECT_GEOCODER_LOCAL:
        return False
    try:
        Geocoder()  # Validate configuration only; the worker performs requests.
    except ValueError:
        return False
    return True


def latest_local_run(case, kind):
    """Correlate source ID/checksum for already paginated address observations."""
    source = JSONObject(
        kind=Value(kind),
        evidence_id=Cast(OuterRef("id"), CharField()),
        sha256=OuterRef("sha256"),
    )
    return Subquery(
        AnalysisRun.objects.filter(
            case=case,
            operation="geocode_locations",
            external_processing=False,
            parameters___execution__location="local",
            inputs__contains=Func(source, function="jsonb_build_array"),
        )
        .filter(Q(parameters__reverse=False) | Q(parameters__reverse__isnull=True))
        .order_by("-created_at", "id")
        .values("id")[:1]
    )


def address_estimates(case, rows):
    """Resolve at most one latest local run per visible immutable original.

    Paginate before correlating analysis history so off-page observations never
    scan lookup runs. Shared results/reviews are loaded once for visible rows.
    """
    by_run = {}
    by_kind = defaultdict(list)
    for row in rows:
        if row["latitude"] is not None:
            continue
        row["geocode"] = {
            "label": "Address not located",
            "url": "",
            "new": False,
            "pending": False,
            "retryable": False,
            "points": [],
        }
        if not row["address"].strip() or len(row["address"]) > 2_000:
            row["geocode"]["label"] = "Address is unsupported for lookup"
            continue
        by_kind[row["kind"]].append(row)
    for kind, sources in by_kind.items():
        run_ids = {
            (source_id, checksum): run_id
            for source_id, checksum, run_id in scoped_evidence(case, kind)
            .filter(pk__in=[row["id"] for row in sources])
            .order_by()
            .annotate(geocode_run_id=latest_local_run(case, kind))
            .values_list("pk", "sha256", "geocode_run_id")
        }
        for row in sources:
            run_id = run_ids.get((row["id"], row["sha256"]))
            if run_id:
                by_run.setdefault(run_id, []).append(row)
            else:
                row["geocode"]["new"] = True
    reviews_by_result = defaultdict(dict)
    reviews = (
        AnalysisReview.objects.filter(
            case=case,
            result__case=case,
            result__run_id__in=by_run,
            item_index__isnull=False,
        )
        .order_by("result_id", "item_index", "-created_at", "id")
        .distinct("result_id", "item_index")
        .values_list("result_id", "item_index", "decision")
    )
    for result_id, index, decision in reviews:
        reviews_by_result[result_id][index] = decision
    runs = AnalysisRun.objects.filter(case=case, pk__in=by_run).select_related("result")
    for run in runs.iterator(chunk_size=1):
        try:
            result = run.result
        except ObjectDoesNotExist:
            result = None
        reviews = reviews_by_result.get(result.pk, {}) if result else {}
        for row in by_run[run.pk]:
            state = row["geocode"]
            state["url"] = finding_url(case.pk, run.pk, 0)
            if run.status in {"queued", "running"}:
                state.update(pending=True, label="Locating address locally…")
                continue
            state["retryable"] = True
            if (
                run.status not in {"complete", "partial"}
                or not result
                or result.case_id != case.pk
            ):
                state["label"] = (
                    "Address lookup failed; retry when the service is ready"
                )
                continue
            source_ids = {
                entry["id"]
                for entry in run.inputs
                if entry["kind"] == row["kind"]
                and entry["evidence_id"] == str(row["id"])
                and entry["sha256"] == row["sha256"]
            }
            state["label"] = "No address match found"
            matched = False
            for index, finding in enumerate(result.data.get("items", [])):
                if not source_ids.intersection(finding.get("source_ids", [])):
                    continue
                if not matched:
                    state["url"] = finding_url(case.pk, run.pk, index)
                    matched = True
                values = finding.get("values", {})
                if values.get("Failure code"):
                    state["label"] = "Address lookup unavailable; try again later"
                if values.get("latitude") is None:
                    continue
                if reviews.get(index) == "rejected":
                    state["label"] = "Address estimates rejected"
                    continue
                try:
                    latitude, longitude = coordinate(
                        values["latitude"], values.get("longitude")
                    )
                except ValueError:
                    state["label"] = "Address estimate has invalid coordinates"
                    continue
                if abs(latitude) > 85.05112878:
                    state["label"] = "Address estimate outside map display range"
                    continue
                label = (
                    "Reviewed address estimate"
                    if reviews.get(index) == "accepted"
                    else "Unreviewed address estimate"
                )
                state["points"].append(
                    {
                        "number": row["number"],
                        "latitude": latitude,
                        "longitude": longitude,
                        "title": row["title"],
                        "url": row["url"],
                        "derived": True,
                        "label": label,
                        "review_url": finding_url(case.pk, run.pk, index),
                        "attribution": " · ".join(
                            dict.fromkeys(
                                str(values[key])
                                for key in ("Attribution", "licence")
                                if values.get(key)
                            )
                        ),
                    }
                )
            if state["points"]:
                state["retryable"] = False
                state["label"] = (
                    state["points"][0]["label"]
                    if len(state["points"]) == 1
                    else f"{len(state['points'])} address estimates"
                )
    return rows


def queue_addresses(actor, case, rows, *, retry=False):
    """Caller holds the case lock; repeated POSTs see the queued run and reuse it."""
    addresses = set()
    selections = []
    for row in rows:
        if len(selections) >= settings.PANDECT_ANALYSIS_MAX_SOURCES:
            break
        state = row.get("geocode", {})
        if not (state.get("new") or (retry and state.get("retryable"))):
            continue
        if row["address"] not in addresses and len(addresses) >= MAX_LOOKUPS:
            continue
        addresses.add(row["address"])
        selections.append({"kind": row["kind"], "id": str(row["id"])})
    if selections:
        # start_run records the local endpoint identity and atomically enqueues
        # the existing authorized worker. Configuration drift fails closed.
        return start_run(
            actor,
            case.pk,
            "geocode_locations",
            selections,
            parameters={"reverse": False, "map": True},
        )[0]
    return None
