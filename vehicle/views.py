from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Q
from django.db.models.deletion import RestrictedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe
from django.views.decorators.vary import vary_on_headers

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.reports import report
from core.evidence.queries import same_case_evidence
from geo.models import VehicleLocationImport
from ui.dialogs import form_saved, render_quick_form, render_record, wants_record
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
    return render_live(
        request,
        "vehicle/list.html",
        {
            **case_context(case, request.user),
            "page_obj": page,
            "query": query,
            "search_query": urlencode({"q": query}) if query else "",
            "list_query": list_query(request),
        },
        collection=True,
    )


@product_login_required
@require_safe
@vary_on_headers("X-Pandect-Form")
def vehicle_detail(request, case_id, vehicle_id):
    case = get_case(request.user, case_id)
    vehicle = get_object_or_404(Vehicle, case=case, pk=vehicle_id)
    context = case_context(case, request.user) | {"vehicle": vehicle}
    if wants_record(request):
        return render_record(request, "vehicle/detail.html", context)
    trip_report = report(case, "vehicle-trips", vehicle_id=vehicle.pk, page_size=5)
    observations = same_case_evidence(
        vehicle.location_evidence.filter(case=case)
    ).order_by(F("observed_at").desc(nulls_last=True), "-created_at", "pk")
    return render_live(
        request,
        "vehicle/detail.html",
        {
            **context,
            "trip_report": trip_report,
            "more_trips": trip_report["page_obj"].has_next(),
            "latest_observation": observations.filter(
                observed_at__isnull=False
            ).first(),
            "locations": Paginator(observations, 25).get_page(
                request.GET.get("locations_page")
            ),
            "list_query": list_query(request),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_create(request, case_id, import_id=None):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=request.method == "POST")
        review_url = ""
        if import_id is not None:
            batch = get_object_or_404(
                VehicleLocationImport, pk=import_id, case=case, document__case=case
            )
            review_url = reverse("geo:vehicle_import_review", args=[case.pk, batch.pk])
            if batch.status != "review" or batch.completed_at or batch.invalid_count:
                return redirect(review_url)
            review_url += "#mapping-heading"
        vehicle = Vehicle(case=case)
        form = VehicleForm(
            request.POST if request.method == "POST" else None, instance=vehicle
        )
        if request.method == "POST" and form.is_valid():
            vehicle = form.save(commit=False)
            vehicle.case = case
            vehicle.save(force_insert=True)
            if review_url:
                messages.success(request, "Vehicle added.")
                return redirect(review_url)
            url = reverse(
                "vehicle:detail", kwargs={"case_id": case.pk, "vehicle_id": vehicle.pk}
            )
            if query := list_query(request):
                url += f"?{query}"
            return form_saved(request, "Vehicle added.", url)
    return render_quick_form(
        request,
        "vehicle/form.html",
        {
            **case_context(case, request.user),
            "form": form,
            "creating": True,
            "review_url": review_url,
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
            url = reverse(
                "vehicle:detail", kwargs={"case_id": case.pk, "vehicle_id": vehicle.pk}
            )
            if query := list_query(request):
                url += f"?{query}"
            return form_saved(request, "Vehicle details saved.", url)
        if form.stale:
            latest_vehicle = Vehicle.objects.get(pk=vehicle.pk, case=case)
            form.data = form.data.copy()
            form.data["revision"] = form.initial["revision"]
    return render_quick_form(
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
        locations = list(
            same_case_evidence(vehicle.location_evidence.filter(case=case))
            .order_by("-observed_at", "pk")
            .values("pk", "title", "is_archived")[:5]
        )
        imports = list(
            vehicle.location_imports.filter(case=case, document__case=case)
            .order_by("-created_at", "pk")
            .values("pk", "document__original_filename")[:5]
        )
        form = VehicleDeleteForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and not (locations or imports) and form.is_valid():
            try:
                vehicle.delete()
            except RestrictedError:
                form.add_error(
                    None,
                    "This vehicle is still linked to retained location evidence "
                    "or a retained location import.",
                )
            else:
                messages.success(request, "Vehicle record deleted.")
                url = reverse("vehicle:list", kwargs={"case_id": case.pk})
                if query := list_query(request):
                    url += f"?{query}"
                return redirect(url)
    return render(
        request,
        "vehicle/delete.html",
        {
            **case_context(case, request.user),
            "vehicle": vehicle,
            "form": form,
            "locations": locations,
            "imports": imports,
            "list_query": list_query(request),
        },
        status=400 if request.method == "POST" else 200,
    )
