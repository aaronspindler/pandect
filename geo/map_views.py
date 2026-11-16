"""Saved, complete vehicle trips and investigator-maintained person POIs."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from geo.models import PointOfInterest
from geo.report_forms import VehicleReportForm
from geo.trip_geometry import GEOMETRY_CHUNK_SIZE, geometry_revision, source_positions
from geo.trip_models import VehicleTrip
from geo.trip_processing import (
    format_trip,
    prepare_case_trips,
    saved_trips,
    trip_readiness,
    trip_sources,
)
from ui.live import render_live


class TripMapForm(VehicleReportForm):
    """Filter complete saved trips rather than cutting their input observations."""

    q = forms.CharField(required=False, max_length=200, label="Search POIs")


def poi_map_data(request, case, query):
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
        if poi.plottable:
            points.append(
                {
                    "number": poi.number,
                    "latitude": float(poi.latitude),
                    "longitude": float(poi.longitude),
                    "title": str(poi),
                    "poi": True,
                    "url": reverse("geo:poi_detail", args=(case.pk, poi.pk)),
                }
            )
    return page, points


def _trip_url(case, trip, params=None):
    url = reverse("geo:trip_detail", args=(case.pk, trip.pk))
    return url + (f"?{params.urlencode()}" if params else "")


def _summary(case, trip, params=None):
    return format_trip(case, trip) | {"detail_url": _trip_url(case, trip, params)}


def _map_context(request, case):
    query = request.GET.get("q", "")[:200].strip()
    poi_page, points = poi_map_data(request, case, query)
    return {
        **case_context(case, request.user),
        **trip_readiness(case),
        "poi_page": poi_page,
        "points": points,
        "poi_mapped_count": len(points),
        "query": query,
        "tile_url": settings.PANDECT_MAP_TILE_URL,
        "tile_attribution": settings.PANDECT_MAP_TILE_ATTRIBUTION,
        "map_url": reverse("geo:case_map", args=(case.pk,)),
    }


@product_login_required
@require_GET
def case_map(request, case_id):
    case = get_case(request.user, case_id)
    data = request.GET.copy()
    if data:
        data.setdefault("state", "active")
    form = TripMapForm(data or None, case=case)
    filters = {"state": "active"}
    trips = saved_trips(case)
    if form.is_bound:
        if form.is_valid():
            filters = {
                key: value or None
                for key, value in form.cleaned_data.items()
                if key != "q"
            }
            trips = saved_trips(case, **filters)
        else:
            trips = trips.none()
    page = Paginator(trips, 25).get_page(request.GET.get("page"))
    context = _map_context(request, case)
    selected = page.object_list[0] if page else None
    context.update(
        form=form,
        page_obj=page,
        trips=[_summary(case, trip, request.GET) for trip in page],
        selected_trip=_summary(case, selected, request.GET) if selected else None,
        geometry_url=_trip_url(case, selected) if selected else "",
        filters_applied=bool(request.GET),
        state=filters["state"],
    )
    if wants_json(request):
        return JsonResponse(
            {
                "pending": context["pending"],
                "failed": context["failed"],
                "trip_count": page.paginator.count,
            }
        )
    return render_live(request, "geo/map.html", context)


@product_login_required
@require_GET
def trip_detail(request, case_id, trip_id):
    case = get_case(request.user, case_id)
    state = (
        VehicleTrip.objects.filter(case=case, vehicle__case=case, pk=trip_id)
        .values_list("evidence_state", flat=True)
        .first()
    )
    if not state:
        raise Http404("Trip not found.")
    try:
        trip = saved_trips(case, state=state).get(pk=trip_id)
    except VehicleTrip.DoesNotExist as error:
        raise Http404(
            "This trip changed. Open the map to see the current trips."
        ) from error
    sources = trip_sources(case, trip)
    if wants_json(request):
        cursor = request.GET.get("cursor", "0")
        if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 18:
            return JsonResponse(
                {"message": "Choose a valid trip position."}, status=400
            )
        revision = geometry_revision(case, trip)
        if request.GET.get("revision", revision) != revision:
            return JsonResponse(
                {"message": "Trip positions changed. Reload the trip."}, status=409
            )
        inputs = list(
            sources.filter(ordinal__gt=int(cursor))[: GEOMETRY_CHUNK_SIZE + 1]
        )
        has_next = len(inputs) > GEOMETRY_CHUNK_SIZE
        rows = source_positions(case, inputs[:GEOMETRY_CHUNK_SIZE])
        if revision != geometry_revision(case, trip):
            return JsonResponse(
                {"message": "Trip positions changed. Reload the trip."}, status=409
            )
        return JsonResponse(
            {
                "trip_id": str(trip.pk),
                "revision": revision,
                "positions": [
                    {
                        "position": row["position"],
                        "label": row["position_label"],
                        "attribution": row["attribution"],
                    }
                    for row in rows
                ],
                "next_cursor": rows[-1]["ordinal"] if has_next else None,
                "source_count": trip.source_count,
            }
        )
    page = Paginator(sources, 50).get_page(request.GET.get("sources_page"))
    context = _map_context(request, case)
    back_params = request.GET.copy()
    for key in tuple(back_params):
        if key not in {"state", "vehicle_id", "start", "end", "q", "page", "poi_page"}:
            del back_params[key]
    if trip.evidence_state != "active":
        back_params.setdefault("state", trip.evidence_state)
    back_url = context["map_url"]
    if back_params:
        back_url += "?" + back_params.urlencode()
    context.update(
        back_url=back_url,
        selected_trip=_summary(case, trip),
        geometry_url=_trip_url(case, trip),
        source_page=page,
        source_rows=source_positions(case, list(page)),
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
    return redirect("geo:case_map", case_id=case_id)
