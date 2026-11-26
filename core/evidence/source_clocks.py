"""Clock roles and source details for one page of recorded chronology."""

import re
from datetime import date
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Exists, F, OuterRef, Q
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS
from phone.conversations import imported_contexts


def set_call_interval(row, start, duration):
    row["duration"] = duration
    if duration is not None:
        try:
            row["end_at"] = start + duration
        except OverflowError:
            row["end_out_of_range"] = True


def _export(row, case, document_id, source_row, zone, version):
    row["export_url"] = reverse(
        "core:evidence:detail",
        args=(case.pk, "document", document_id),
    )
    row["source_row"] = source_row
    row["clock_fields"].extend((("Export timezone", zone), ("Import parser", version)))


def _message_contexts(case):
    return imported_contexts(case).filter(
        Q(import_run__isnull=True) | Q(import_run__case=case),
        source_sha256=F("source_document__sha256"),
    )


def _date_only_contexts(case):
    # ISO calendar and week dates accepted by the message importer without
    # a time component. Its stored midnight is a parsing convention.
    return _message_contexts(case).filter(
        raw_timestamp__regex=(
            r"^(?:[0-9]{8}|[0-9]{4}-[0-9]{2}-[0-9]{2}|"
            r"[0-9]{4}-?W[0-9]{2}(-?[1-7])?)$"
        )
    )


def date_only_messages(case):
    return _date_only_contexts(case).order_by().values("message_id")


def is_date_only_message(case, *, through=None):
    contexts = _date_only_contexts(case).filter(message_id=OuterRef("pk"))
    if through is not None:
        contexts = contexts.filter(created_at__lte=through)
    return Exists(contexts)


def date_only_message_dates(case, message_ids):
    dates = {}
    for message_id, raw in (
        _date_only_contexts(case)
        .filter(message_id__in=message_ids)
        .order_by()
        .values_list("message_id", "raw_timestamp")
    ):
        try:
            dates[message_id] = date.fromisoformat(raw)
        except ValueError:
            dates[message_id] = None
    return dates


def attach_unplaced_dates(case, rows):
    messages = {row["id"]: row for row in rows if row["kind"] == "text-message"}
    for context in (
        _date_only_contexts(case)
        .filter(message_id__in=messages)
        .only(
            "message_id",
            "source_document_id",
            "raw_timestamp",
            "source_row",
            "parser_version",
            "timezone",
        )
    ):
        row = messages[context.message_id]
        try:
            row["source_date"] = date.fromisoformat(context.raw_timestamp)
        except ValueError:
            continue
        row["source_zone"] = context.timezone
        row["clock_fields"] = [("Message Date as supplied", context.raw_timestamp)]
        _export(
            row,
            case,
            context.source_document_id,
            context.source_row,
            context.timezone,
            context.parser_version,
        )


def _clock_note(raw, zone):
    try:
        reported = parse_datetime(raw)
        if reported and reported.utcoffset() is not None:
            local = reported.astimezone(ZoneInfo(zone))
            if reported.utcoffset() != local.utcoffset():
                return "Supplied offset differs from the export timezone."
    except ValueError, OverflowError, ZoneInfoNotFoundError:
        pass
    return ""


def attach_daily_message_clocks(case, rows):
    messages = {row["id"]: row for row in rows if row["kind"] == "message"}
    for pk, raw, zone in (
        _message_contexts(case)
        .filter(message_id__in=messages)
        .values_list("message_id", "raw_timestamp", "timezone")
    ):
        messages[pk]["clock_note"] = _clock_note(raw, zone)


def _message_clocks(case, rows):
    contexts = (
        _message_contexts(case)
        .filter(message_id__in=rows)
        .only(
            "message_id",
            "source_document_id",
            "source_row",
            "parser_version",
            "timezone",
            "raw_timestamp",
            "source_context",
        )
    )
    for context in contexts:
        row = rows[context.message_id]
        raw = context.raw_timestamp
        row["clock_fields"] = [("Message Date as supplied", raw or "Unknown")]
        _export(
            row,
            case,
            context.source_document_id,
            context.source_row,
            context.timezone,
            context.parser_version,
        )
        row["clock_note"] = _clock_note(raw, context.timezone)
        # Read only precision actually written in the source wall clock.
        clock = re.match(
            r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::(\d\d)([.,]\d+)?)?(?:Z|[+-]|$)",
            raw,
        )
        if clock:
            precision = "Minute"
            if clock[1]:
                precision = "Fractional second" if clock[2] else "Second"
            row["clock_fields"].append(("Source precision", precision))
        details = context.source_context
        raw_fields = details.get("raw_fields") if isinstance(details, dict) else None
        if isinstance(raw_fields, dict):
            for label in ("Delivered Date", "Read Date", "Edited Date", "Deleted Date"):
                value = raw_fields.get(label)
                if isinstance(value, str) and value:
                    row["clock_fields"].append((label + " as supplied", value[:255]))


def _vehicle_clocks(case, rows):
    observations = (
        scoped_evidence(case, "vehicle-location")
        .filter(
            pk__in=rows,
            import_batch__isnull=False,
        )
        .select_related("import_batch__document")
    )
    for observation in observations:
        # The immutable observation digest covers the row, the interpretation
        # and its parent document ID/hash. Do not retarget a changed export.
        if sha256(observation.original_bytes()).hexdigest() != observation.sha256:
            continue
        batch = observation.import_batch
        raw = observation.raw_row
        if not isinstance(raw, dict):
            continue
        row = rows[observation.pk]
        row["clock_fields"] = [
            ("Created Time as supplied", raw.get("Created Time") or "Unknown"),
            ("Source precision", "Second"),
        ]
        _export(
            row,
            case,
            batch.document_id,
            observation.source_row,
            batch.timezone,
            batch.parser_version,
        )
        if receipt := raw.get("Received Time"):
            row["clock_fields"].append(("Received Time as supplied", receipt))
        if (
            observation.received_at is not None
            and observation.received_at < observation.observed_at
        ):
            row["clock_note"] = "The reported receipt precedes the observation."


def attach_clock_details(case, rows):
    """Hydrate only visible source clocks, independently of acquisition rows."""
    by_kind = {}
    for row in rows:
        kind = row["kind"]
        row["clock_fields"] = [("Source offset and precision", "Not retained")]
        if row["event_label"] == "Collected":
            row["clock_label"] = "Collected"
            continue
        if kind in CALL_KINDS:
            row["clock_label"] = "Reported call start"
        elif kind == "text-message":
            row["clock_label"] = "Reported message time"
        else:
            row["clock_label"] = "Reported observation"
        by_kind.setdefault(kind, {})[row["id"]] = row
    _message_clocks(case, by_kind.get("text-message", {}))
    _vehicle_clocks(case, by_kind.get("vehicle-location", {}))
    for kind in CALL_KINDS:
        calls = by_kind.get(kind, {})
        for call in (
            scoped_evidence(case, kind)
            .filter(pk__in=calls)
            .select_related("import_row__batch")
            .only(
                "duration",
                "import_row__number",
                "import_row__raw",
                "import_row__batch__document_id",
                "import_row__batch__timezone",
                "import_row__batch__parser_version",
            )
        ):
            row = calls[call.pk]
            set_call_interval(row, row["event_at"], call.duration)
            if call.import_row_id:
                source = call.import_row
                row["clock_fields"] = [("Call date as supplied", source.raw["Date"])]
                _export(
                    row,
                    case,
                    source.batch.document_id,
                    source.number,
                    source.batch.timezone,
                    source.batch.parser_version,
                )
