"""Keep case map and trip navigation scoped to current records and filters."""

from uuid import UUID

from django.http import Http404

from geo.trip_models import VehicleTrip
from geo.trip_processing import saved_trips


def current_map_trip(case, trip_id):
    state = (
        VehicleTrip.objects.filter(case=case, vehicle__case=case, pk=trip_id)
        .values_list("evidence_state", flat=True)
        .first()
    )
    if not state:
        raise Http404("Trip not found.")
    try:
        return saved_trips(case, state=state).get(pk=trip_id)
    except VehicleTrip.DoesNotExist as error:
        raise Http404(
            "This trip changed. Open the map to see the current trips."
        ) from error


def poi_list_query(values):
    params = {}
    if query := values.get("q", "").strip()[:200]:
        params["q"] = query
    for key in ("page", "poi_page"):
        page = values.get(key, "")
        if page.isascii() and page.isdecimal() and len(page) <= 9:
            params[key] = page
    return params


def map_query(values):
    params = poi_list_query(values)
    if (state := values.get("state")) in {"active", "archived", "all"}:
        params["state"] = state
    vehicle_id = values.get("vehicle_id", "")
    if len(vehicle_id) == 36:
        try:
            params["vehicle_id"] = str(UUID(vehicle_id))
        except ValueError:
            pass
    for key in ("start", "end"):
        value = values.get(key, "")
        if 0 < len(value) <= 64:
            params[key] = value
    return params


def trip_query(values):
    params = map_query(values)
    for key in ("sources_page", "findings_page"):
        page = values.get(key, "")
        if page.isascii() and page.isdecimal() and len(page) <= 9:
            params[key] = page
    if values.get("view") == "estimates":
        params["view"] = "estimates"
    return params
