"""Deterministic reports over retained vehicle-location observations."""

from collections import Counter, defaultdict
from statistics import median

from django.urls import reverse

from core.analysis.reporting import (
    _base_metrics,
    _instant,
    _period,
    _selection,
    _source,
    _state,
    _url,
)
from core.evidence.queries import scoped_evidence
from geo.vehicle_imports import row_fingerprint

GAP_SECONDS = 30 * 60
HEADINGS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def vehicle_report(case, state, vehicle_id, start, end, limit):
    queryset = _state(scoped_evidence(case, "vehicle-location"), state).select_related(
        "vehicle", "import_batch__document"
    )
    if vehicle_id:
        queryset = queryset.filter(vehicle_id=vehicle_id)
    missing_times = queryset.filter(observed_at__isnull=True).count()
    queryset = _period(queryset, "observed_at", start, end)
    rows, available, warnings = _selection([("vehicle-location", queryset)], limit)
    records = [item for _, item in rows]
    flags = defaultdict(list)
    related = defaultdict(list)
    intervals = {}
    coverage = defaultdict(list)
    source_order = defaultdict(list)
    duplicates = defaultdict(list)
    headings = Counter()
    direction_changes = 0
    gap_count = 0
    equal_count = 0
    out_of_order = 0
    known_speeds = []
    delays = []
    for item in records:
        if item.observed_at is not None:
            coverage[item.vehicle_id].append(item)
        if item.import_batch_id:
            source_order[(item.vehicle_id, item.import_batch_id)].append(item)
        if item.raw_row:
            duplicates[(item.vehicle_id, row_fingerprint(item.raw_row))].append(item)
        if item.speed_kmh is not None:
            known_speeds.append(float(item.speed_kmh))
        raw_heading = item.raw_row.get("Heading", "")
        heading = raw_heading.strip().upper() if isinstance(raw_heading, str) else ""
        headings[heading if heading in HEADINGS else "Unknown / unsupported"] += 1
        if item.observed_at is not None and item.received_at is not None:
            delay = (item.received_at - item.observed_at).total_seconds()
            delays.append(delay)
            if delay < 0:
                flags[item.pk].append(
                    "Negative receipt-minus-event delay; "
                    "check clock and source semantics."
                )
    for group in source_order.values():
        previous = None
        for item in sorted(group, key=lambda row: row.source_row):
            if item.observed_at is None:
                continue
            if previous is not None and item.observed_at < previous.observed_at:
                out_of_order += 1
                flags[item.pk].append(
                    "Event time precedes the preceding timed CSV row."
                )
                related[item.pk].append(previous)
            previous = item
    for group in coverage.values():
        previous = None
        previous_heading = None
        for item in sorted(group, key=lambda row: (row.observed_at, str(row.pk))):
            raw_heading = item.raw_row.get("Heading", "")
            heading = (
                raw_heading.strip().upper() if isinstance(raw_heading, str) else ""
            )
            if heading in HEADINGS:
                if previous_heading is not None and heading != previous_heading:
                    direction_changes += 1
                    flags[item.pk].append(
                        "Reported heading differs from the preceding known heading."
                    )
                previous_heading = heading
            if previous is not None:
                seconds = (item.observed_at - previous.observed_at).total_seconds()
                intervals[item.pk] = seconds
                related[item.pk].append(previous)
                if seconds == 0:
                    equal_count += 1
                    flags[item.pk].append(
                        "Same event timestamp as another selected observation."
                    )
                elif seconds >= GAP_SECONDS:
                    gap_count += 1
                    flags[item.pk].append(
                        "At least 30 minutes between selected observations; "
                        "movement is unknown in between."
                    )
            previous = item
    duplicate_groups = [group for group in duplicates.values() if len(group) > 1]
    for group in duplicate_groups:
        for item in group:
            flags[item.pk].append(
                "Identical retained CSV row appears more than once for this vehicle."
            )
            related[item.pk].extend(other for other in group if other.pk != item.pk)
    items = []
    for item in records:
        delay = (
            (item.received_at - item.observed_at).total_seconds()
            if item.received_at is not None and item.observed_at is not None
            else "Unknown"
        )
        entry = _source(
            case,
            "vehicle-location",
            item,
            {
                "Vehicle": str(item.vehicle),
                "Vehicle ID": str(item.vehicle_id),
                "Address": item.address or "Not supplied",
                "Observed at (UTC)": _instant(item.observed_at),
                "Received at (UTC)": _instant(item.received_at),
                "Receipt minus event (seconds)": delay,
                "Reported speed (km/h)": float(item.speed_kmh)
                if item.speed_kmh is not None
                else "Unknown",
                "Raw heading": item.raw_row.get("Heading", "") or "Not supplied",
                "Raw trigger": item.raw_row.get("Trigger", "") or "Not supplied",
                "Raw time summary": item.raw_row.get("Time Summary", "")
                or "Not supplied",
                "Interval since preceding timed observation (seconds)": intervals.get(
                    item.pk, "Unknown"
                ),
                "CSV row": item.source_row
                if item.source_row is not None
                else "Not imported",
            },
            text=" ".join(flags[item.pk]),
            kind_label="Vehicle location",
        )
        links = {
            str(other.pk): {
                "label": other.title,
                "url": _url(case, "vehicle-location", other),
            }
            for other in related[item.pk]
        }
        if item.import_batch_id:
            links["document"] = {
                "label": "Retained source CSV",
                "url": _url(case, "document", item.import_batch.document),
            }
            links["import"] = {
                "label": f"Import interpretation · row {item.source_row}",
                "url": reverse(
                    "geo:vehicle_import_review",
                    kwargs={"case_id": case.pk, "import_id": item.import_batch_id},
                ),
            }
        entry["related"] = list(links.values())
        items.append(entry)
    metrics = _base_metrics(
        rows, available, state, start, end, "Reported observation time"
    )
    timed = [item.observed_at for item in records if item.observed_at is not None]
    metrics.update(
        {
            "Vehicles in selection": len({item.vehicle_id for item in records}),
            "Observed window start (UTC)": _instant(min(timed) if timed else None),
            "Observed window end (UTC)": _instant(max(timed) if timed else None),
            "Missing observation time in selection": len(records) - len(timed),
            "Missing observation time before period filter": missing_times,
            "Intervals of at least 30 minutes": gap_count,
            "Equal-time pairs": equal_count,
            "Out-of-order CSV rows": out_of_order,
            "Duplicate raw-row groups": len(duplicate_groups),
            "Known receipt delays": len(delays),
            "Unknown receipt delays": len(records) - len(delays),
            "Negative receipt delays": sum(delay < 0 for delay in delays),
            "Receipt delay minimum (seconds)": min(delays) if delays else "Unknown",
            "Receipt delay maximum (seconds)": max(delays) if delays else "Unknown",
            "Known reported speeds": len(known_speeds),
            "Unknown reported speeds": len(records) - len(known_speeds),
            "Zero reported speeds": sum(speed == 0 for speed in known_speeds),
            "Reported speed minimum (km/h)": min(known_speeds)
            if known_speeds
            else "Unknown",
            "Reported speed maximum (km/h)": max(known_speeds)
            if known_speeds
            else "Unknown",
            "Reported speed median (km/h)": median(known_speeds)
            if known_speeds
            else "Unknown",
            "Reported heading counts": "; ".join(
                f"{heading}: {count}" for heading, count in sorted(headings.items())
            )
            or "No observations",
            "Changes between known reported headings": direction_changes,
        }
    )
    limitations = [
        "An observation gap means unavailable reporting, not inactivity. The "
        "30-minute threshold describes selected observations only; it does "
        "not infer visits, parking, engine state, or a driver.",
        "Speeds are retained telemetry, not distance/time estimates or "
        "speeding findings. Trigger and Time Summary remain uninterpreted. "
        "Receipt, observation, collection, and upload times "
        "have different meanings.",
        "CSV ordering checks compare timed rows within each selected import "
        "and vehicle. No source order is asserted for manual observations.",
    ]
    if (start is not None or end is not None) and missing_times:
        warnings.append(
            "Observations without an event time are excluded by the selected period."
        )
    return {
        "summary": (
            f"{len(records)} selected vehicle observations. Each source row "
            "and available coverage interval remains inspectable."
        ),
        "metrics": metrics,
        "items": items,
        "warnings": warnings,
        "limitations": limitations,
    }
