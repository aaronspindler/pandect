"""Person-linked map annotations using current case permissions."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from geo.models import PointOfInterest
from geo.poi_forms import PointOfInterestDeleteForm, PointOfInterestForm, poi_revision
from people.models import Person


@product_login_required
@require_safe
@never_cache
def poi_detail(request, case_id, poi_id):
    case = get_case(request.user, case_id)
    poi = get_object_or_404(
        PointOfInterest.objects.select_related("person", "created_by"),
        person__case=case,
        pk=poi_id,
    )
    return render(
        request,
        "geo/poi_detail.html",
        {**case_context(case, request.user), "poi": poi},
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
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
            return redirect("geo:poi_detail", case_id=case.pk, poi_id=poi.pk)
        if form.stale:
            latest_poi = PointOfInterest.objects.select_related("person").get(
                pk=poi.pk, person__case=case
            )
            form.data = form.data.copy()
            form.data["revision"] = poi_revision(latest_poi)
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
        },
        status=409 if form.stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def poi_delete(request, case_id, poi_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        poi = get_object_or_404(
            PointOfInterest.objects.select_related("person"),
            person__case=case,
            pk=poi_id,
        )
        form = PointOfInterestDeleteForm(
            request.POST if request.method == "POST" else None
        )
        if request.method == "POST" and form.is_valid():
            person_id = poi.person_id
            poi.delete()
            messages.success(request, "POI deleted.")
            return redirect("people:detail", case_id=case.pk, person_id=person_id)
    return render(
        request,
        "geo/poi_delete.html",
        {**case_context(case, request.user), "poi": poi, "form": form},
        status=400 if request.method == "POST" else 200,
    )
