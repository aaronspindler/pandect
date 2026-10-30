"""Read-only map of retained, same-case location observations."""

import hashlib

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, CharField, Q, Value, When
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from config.access import product_login_required, wants_json
from evidence.queries import scoped_evidence
from evidence.types import KIND_LABELS, LOCATION_KINDS

from .map_geocoding import (
    address_estimates,
    latest_local_run,
    local_geocoder_ready,
    queue_addresses,
)
from .permissions import case_context, get_case


def map_page(request, case):
    kind = request.GET.get("kind", "")
    if kind not in LOCATION_KINDS:
        kind = ""
    archive = request.GET.get("archive", "active")
    if archive not in ("active", "archived", "all"):
        archive = "active"
    query = request.GET.get("q", "")[:200].strip()
    queries = []
    for location_kind in LOCATION_KINDS:
        if kind and location_kind != kind:
            continue
        queryset = scoped_evidence(case, location_kind).order_by()
        if archive != "all":
            queryset = queryset.filter(is_archived=archive == "archived")
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query)
                | Q(address__icontains=query)
                | Q(source__icontains=query)
            )
        queries.append(
            queryset.annotate(
                kind=Value(location_kind, output_field=CharField()),
                geocode_run_id=Case(
                    When(
                        latitude__isnull=True,
                        then=latest_local_run(case, location_kind),
                    )
                ),
            ).values(
                "id",
                "kind",
                "title",
                "address",
                "latitude",
                "longitude",
                "observed_at",
                "source",
                "is_archived",
                "created_at",
                "sha256",
                "geocode_run_id",
                "edited_at",
            )
        )
    records = (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )
    page = Paginator(records, 100).get_page(request.GET.get("page"))
    for index, item in enumerate(page, start=page.start_index()):
        item["number"] = index
        item["kind_label"] = KIND_LABELS[item["kind"]]
        item["url"] = reverse(
            "evidence:detail", args=(case.pk, item["kind"], item["id"])
        )
        # Web Mercator cannot display the poles. Preserve their exact values in
        # the list rather than silently moving a reported position onto the map.
        item["plottable"] = (
            item["latitude"] is not None and abs(item["latitude"]) <= 85.05112878
        )
    return page, kind, archive, query


def page_key(page):
    """Do not attach asynchronous updates to a changed list of source records."""
    identity = "\n".join(
        f"{item['number']}:{item['kind']}:{item['id']}:{item['sha256']}:"
        f"{item['edited_at'].isoformat()}:{item['is_archived']}"
        for item in page
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def map_data(case, page):
    address_estimates(case, page)
    points = []
    locations = []
    for item in page:
        if item["plottable"]:
            points.append(
                {
                    "number": item["number"],
                    "latitude": float(item["latitude"]),
                    "longitude": float(item["longitude"]),
                    "title": item["title"],
                    "url": item["url"],
                }
            )
        state = item.get("geocode", {})
        points.extend(state.get("points", []))
        item["plottable"] = item["plottable"] or bool(state.get("points"))
        locations.append(
            {
                "number": item["number"],
                "plottable": item["plottable"],
                "label": state.get("label", ""),
                "url": state.get("url", ""),
            }
        )
    return {
        "page_key": page_key(page),
        "points": points,
        "locations": locations,
        "mapped_count": sum(item["plottable"] for item in page),
        "unmapped_count": sum(not item["plottable"] for item in page),
        "pending": any(item.get("geocode", {}).get("pending") for item in page),
        "new_addresses": any(item.get("geocode", {}).get("new") for item in page),
    }


@product_login_required
@require_GET
def case_map(request, case_id):
    case = get_case(request.user, case_id)
    page, kind, archive, query = map_page(request, case)
    data = map_data(case, page)
    context = case_context(case, request.user)
    local_ready = local_geocoder_ready()
    data["can_queue"] = context["can_edit"] and local_ready and data["new_addresses"]
    if wants_json(request):
        return JsonResponse(data)
    return render(
        request,
        "cases/map.html",
        {
            **context,
            **data,
            "page_obj": page,
            "local_geocoder_ready": local_ready,
            "has_addresses": any(item["latitude"] is None for item in page),
            "kind": kind,
            "archive": archive,
            "query": query,
            "location_kinds": [(key, KIND_LABELS[key]) for key in LOCATION_KINDS],
            "tile_url": settings.PANDECT_MAP_TILE_URL,
            "tile_attribution": settings.PANDECT_MAP_TILE_ATTRIBUTION,
        },
    )


@product_login_required
@require_POST
@transaction.atomic
def locate_map_addresses(request, case_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
    if not local_geocoder_ready():
        if wants_json(request):
            return JsonResponse(
                {
                    "message": (
                        "Configure an operator-controlled local geocoder "
                        "to locate addresses."
                    )
                },
                status=400,
            )
        messages.error(
            request, "Local address lookup is not configured. No address was sent."
        )
    else:
        page, *_ = map_page(request, case)
        if request.POST.get("page_key") != page_key(page):
            if wants_json(request):
                return JsonResponse(
                    {
                        "message": (
                            "Locations changed. Reload the map "
                            "before looking up addresses."
                        )
                    },
                    status=409,
                )
            messages.error(
                request, "Locations changed. Review the current map and try again."
            )
            url = reverse("cases:case_map", args=(case.pk,))
            return redirect(
                url + (f"?{request.GET.urlencode()}" if request.GET else "")
            )
        address_estimates(case, page)
        run = queue_addresses(
            request.user, case, page, retry=request.POST.get("retry") == "1"
        )
        if wants_json(request):
            return JsonResponse({"queued": bool(run)})
        messages.success(
            request,
            "Local address lookup queued. Refresh the map to see the estimates."
            if run
            else "No new address lookups are needed.",
        )
    url = reverse("cases:case_map", args=(case.pk,))
    return redirect(url + (f"?{request.GET.urlencode()}" if request.GET else ""))
