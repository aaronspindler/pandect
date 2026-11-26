"""Saved, complete vehicle trips and investigator-maintained person POIs."""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from core.analysis.catalogue import PER_PAGE, finding_queryset, finding_rows
from core.models import AnalysisFindingSource
from geo.map_navigation import current_map_trip, map_query, trip_query
from geo.models import PointOfInterest
from geo.report_forms import TripMapForm
from geo.trip_geometry import GEOMETRY_CHUNK_SIZE, geometry_revision, source_positions
from geo.trip_processing import (
    _source_rows,
    format_trip,
    geometry_source_page,
    prepare_case_trips,
    trip_readiness,
)
from geo.trip_reports import saved_trip_report
from ui.live import render_live


def poi_map_data(request, case, query, return_query):
    pois = PointOfInterest.objects.filter(person__case=case).select_related("person")
    if query:
        pois = pois.filter(
            Q(kind__icontains=query)
            | Q(custom_label__icontains=query)
            | Q(address__icontains=query)
            | Q(source__icontains=query)
            | Q(person__first_name__icontains=query)
            | Q(person__last_name__icontains=query)
            | Q(person__email__icontains=query)
            | Q(person__phone_number__icontains=query)
        )
    page = Paginator(pois, 100).get_page(request.GET.get("poi_page"))
    points = []
    for index, poi in enumerate(page, start=page.start_index()):
        poi.number = f"P{index}"
        poi.map_detail_url = reverse(
            "geo:poi_detail", args=(case.pk, poi.pk), query=return_query
        )
        if poi.plottable:
            points.append(
                {
                    "number": poi.number,
                    "latitude": float(poi.latitude),
                    "longitude": float(poi.longitude),
                    "title": str(poi),
                    "poi": True,
                    "url": poi.map_detail_url,
                }
            )
    return page, points


def _map_context(request, case, *, trip=None):
    query = request.GET.get("q", "")[:200].strip()
    if trip is None:
        return_query = {"from_map": "1"} | map_query(request.GET)
    else:
        return_query = {"from_trip": trip.pk} | trip_query(request.GET)
    poi_page, points = poi_map_data(request, case, query, return_query)
    return {
        **case_context(case, request.user),
        "poi_page": poi_page,
        "points": points,
        "poi_mapped_count": len(points),
        "query": query,
        "tile_url": settings.PANDECT_MAP_TILE_URL,
        "tile_attribution": settings.PANDECT_MAP_TILE_ATTRIBUTION,
        "map_url": reverse("geo:case_map", args=(case.pk,)),
        "poi_create_url": reverse(
            "geo:poi_create",
            args=(case.pk,),
            query=return_query,
        ),
    }


@product_login_required
@require_safe
def case_map(request, case_id):
    case = get_case(request.user, case_id)
    form = TripMapForm(request.GET, case=case)
    params = map_query(request.GET)
    trip_report = None
    if form.is_valid():
        trip_report = saved_trip_report(
            case,
            vehicle_id=form.cleaned_data["vehicle_id"] or None,
            page=params.get("page"),
            gap_page=params.get("gap_page"),
            day_bounds=form.day_bounds,
        )
    summaries = trip_report["items"] if trip_report else []
    suffix = "?" + urlencode(params) if params else ""
    for summary in summaries:
        summary["detail_url"] = summary["trip_url"] + suffix
    day_trip = next(
        (trip for trip in summaries if trip["id"] == trip_report["day_trip_id"]),
        None,
    )
    selected = day_trip or (summaries[0] if summaries else None)
    context = _map_context(request, case)
    if trip_report:
        context.update(
            pending=trip_report["processing_pending"],
            failed=trip_report["failed"],
            needs_prepare=trip_report["needs_prepare"],
        )
    else:
        context.update(trip_readiness(case))
    clear_keys = {"q", "poi_page"}
    if form["day"].value() and not form["vehicle_id"].errors:
        clear_keys.add("vehicle_id")
    context.update(
        form=form,
        map_params=params,
        clear_trip_url=reverse(
            "geo:case_map",
            args=(case.pk,),
            query={key: value for key, value in params.items() if key in clear_keys},
        ),
        trip_report=trip_report,
        page_obj=trip_report["page_obj"] if trip_report else None,
        trips=summaries,
        day_trip=day_trip,
        selected_trip=selected,
        geometry_url=selected["trip_url"] if selected else "",
    )
    if wants_json(request):
        return JsonResponse(
            {
                "pending": context["pending"],
                "failed": context["failed"],
                "trip_count": trip_report["page_obj"].paginator.count
                if trip_report
                else 0,
            }
        )
    return render_live(request, "geo/map.html", context)


def _trip_findings(case, sources, page_number):
    # Match the exact observation version cited by a finding, anywhere in the trip.
    matching_input = sources.filter(
        observation_id=OuterRef("evidence_id"), sha256=OuterRef("source_sha256")
    )
    citations = AnalysisFindingSource.objects.filter(
        case=case, kind="vehicle-location", finding_id=OuterRef("pk")
    ).filter(Exists(matching_input))
    findings = (
        finding_queryset(case, {"weak": "1"})
        .filter(Exists(citations))
        .annotate(run_id=F("result__run_id"), excerpt=F("preview"))
        .only("pk", "result_id", "item_index", "preview", "label", "weak")
    )
    page = Paginator(findings, PER_PAGE).get_page(page_number)
    page.object_list = finding_rows(case, page.object_list, show_weak=True)
    return page


