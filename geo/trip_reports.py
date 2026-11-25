"""Shared, paginated presentation of complete processed vehicle trips."""

from django.core.paginator import Paginator
from django.db.models import Q

from core.evidence.queries import scoped_evidence
from geo.trip_processing import (
    current_generations,
    format_trips,
    saved_trips,
    trip_readiness,
)


def saved_trip_report(
    case,
    *,
    vehicle_id=None,
    start=None,
    end=None,
    page=None,
    page_size=25,
    gap_page=1,
    day_bounds=None,
):
    trips = saved_trips(case, vehicle_id=vehicle_id, start=start, end=end)
    jump_requested = day_bounds is not None and not page
    anchor = None
    if jump_requested:
        day_start, next_day = day_bounds
        if day_start < next_day:
            anchor = (
                trips.filter(start_at__lt=next_day, end_at__gte=day_start)
                .values("start_at", "pk")
                .first()
            )
        if anchor:
            preceding = trips.filter(
                Q(start_at__gt=anchor["start_at"])
                | Q(start_at=anchor["start_at"], pk__lt=anchor["pk"])
            ).count()
            page = preceding // page_size + 1
    page_obj = Paginator(trips, page_size).get_page(page)
    generations = current_generations(case, vehicle_id=vehicle_id)
    vehicle_count = 0
    observations = missing_times = conflicts = incomparable = 0
    for metrics in generations.values_list("metrics", flat=True):
        vehicle_count += 1
        counts = metrics.get("active", {})
        observations += counts.get("observations", 0)
        missing_times += counts.get("missing_times", 0)
        conflicts += counts.get("conflicts", 0)
        incomparable += counts.get("incomparable", 0)
    sources = scoped_evidence(case, "vehicle-location").filter(is_archived=False)
    if vehicle_id:
        sources = sources.filter(vehicle_id=vehicle_id)
    pending = sources.exclude(vehicle_id__in=generations.values("vehicle_id")).exists()
    readiness = trip_readiness(case, vehicle_id=vehicle_id)
    gaps = saved_trips(case, vehicle_id=vehicle_id, start=start, end=end, kind="gap")
    gap_page_obj = Paginator(gaps, 25).get_page(gap_page)
    warnings = []
    if pending:
        warnings.append(
            "Some vehicle trips are being prepared or need attention in Processing."
        )
    if missing_times:
        warnings.append(
            f"{missing_times} observations without a recorded time cannot form trips."
        )
    if conflicts:
        warnings.append(f"{conflicts} conflicting same-time positions split trips.")
    if incomparable:
        warnings.append(
            f"{incomparable} position changes cannot be compared and split trips."
        )
    return {
        "summary": (
            f"{page_obj.paginator.count} possible "
            f"trip{'s' if page_obj.paginator.count != 1 else ''}"
        ),
        "items": format_trips(case, page_obj),
        "page_obj": page_obj,
        "day_trip_id": str(anchor["pk"]) if anchor else "",
        "day_not_found": jump_requested and not anchor,
        "gaps": format_trips(case, gap_page_obj),
        "gap_page_obj": gap_page_obj,
        "warnings": warnings,
        "pending": pending,
        "processing_pending": readiness["pending"],
        "failed": readiness["failed"],
        "needs_prepare": readiness["needs_prepare"],
        "vehicle_id": vehicle_id,
        "coverage": (
            f"Processed from {observations} observations "
            "in complete vehicle histories. "
            "Times reflect reported observations."
        ),
        "metrics": {
            "Possible trips": page_obj.paginator.count,
            "Vehicles in selection": vehicle_count,
            "Matching records": observations,
            "Selected records": observations,
            "Missing observation time": missing_times,
            "Coverage gaps": gap_page_obj.paginator.count,
            "Truncated": False,
        },
    }
