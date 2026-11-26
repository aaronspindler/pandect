"""Person-linked map annotations using current case permissions."""

from contextlib import suppress
from uuid import UUID

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from geo.map_navigation import current_map_trip, map_query, poi_list_query, trip_query
from geo.models import PointOfInterest
from geo.poi_forms import PointOfInterestDeleteForm, PointOfInterestForm
from people.models import Person


def _navigation(case, values, person_id=None):
    """Resolve the originating case view independently of the saved association."""
    from_person = values.get("from_person", "")
    origin_person_id = None
    if from_person == "1":
        origin_person_id = person_id
    elif len(from_person) == 36:
        with suppress(ValueError):
            origin_person_id = UUID(from_person)
    if origin_person_id and (
        origin_person_id == person_id
        or case.people.filter(pk=origin_person_id).exists()
    ):
        filters = poi_list_query(values)
        return (
            {"from_person": origin_person_id} | filters,
            reverse(
                "people:detail",
                args=(case.pk, origin_person_id),
                query=filters,
                fragment="poi-list",
            ),
            "Back to person",
        )
    from_trip = values.get("from_trip", "")
    if len(from_trip) == 36:
        with suppress(ValueError, Http404):
            trip = current_map_trip(case, UUID(from_trip))
            filters = trip_query(values)
            return (
                {"from_trip": trip.pk} | filters,
                reverse(
                    "geo:trip_detail",
                    args=(case.pk, trip.pk),
                    query=filters,
                    fragment="map-heading",
                ),
                "Back to trip",
            )
    filters = map_query(values)
    return (
        ({"from_map": "1"} if from_trip or values.get("from_map") == "1" else {})
        | filters,
        reverse(
            "geo:case_map",
            args=(case.pk,),
            query=filters,
            fragment="map-heading",
        ),
        "Back to map",
    )


@product_login_required
@require_safe
def poi_detail(request, case_id, poi_id):
    case = get_case(request.user, case_id)
    poi = get_object_or_404(
        PointOfInterest.objects.select_related("person", "created_by"),
        person__case=case,
        pk=poi_id,
    )
    origin, back_url, back_label = _navigation(case, request.GET, poi.person_id)
    return render(
        request,
        "geo/poi_detail.html",
        {
            **case_context(case, request.user),
            "poi": poi,
            "back_url": back_url,
            "back_label": back_label,
            "edit_url": reverse("geo:poi_edit", args=(case.pk, poi.pk), query=origin),
            "delete_url": reverse(
                "geo:poi_delete", args=(case.pk, poi.pk), query=origin
            ),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def poi_save(request, case_id, poi_id=None):
    latest_poi = None
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        initial = {}
        if poi_id:
            queryset = PointOfInterest.objects.filter(person__case=case)
            if request.method == "POST":
                queryset = queryset.select_for_update()
            poi = get_object_or_404(queryset, pk=poi_id)
        else:
            poi = PointOfInterest(created_by=request.user)
            if request.GET.get("person"):
                try:
                    initial["person"] = get_object_or_404(
                        Person, case=case, pk=request.GET["person"]
                    )
                except ValidationError as error:
                    raise Http404("Person not found.") from error
            # Query parameters only prefill; the bound form validates coordinates.
            initial.update(
                {
                    key: request.GET[key][:32]
                    for key in ("latitude", "longitude")
                    if key in request.GET
                }
            )
        origin, back_url, back_label = _navigation(case, request.GET, poi.person_id)
        if poi_id:
            back_url = reverse("geo:poi_detail", args=(case.pk, poi.pk), query=origin)
            back_label = "Back to POI"
        form = PointOfInterestForm(
            request.POST if request.method == "POST" else None,
            case=case,
            instance=poi,
            initial=initial,
        )
        if request.method == "POST" and form.is_valid():
            form.save(commit=False).save(
                force_insert=not poi_id, force_update=bool(poi_id)
            )
            messages.success(request, "POI details saved." if poi_id else "POI added.")
            return redirect(
                reverse("geo:poi_detail", args=(case.pk, poi.pk), query=origin)
            )
        if form.stale:
            latest_poi = PointOfInterest.objects.select_related("person").get(
                pk=poi.pk, person__case=case
            )
            form.refresh_revision()
    return render(
        request,
        "geo/poi_form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "poi": poi,
            "creating": not poi_id,
            "latest_poi": latest_poi,
            "has_people": case.people.exists(),
            "back_url": back_url,
            "back_label": back_label,
        },
        status=form.response_status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def poi_delete(request, case_id, poi_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        poi = get_object_or_404(
            PointOfInterest.objects.select_related("person"),
            person__case=case,
            pk=poi_id,
        )
        origin, return_url, _ = _navigation(case, request.GET, poi.person_id)
        back_url = reverse("geo:poi_detail", args=(case.pk, poi.pk), query=origin)
        form = PointOfInterestDeleteForm(
            request.POST if request.method == "POST" else None
        )
        if request.method == "POST" and form.is_valid():
            person_id = poi.person_id
            poi.delete()
            messages.success(request, "POI deleted.")
            if origin:
                return redirect(return_url)
            return redirect("people:detail", case_id=case.pk, person_id=person_id)
    return render(
        request,
        "geo/poi_delete.html",
        {
            **case_context(case, request.user),
            "poi": poi,
            "form": form,
            "back_url": back_url,
        },
        status=400 if request.method == "POST" else 200,
    )
