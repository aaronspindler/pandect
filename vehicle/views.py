from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import RestrictedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.reports import report
from core.evidence.queries import same_case_evidence
from ui.live import render_live
from vehicle.forms import VehicleDeleteForm, VehicleForm
from vehicle.models import Vehicle


def list_query(request):
    values = {}
    query = request.GET.get("q", "").strip()[:200]
    if query:
        values["q"] = query
    if request.GET.get("page", "").isdigit():
        values["page"] = request.GET["page"]
    return urlencode(values)


@product_login_required
@require_safe
def vehicle_list(request, case_id):
    case = get_case(request.user, case_id)
    query = request.GET.get("q", "").strip()[:200]
    vehicles = Vehicle.objects.filter(case=case)
    if query:
        search = (
            Q(make__icontains=query)
            | Q(model__icontains=query)
            | Q(license_plate__icontains=query)
            | Q(vin__icontains=query)
            | Q(color__icontains=query)
            | Q(notes__icontains=query)
        )
        if query.isascii() and query.isdigit() and 1 <= int(query) <= 9999:
            search |= Q(year=int(query))
        vehicles = vehicles.filter(search)
    page = Paginator(vehicles, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "vehicle/list.html",
        {
            **case_context(case, request.user),
            "page_obj": page,
            "query": query,
            "search_query": urlencode({"q": query}) if query else "",
            "list_query": list_query(request),
        },
    )


@product_login_required
@require_safe
def vehicle_detail(request, case_id, vehicle_id):
    case = get_case(request.user, case_id)
    vehicle = get_object_or_404(Vehicle, case=case, pk=vehicle_id)
    trip_report = report(case, "vehicle-trips", vehicle_id=vehicle.pk, page_size=5)
    more_trips = trip_report["page_obj"].paginator.count > 5
    return render_live(
        request,
        "vehicle/detail.html",
        {
            **case_context(case, request.user),
            "vehicle": vehicle,
            "trip_report": trip_report,
            "more_trips": more_trips,
            "locations": Paginator(
                same_case_evidence(
                    vehicle.location_evidence.filter(case=case)
                ).order_by("-observed_at", "-created_at", "pk"),
                25,
            ).get_page(request.GET.get("locations_page")),
            "list_query": list_query(request),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_create(request, case_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        vehicle = Vehicle(case=case)
        form = VehicleForm(
            request.POST if request.method == "POST" else None, instance=vehicle
        )
        if request.method == "POST" and form.is_valid():
            vehicle = form.save(commit=False)
            vehicle.case = case
            vehicle.save(force_insert=True)
            messages.success(request, "Vehicle added.")
            return redirect("vehicle:detail", case_id=case.pk, vehicle_id=vehicle.pk)
    return render(
        request,
        "vehicle/form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "creating": True,
            "list_query": list_query(request),
        },
        status=400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_edit(request, case_id, vehicle_id):
    latest_vehicle = None
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        vehicles = Vehicle.objects.filter(case=case)
        if request.method == "POST":
            vehicles = vehicles.select_for_update()
        vehicle = get_object_or_404(vehicles, pk=vehicle_id)
        form = VehicleForm(
            request.POST if request.method == "POST" else None, instance=vehicle
        )
        if request.method == "POST" and form.is_valid():
            form.save(commit=False).save(force_update=True)
            messages.success(request, "Vehicle details saved.")
            return redirect("vehicle:detail", case_id=case.pk, vehicle_id=vehicle.pk)
        if form.stale:
            latest_vehicle = Vehicle.objects.get(pk=vehicle.pk, case=case)
            form.data = form.data.copy()
            form.data["revision"] = form.initial["revision"]
    return render(
        request,
        "vehicle/form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "vehicle": vehicle,
            "latest_vehicle": latest_vehicle,
            "creating": False,
            "list_query": list_query(request),
        },
        status=409 if form.stale else 400 if request.method == "POST" else 200,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_delete(request, case_id, vehicle_id):
    with transaction.atomic():
        case = get_case(
            request.user, case_id, owner=True, lock=request.method == "POST"
        )
        vehicles = Vehicle.objects.filter(case=case)
        if request.method == "POST":
            vehicles = vehicles.select_for_update()
        vehicle = get_object_or_404(vehicles, pk=vehicle_id)
        form = VehicleDeleteForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                vehicle.delete()
            except RestrictedError:
                form.add_error(
                    None,
                    "This vehicle is linked to retained location evidence and cannot "
                    "be deleted individually. Archived evidence also retains its link.",
                )
            else:
                messages.success(request, "Vehicle record deleted.")
                return redirect("vehicle:list", case_id=case.pk)
    return render(
        request,
        "vehicle/delete.html",
        {
            **case_context(case, request.user),
            "vehicle": vehicle,
            "form": form,
        },
        status=400 if request.method == "POST" else 200,
    )
