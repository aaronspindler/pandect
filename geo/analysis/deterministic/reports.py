"""Complete vehicle reports streamed in bounded batches, with paginated sources."""

from collections import Counter, defaultdict
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Window
from django.db.models.functions import RowNumber
from django.urls import reverse

from core.analysis.reporting import (
    BATCH_SIZE,
    _base_metrics,
    _instant,
    _median,
    _period,
    _record_page,
    _source,
    _url,
)
from core.evidence.queries import scoped_evidence

GAP_SECONDS = 30 * 60
HEADINGS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}


def vehicle_report(case, vehicle_id, start, end, page, page_size, duplicate_row=None):
    queryset = (
        scoped_evidence(case, "vehicle-location")
        .filter(is_archived=False)
        .select_related("vehicle", "import_batch__document")
    )
    if vehicle_id:
        queryset = queryset.filter(vehicle_id=vehicle_id)
    missing_times = queryset.filter(observed_at__isnull=True).count()
    queryset = _period(queryset, "observed_at", start, end)
    if duplicate_row:
        from django.http import Http404

        try:
            duplicate = (
                scoped_evidence(case, "vehicle-location")
                .filter(is_archived=False)
                .select_related(None)
                .only("vehicle_id", "raw_row")
                .get(pk=duplicate_row)
            )
        except queryset.model.DoesNotExist, ValueError, ValidationError:
            raise Http404("Source row not found.") from None
        queryset = queryset.filter(
            vehicle_id=duplicate.vehicle_id, raw_row=duplicate.raw_row
        )
    page_obj, rows = _record_page(
        [("vehicle-location", queryset)], page, page_size, time_field="observed_at"
    )
    total = page_obj.paginator.count
    records = [item for _, item in rows]
    page_ids = {item.pk for item in records}
    flags = defaultdict(list)
    related = defaultdict(dict)
    intervals = {}
    duplicate_counts = {}
    headings = Counter()
    direction_changes = gap_count = equal_count = out_of_order = duplicate_groups = 0
    known_speeds = zero_speeds = known_delays = negative_delays = missing = vehicles = 0
    speed_min = speed_max = delay_min = delay_max = window_start = window_end = None
    previous = previous_heading = current_vehicle = None
    annotated = queryset.annotate(
        raw_count=Window(Count("pk"), partition_by=[F("vehicle_id"), F("raw_row")]),
        raw_rank=Window(
            RowNumber(),
            partition_by=[F("vehicle_id"), F("raw_row")],
            order_by=[F("pk").asc()],
        ),
    )
    fields = (
        "pk",
        "title",
        "vehicle_id",
        "observed_at",
        "received_at",
        "speed_kmh",
        "raw_row",
        "raw_count",
        "raw_rank",
    )
    for row in (
        annotated.order_by("vehicle_id", "observed_at", "pk")
        .values(*fields)
        .iterator(chunk_size=BATCH_SIZE)
    ):
        pk = row["pk"]
        if row["vehicle_id"] != current_vehicle:
            vehicles += 1
            current_vehicle = row["vehicle_id"]
            previous = previous_heading = None
        raw = row["raw_row"]
        raw_heading = raw.get("Heading", "")
        heading = raw_heading.strip().upper() if isinstance(raw_heading, str) else ""
        headings[heading if heading in HEADINGS else "Unknown / unsupported"] += 1
        speed = row["speed_kmh"]
        if speed is not None:
            known_speeds += 1
            zero_speeds += speed == 0
            speed_min = speed if speed_min is None else min(speed_min, speed)
            speed_max = speed if speed_max is None else max(speed_max, speed)
        at = row["observed_at"]
        received = row["received_at"]
        if at is not None and received is not None:
            delay = (received - at).total_seconds()
            known_delays += 1
            delay_min = delay if delay_min is None else min(delay_min, delay)
            delay_max = delay if delay_max is None else max(delay_max, delay)
            if delay < 0:
                negative_delays += 1
                if pk in page_ids:
                    flags[pk].append(
                        "Negative receipt-minus-event delay; "
                        "check clock and source semantics."
                    )
        if raw and row["raw_count"] > 1:
            duplicate_groups += row["raw_rank"] == 1
            if pk in page_ids:
                duplicate_counts[pk] = row["raw_count"]
                flags[pk].append(
                    "Identical retained CSV row appears more than once "
                    "for this vehicle."
                )
        if at is None:
            missing += 1
            continue
        window_start = at if window_start is None else min(window_start, at)
        window_end = at if window_end is None else max(window_end, at)
        if heading in HEADINGS:
            if previous_heading is not None and heading != previous_heading:
                direction_changes += 1
                if pk in page_ids:
                    flags[pk].append(
                        "Reported heading differs from the preceding known heading."
                    )
            previous_heading = heading
        if previous is not None:
            seconds = (at - previous["observed_at"]).total_seconds()
            equal_count += seconds == 0
            gap_count += seconds >= GAP_SECONDS
            if pk in page_ids:
                intervals[pk] = seconds
                related[pk][previous["pk"]] = previous["title"]
                if seconds == 0:
                    flags[pk].append(
                        "Same event timestamp as another selected observation."
                    )
                elif seconds >= GAP_SECONDS:
                    flags[pk].append(
                        "At least 30 minutes between selected observations; "
                        "movement is unknown in between."
                    )
        previous = row
    previous = current_group = None
    ordered = queryset.filter(
        import_batch__isnull=False, observed_at__isnull=False
    ).order_by("vehicle_id", "import_batch_id", "source_row", "pk")
    for row in ordered.values(
        "pk", "title", "vehicle_id", "import_batch_id", "observed_at"
    ).iterator(chunk_size=BATCH_SIZE):
        group = (row["vehicle_id"], row["import_batch_id"])
        if group != current_group:
            current_group, previous = group, None
        if previous is not None and row["observed_at"] < previous["observed_at"]:
            out_of_order += 1
            if row["pk"] in page_ids:
                flags[row["pk"]].append(
                    "Event time precedes the preceding timed CSV row."
                )
                related[row["pk"]][previous["pk"]] = previous["title"]
        previous = row
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
        entry["related"] = [
            {
                "label": title,
                "url": reverse(
                    "core:evidence:detail", args=(case.pk, "vehicle-location", pk)
                ),
            }
            for pk, title in related[item.pk].items()
        ]
        if item.pk in duplicate_counts:
            filters = {"duplicate_row": item.pk}
            filters.update(
                {
                    key: value.isoformat()
                    for key, value in (("start", start), ("end", end))
                    if value is not None
                }
            )
            entry["related"].append(
                {
                    "label": (
                        f"Inspect all {duplicate_counts[item.pk]} matching CSV rows"
                    ),
                    "url": reverse(
                        "core:analysis:report", args=(case.pk, "vehicle-coverage")
                    )
                    + "?"
                    + urlencode(filters),
                }
            )
        if item.import_batch_id:
            entry["related"].extend(
                [
                    {
                        "label": "Retained source CSV",
                        "url": _url(case, "document", item.import_batch.document),
                    },
                    {
                        "label": f"Import interpretation · row {item.source_row}",
                        "url": reverse(
                            "geo:vehicle_import_review",
                            kwargs={
                                "case_id": case.pk,
                                "import_id": item.import_batch_id,
                            },
                        ),
                    },
                ]
            )
        items.append(entry)
    median_speed = _median([queryset], "speed_kmh")
    metrics = _base_metrics(total, start, end, "Reported observation time")
    metrics.update(
        {
            "Vehicles in selection": vehicles,
            "Observed window start (UTC)": _instant(window_start),
            "Observed window end (UTC)": _instant(window_end),
            "Missing observation time in selection": missing,
            "Missing observation time before period filter": missing_times,
            "Intervals of at least 30 minutes": gap_count,
            "Equal-time pairs": equal_count,
            "Out-of-order CSV rows": out_of_order,
            "Duplicate raw-row groups": duplicate_groups,
            "Known receipt delays": known_delays,
            "Unknown receipt delays": total - known_delays,
            "Negative receipt delays": negative_delays,
            "Receipt delay minimum (seconds)": delay_min
            if delay_min is not None
            else "Unknown",
            "Receipt delay maximum (seconds)": delay_max
            if delay_max is not None
            else "Unknown",
            "Known reported speeds": known_speeds,
            "Unknown reported speeds": total - known_speeds,
            "Zero reported speeds": zero_speeds,
            "Reported speed minimum (km/h)": float(speed_min)
            if speed_min is not None
            else "Unknown",
            "Reported speed maximum (km/h)": float(speed_max)
            if speed_max is not None
            else "Unknown",
            "Reported speed median (km/h)": float(median_speed)
            if median_speed is not None
            else "Unknown",
            "Reported heading counts": "; ".join(
                f"{heading}: {count}" for heading, count in sorted(headings.items())
            )
            or "No observations",
            "Changes between known reported headings": direction_changes,
        }
    )
    warnings = []
    if (start is not None or end is not None) and missing_times:
        warnings.append(
            "Observations without an event time are excluded by the selected period."
        )
    return {
        "summary": (
            f"{total} selected vehicle observations. Each source row "
            "and available coverage interval remains inspectable."
        ),
        "metrics": metrics,
        "items": items,
        "page_obj": page_obj,
        "warnings": warnings,
        "limitations": [
            "An observation gap means unavailable reporting, not inactivity. The "
            "30-minute threshold describes selected observations only; it does "
            "not infer visits, parking, engine state, or a driver.",
            "Speeds are retained telemetry, not distance/time estimates or "
            "speeding findings. Trigger and Time Summary remain uninterpreted. "
            "Receipt, observation, collection, and upload times "
            "have different meanings.",
            "CSV ordering checks compare timed rows within each selected import "
            "and vehicle. No source order is asserted for manual observations.",
        ],
    }
