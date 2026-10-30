"""Bounded reports over retained fields, without storing new conclusions.

The caller authorizes the case. All subordinate selections are checked here;
the shared evidence query also rejects inconsistent cross-case relationships.
"""

from collections import Counter, defaultdict
from datetime import UTC
from statistics import median

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from evidence.models import CallEvidenceAttachment
from evidence.queries import scoped_evidence
from evidence.types import CALL_KINDS, FILE_KINDS, KIND_LABELS
from evidence.vehicle_imports import row_fingerprint
from people.models import Person
from vehicles.models import Vehicle

MAX_RECORDS = 500
GAP_SECONDS = 30 * 60
HEADINGS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
REPORT_NAMES = {
    "calls": "calls",
    "vehicle-coverage": "vehicle",
    "vehicle-speed": "vehicle",
    "duplicates": "duplicates",
}


def _url(case, kind, item):
    return reverse(
        "evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def _instant(value):
    return value.astimezone(UTC).isoformat() if value is not None else "Unknown"


def _state(queryset, state):
    if state == "all":
        return queryset
    return queryset.filter(is_archived=state == "archived")


def _period(queryset, field, start, end):
    if start is not None:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end is not None:
        queryset = queryset.filter(**{f"{field}__lte": end})
    return queryset


def _selection(querysets, limit):
    """Bound materialization across the small explicit set of source tables."""
    available = 0
    rows = []
    for kind, queryset in querysets:
        available += queryset.count()
        rows.extend(
            (kind, item) for item in queryset.order_by("created_at", "pk")[:limit]
        )
    rows.sort(key=lambda row: (row[1].created_at, row[0], str(row[1].pk)))
    rows = rows[:limit]
    warnings = []
    if available > len(rows):
        warnings.append(
            f"Only the first {len(rows)} of {available} matching records by upload "
            "time are included. Narrow the selection to inspect the rest. "
            "Counts and findings describe this bounded selection."
        )
    return rows, available, warnings


def _base_metrics(rows, available, state, start, end, time_field):
    return {
        "Matching records": available,
        "Selected records": len(rows),
        "Truncated": available > len(rows),
        "Archive filter": state,
        "Period field": time_field,
        "Period start (UTC, inclusive)": _instant(start)
        if start
        else "Not constrained",
        "Period end (UTC, inclusive)": _instant(end) if end else "Not constrained",
    }


def _source(case, kind, item, values, *, text=""):
    return {
        "label": f"{KIND_LABELS[kind]} · {item.title}",
        "text": text,
        "url": _url(case, kind, item),
        "values": {
            "Archived": item.is_archived,
            "Collected at (UTC)": _instant(item.collected_at),
            "Uploaded at (UTC)": _instant(item.created_at),
            **values,
        },
    }


def _calls(case, state, participant_id, other_participant_id, start, end, limit):
    queries = []
    missing_times = 0
    for kind in CALL_KINDS:
        queryset = _state(scoped_evidence(case, kind), state).select_related(
            "sender", "recipient"
        )
        if other_participant_id:
            queryset = queryset.filter(
                Q(sender_id=participant_id, recipient_id=other_participant_id)
                | Q(sender_id=other_participant_id, recipient_id=participant_id)
            )
        elif participant_id:
            queryset = queryset.filter(
                Q(sender_id=participant_id) | Q(recipient_id=participant_id)
            )
        missing_times += queryset.filter(occurred_at__isnull=True).count()
        queries.append((kind, _period(queryset, "occurred_at", start, end)))
    rows, available, warnings = _selection(queries, limit)
    known = [
        item.duration.total_seconds() for _, item in rows if item.duration is not None
    ]
    hours = Counter()
    pairs = Counter()
    pair_labels = {}
    durations = Counter()
    items = []
    for kind, item in rows:
        pair = sorted(
            {
                (str(item.sender_id), str(item.sender)),
                (str(item.recipient_id), str(item.recipient)),
            }
        )
        pair_label = " ↔ ".join(name for _, name in pair)
        pair_key = tuple(identifier for identifier, _ in pair)
        pairs[pair_key] += 1
        pair_labels[pair_key] = pair_label
        if item.occurred_at is not None:
            hours[item.occurred_at.astimezone(UTC).hour] += 1
        seconds = item.duration.total_seconds() if item.duration is not None else None
        bucket = (
            "Unknown"
            if seconds is None
            else "Zero"
            if seconds == 0
            else "Under 1 minute"
            if seconds < 60
            else "1 to under 5 minutes"
            if seconds < 300
            else "5 minutes or more"
        )
        durations[bucket] += 1
        items.append(
            _source(
                case,
                kind,
                item,
                {
                    "Participant pair": pair_label,
                    "Sender ID": str(item.sender_id),
                    "Recipient ID": str(item.recipient_id),
                    "Recorded call time (UTC)": _instant(item.occurred_at),
                    "Reported duration (seconds)": seconds
                    if seconds is not None
                    else "Unknown",
                },
            )
        )
    metrics = _base_metrics(rows, available, state, start, end, "Recorded call time")
    metrics.update(
        {
            "Participant filter": str(participant_id)
            if participant_id
            else "All participants",
            "Other participant filter": str(other_participant_id)
            if other_participant_id
            else "Not constrained",
            "Phone calls": sum(kind == "phone-call" for kind, _ in rows),
            "FaceTime calls": sum(kind == "facetime" for kind, _ in rows),
            "Unknown duration": len(rows) - len(known),
            "Zero duration": sum(value == 0 for value in known),
            "Reported duration total (seconds, known only)": sum(known)
            if known
            else "Unknown",
            "Reported duration median (seconds, known only)": median(known)
            if known
            else "Unknown",
            "Reported duration minimum (seconds, known only)": min(known)
            if known
            else "Unknown",
            "Reported duration maximum (seconds, known only)": max(known)
            if known
            else "Unknown",
            "Duration distribution": "; ".join(
                f"{key}: {value}" for key, value in sorted(durations.items())
            )
            or "No calls",
            "Unknown event time in selection": sum(
                item.occurred_at is None for _, item in rows
            ),
            "Missing event time before period filter": missing_times,
            "Recorded UTC hour counts": "; ".join(
                f"{hour:02d}:00–{hour:02d}:59: {count}"
                for hour, count in sorted(hours.items())
            )
            or "No known call times",
            "Participant pair counts": "; ".join(
                f"{pair_labels[pair]} [{', '.join(pair)}]: {count}"
                for pair, count in sorted(pairs.items())
            )
            or "No calls",
        }
    )
    warnings.append(
        "Counts describe retained records, not network-verified activity. Unknown "
        "duration differs from zero; these records do not report answered/missed "
        "status or verify who participated or appeared on camera."
    )
    if (start is not None or end is not None) and missing_times:
        warnings.append(
            "Calls without an event time are excluded by the selected period."
        )
    return {
        "summary": (
            f"{len(rows)} selected call records, with {len(known)} known "
            "reported durations. All times are UTC."
        ),
        "metrics": metrics,
        "items": items,
        "warnings": warnings,
    }


def _vehicle(case, state, vehicle_id, start, end, limit):
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
                    "evidence:vehicle_import_review",
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
    warnings.extend(
        [
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
    )
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
    }


def _duplicates(case, state, start, end, limit):
    queries = [
        (
            kind,
            _period(
                _state(scoped_evidence(case, kind), state), "created_at", start, end
            ),
        )
        for kind in FILE_KINDS
    ]
    for kind in CALL_KINDS:
        parent = "phone_call" if kind == "phone-call" else "facetime"
        parents = _state(scoped_evidence(case, kind), state)
        attachments = CallEvidenceAttachment.objects.filter(
            case=case, **{f"{parent}__in": parents}
        ).exclude(audio_sha256="")
        queries.append(
            (
                kind,
                _period(attachments, "created_at", start, end).select_related(parent),
            )
        )
    rows, available, warnings = _selection(queries, limit)
    groups = defaultdict(list)
    for kind, item in rows:
        checksum = item.sha256 if kind in FILE_KINDS else item.audio_sha256
        groups[checksum].append((kind, item))
    repeated = [
        (checksum, group)
        for checksum, group in sorted(groups.items())
        if len(group) > 1
    ]
    items = []
    for number, (checksum, group) in enumerate(repeated, start=1):
        for kind, item in group:
            call_attachment = kind in CALL_KINDS
            parent = item.call if call_attachment else item
            url = _url(case, kind, parent)
            if call_attachment:
                url += f"#attachment-{item.pk}"
            items.append(
                {
                    "label": f"Group {number} · {item.original_filename}",
                    "text": (
                        "Identical file-byte checksum; "
                        "retained sources remain separate."
                    ),
                    "url": url,
                    "values": {
                        "Group": number,
                        "Group occurrences": len(group),
                        "SHA-256 domain": "Original file bytes",
                        "SHA-256": checksum,
                        "Source type": f"{KIND_LABELS[kind]} audio addition"
                        if call_attachment
                        else KIND_LABELS[kind],
                        "Source ID": str(item.pk),
                        "Byte size": item.byte_size,
                        "Archived parent": parent.is_archived,
                        "Uploaded at (UTC)": _instant(item.created_at),
                    },
                }
            )
    metrics = _base_metrics(
        rows, available, state, start, end, "File/addition upload time"
    )
    metrics.update(
        {
            "Duplicate byte groups": len(repeated),
            "Occurrences in duplicate groups": len(items),
        }
    )
    warnings.append(
        "Equal byte checksums identify candidate file reuse, not independent "
        "corroboration or the same event. Communication-record and attachment "
        "manifest checksums are excluded. Different file bytes are not matched; "
        "no evidence is merged or deleted."
    )
    return {
        "summary": (
            f"{len(repeated)} exact-byte duplicate groups among "
            f"{len(rows)} selected files and audio additions."
        ),
        "metrics": metrics,
        "items": items,
        "warnings": warnings,
    }


def report(
    case,
    name,
    *,
    state="active",
    vehicle_id=None,
    participant_id=None,
    other_participant_id=None,
    start=None,
    end=None,
    limit=MAX_RECORDS,
):
    """Inspect existing fields in an already authorized case, without writes.

    A second participant selects the exact unordered pair, including self-pairs.
    """
    if name not in REPORT_NAMES:
        raise ValidationError("Choose a supported report.")
    if state not in {"active", "archived", "all"}:
        raise ValidationError("Choose active, archived, or all evidence.")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_RECORDS
    ):
        raise ValidationError(f"Report limits must be between 1 and {MAX_RECORDS}.")
    for value in (start, end):
        if value is not None and timezone.is_naive(value):
            raise ValidationError("Report times require an explicit timezone.")
    if start is not None and end is not None and start > end:
        raise ValidationError("The period end must not precede its start.")
    for model, identifier in (
        (Vehicle, vehicle_id),
        (Person, participant_id),
        (Person, other_participant_id),
    ):
        if identifier:
            try:
                exists = model.objects.filter(pk=identifier, case=case).exists()
            except ValidationError, ValueError:
                exists = False
            if not exists:
                raise Http404("Selection not found.")
    if other_participant_id and not participant_id:
        raise ValidationError("Choose the first participant before selecting a pair.")
    selected = REPORT_NAMES[name]
    if selected == "calls":
        if vehicle_id:
            raise ValidationError("A vehicle filter does not apply to call reports.")
        return _calls(
            case, state, participant_id, other_participant_id, start, end, limit
        )
    if selected == "vehicle":
        if participant_id or other_participant_id:
            raise ValidationError(
                "A participant filter does not apply to vehicle reports."
            )
        return _vehicle(case, state, vehicle_id, start, end, limit)
    if vehicle_id or participant_id or other_participant_id:
        raise ValidationError(
            "Vehicle and participant filters do not apply to file-byte reports."
        )
    return _duplicates(case, state, start, end, limit)
