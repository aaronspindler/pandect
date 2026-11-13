"""Bounded possible trips from current, case-scoped vehicle observations."""

from collections import defaultdict
from itertools import pairwise

from django.db.models import F
from django.urls import reverse

from core.analysis.reporting import _base_metrics, _instant, _period, _state, _url
from core.evidence.queries import scoped_evidence
from geo.analysis.deterministic.locations import distance_m, normalize_address

GAP_SECONDS = 60 * 60
STOP_SECONDS = 5 * 60
STOP_RADIUS_M = 100


def _same_place(first, second):
    if first.latitude is not None and second.latitude is not None:
        return (
            distance_m(
                (first.latitude, first.longitude), (second.latitude, second.longitude)
            )
            <= STOP_RADIUS_M
        )
    first_address = normalize_address(first.address)["comparison_key"]
    second_address = normalize_address(second.address)["comparison_key"]
    if first_address and second_address:
        return first_address == second_address
    return None


def _segments(block):
    """Stops delimit candidates; unchanged or incomparable positions do not."""
    stationary = []
    for index, item in enumerate(block):
        if stationary and _same_place(block[stationary[-1][0]], item) is True:
            stationary[-1].append(index)
        else:
            stationary.append([index])
    start = 0
    for indexes in stationary:
        members = [block[index] for index in indexes]
        span = (members[-1].observed_at - members[0].observed_at).total_seconds()
        if span < STOP_SECONDS or any(
            item.speed_kmh is not None and item.speed_kmh > 0 for item in members
        ):
            continue
        candidate = block[start : indexes[0] + 1]
        if any(_same_place(candidate[0], item) is False for item in candidate):
            yield candidate
        start = indexes[-1]
    candidate = block[start:]
    if any(_same_place(candidate[0], item) is False for item in candidate):
        yield candidate


def _place(item):
    if item.address:
        return item.address
    return f"{item.latitude}, {item.longitude}"


def _source(case, item):
    return {
        "label": item.title,
        "place": _place(item),
        "time": item.observed_at,
        "url": _url(case, "vehicle-location", item),
        "row": item.source_row,
        "import_url": reverse(
            "geo:vehicle_import_review",
            kwargs={"case_id": case.pk, "import_id": item.import_batch_id},
        )
        if item.import_batch_id
        else "",
    }


def vehicle_trip_report(case, state, vehicle_id, start, end, limit):
    queryset = _state(scoped_evidence(case, "vehicle-location"), state)
    if vehicle_id:
        queryset = queryset.filter(vehicle_id=vehicle_id)
    missing_times = queryset.filter(observed_at__isnull=True).count()
    queryset = _period(queryset, "observed_at", start, end)
    available = queryset.count()
    records = list(
        queryset.select_related("vehicle").order_by(
            F("observed_at").desc(nulls_last=True), "pk"
        )[:limit]
    )
    records.sort(
        key=lambda item: (
            item.observed_at is None,
            _instant(item.observed_at),
            str(item.pk),
        )
    )
    metrics = _base_metrics(
        [("vehicle-location", item) for item in records],
        available,
        state,
        start,
        end,
        "Reported observation time",
    )
    warnings = []
    if available > len(records):
        warnings.append(
            f"Only the latest {len(records)} of {available} matching observations "
            "are included. Choose a vehicle or date range to inspect more trips."
        )
    if missing_times:
        warnings.append(
            f"{missing_times} observations without a recorded time cannot form trips."
        )
    tracks = defaultdict(list)
    for item in records:
        if item.observed_at is not None:
            tracks[item.vehicle_id].append(item)
    items, gaps = [], []
    conflicts = 0
    incomparable = 0
    for track in tracks.values():
        blocks = []
        for item in track:
            if not blocks:
                blocks.append([item])
                continue
            previous = blocks[-1][-1]
            seconds = (item.observed_at - previous.observed_at).total_seconds()
            same = _same_place(previous, item)
            if seconds > GAP_SECONDS:
                gaps.append(
                    {
                        "vehicle": str(item.vehicle),
                        "start": _source(case, previous),
                        "end": _source(case, item),
                        "minutes": round(seconds / 60, 1),
                    }
                )
                blocks.append([item])
            elif same is None or (seconds == 0 and same is False):
                conflicts += seconds == 0 and same is False
                incomparable += same is None
                blocks.append([item])
            else:
                blocks[-1].append(item)
        for block in blocks:
            for members in _segments(block):
                first, last = members[0], members[-1]
                seconds = (last.observed_at - first.observed_at).total_seconds()
                if seconds <= 0:
                    continue
                sources = [_source(case, item) for item in members]
                coordinate_based = all(item.latitude is not None for item in members)
                items.append(
                    {
                        "label": "Possible trip",
                        "text": "Movement suggested by the selected observations.",
                        "url": sources[0]["url"],
                        "vehicle": str(first.vehicle),
                        "vehicle_url": reverse(
                            "vehicle:detail",
                            kwargs={"case_id": case.pk, "vehicle_id": first.vehicle_id},
                        ),
                        "start": sources[0],
                        "end": sources[-1],
                        "minutes": round(seconds / 60, 1),
                        "basis": "Reported coordinates"
                        if coordinate_based
                        else "Reported addresses and available coordinates",
                        "sources": sources,
                        "related": sources,
                        "values": {
                            "Vehicle": str(first.vehicle),
                            "First observed (UTC)": _instant(first.observed_at),
                            "Last observed (UTC)": _instant(last.observed_at),
                            "Elapsed minutes": round(seconds / 60, 1),
                            "Observations": len(members),
                            "Largest sampling interval (minutes)": round(
                                max(
                                    (b.observed_at - a.observed_at).total_seconds()
                                    for a, b in pairwise(members)
                                )
                                / 60,
                                1,
                            ),
                        },
                    }
                )
    items.sort(key=lambda item: item["start"]["time"], reverse=True)
    gaps.sort(key=lambda gap: gap["start"]["time"], reverse=True)
    if conflicts:
        warnings.append(f"{conflicts} conflicting same-time positions split trips.")
    if incomparable:
        warnings.append(
            f"{incomparable} position changes cannot be compared and split trips."
        )
    metrics |= {
        "Possible trips": len(items),
        "Coverage gaps": len(gaps),
        "Vehicles in selection": len(tracks),
        "Missing observation time": missing_times,
    }
    return {
        "summary": f"{len(items)} possible trip{'s' if len(items) != 1 else ''}",
        "items": items,
        "gaps": gaps,
        "metrics": metrics,
        "warnings": warnings,
        "coverage": (
            f"Based on {len(records)} of {available} matching observations. "
            "Times are UTC."
        ),
    }