@product_login_required
@require_GET
def trip_detail(request, case_id, trip_id):
    case = get_case(request.user, case_id)
    geometry = wants_json(request)
    trip = current_map_trip(case, trip_id, geometry=geometry)
    if geometry:
        cursor = request.GET.get("cursor", "0")
        if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 18:
            return JsonResponse(
                {"message": "Choose a valid trip position."}, status=400
            )
        cursor = int(cursor)
        recheck = request.GET.get("recheck") == "1"
        if recheck and trip.source_count > GEOMETRY_CHUNK_SIZE:
            return JsonResponse({"message": "Load current trip positions."}, status=409)
        inputs = geometry_source_page(
            case, trip, 0 if recheck else cursor, GEOMETRY_CHUNK_SIZE + 1
        )
        if recheck:
            if any(
                item.snapshot.get("latitude") is None
                or item.snapshot.get("longitude") is None
                for item in inputs
            ):
                return JsonResponse(
                    {"message": "Load current trip positions."}, status=409
                )
            if request.GET.get("revision") != geometry_revision(
                case, trip, reported_only=True
            ):
                return JsonResponse(
                    {"message": "Trip positions changed. Reload the trip."}, status=409
                )
            return HttpResponse(status=204)
        has_next = len(inputs) > GEOMETRY_CHUNK_SIZE
        can_reuse = (
            cursor == 0
            and not has_next
            and all(
                item.snapshot.get("latitude") is not None
                and item.snapshot.get("longitude") is not None
                for item in inputs
            )
        )
        revision = geometry_revision(case, trip, reported_only=can_reuse)
        if request.GET.get("revision", revision) != revision:
            return JsonResponse(
                {"message": "Trip positions changed. Reload the trip."}, status=409
            )
        rows = source_positions(case, inputs[:GEOMETRY_CHUNK_SIZE])
        if not can_reuse and revision != geometry_revision(case, trip):
            return JsonResponse(
                {"message": "Trip positions changed. Reload the trip."}, status=409
            )
        return JsonResponse(
            {
                "trip_id": str(trip.pk),
                "revision": revision,
                "can_reuse": can_reuse,
                "positions": [
                    {
                        "position": row["position"],
                        "label": row["position_label"],
                        "attribution": row["attribution"],
                        "speed_kmh": float(row["speed_kmh"])
                        if row["speed_kmh"] is not None
                        else None,
                    }
                    for row in rows
                ],
                "next_cursor": rows[-1]["ordinal"] if has_next else None,
                "source_count": trip.source_count,
            }
        )
    sources = _source_rows(case, trip)
    address_sources = sources.filter(snapshot__latitude=None)
    review_estimates = request.GET.get("view") == "estimates"
    page = Paginator(address_sources if review_estimates else sources, 50).get_page(
        request.GET.get("sources_page")
    )
    context = _map_context(request, case, trip=trip)
    back_params = map_query(request.GET)
    context.update(
        back_url=reverse("geo:case_map", args=(case.pk,), query=back_params),
        selected_trip=format_trip(case, trip),
        findings_page=_trip_findings(case, sources, request.GET.get("findings_page")),
        geometry_url=reverse("geo:trip_detail", args=(case.pk, trip.pk)),
        source_page=page,
        source_rows=source_positions(case, list(page)),
        has_address_sources=review_estimates or address_sources.exists(),
        review_estimates=review_estimates,
        detail=True,
    )
    return render(request, "geo/trip_detail.html", context)


@product_login_required
@require_POST
def prepare_trips(request, case_id):
    # The service repeats permission checks under the case lock before enqueueing.
    try:
        case = get_case(request.user, case_id, edit=True)
        vehicle_id = request.POST.get("vehicle_id")
        if vehicle_id and not case.vehicles.filter(pk=vehicle_id).exists():
            raise Http404("Vehicle not found.")
        generations = prepare_case_trips(
            request.user,
            case_id,
            vehicle_ids=[vehicle_id] if vehicle_id else None,
            retry=request.POST.get("retry") == "1",
            worker_stopped=request.POST.get("worker_stopped") == "1",
        )
    except ValidationError as error:
        if wants_json(request):
            return JsonResponse({"message": " ".join(error.messages)}, status=400)
        messages.error(request, " ".join(error.messages))
    else:
        if wants_json(request):
            return JsonResponse({"queued": bool(generations)})
        messages.success(
            request, "Trip preparation requested. Refresh to see progress."
        )
    return redirect(
        reverse("geo:case_map", args=(case_id,), query=map_query(request.GET))
    )


@product_login_required
@require_GET
def trip_citation(request, case_id, trip_id):
    from geo.trip_citations import citation_fields, citation_inputs, citation_trips
    from geo.trip_processing import format_source

    case = get_case(request.user, case_id)
    trip = get_object_or_404(citation_trips(case, retained=True), pk=trip_id)
    inputs = citation_inputs(case, trip)
    if not inputs:
        raise Http404("The cited observations are unavailable.")
    return render(
        request,
        "geo/trip_citation.html",
        case_context(case, request.user)
        | {
            "trip": trip,
            "fields": citation_fields(trip, inputs),
            "sources": [format_source(case, item) for item in inputs],
        },
    )
