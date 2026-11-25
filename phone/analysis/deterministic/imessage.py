"""Streaming message CSV review and atomic, case-authorized publication."""

import csv
import gzip
import hashlib
import io
import json
import os
from datetime import UTC, date
from stat import S_ISREG
from tempfile import TemporaryFile
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.evidence.queries import scoped_evidence
from core.models import AnalysisRun
from core.storage import canonical_bytes, private_file
from people.models import Person
from phone.message_csv import IMESSAGE_PARSER_VERSION, WHATSAPP_PARSER_VERSION
from phone.models import CommunicationContext, TextMessageEvidence

PARSER_VERSION = IMESSAGE_PARSER_VERSION
DATE_BOUND_FIELDS = ("start_date", "end_date")
OUTSIDE_DATE_RANGE = "Message Date falls outside the selected date range."
SYSTEM_NOTIFICATION = "System notification with no participant attribution."
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_FIELD_CHARS = 131072
REVIEW_STEP_BYTES = 1024 * 1024
BATCH_SIZE = 500
BATCH_CHARS = 2 * 1024 * 1024
MAX_SENDER_NAMES = 5
HEADERS = (
    "Chat Session",
    "Message ID",
    "Message Date",
    "Delivered Date",
    "Read Date",
    "Edited Date",
    "Deleted Date",
    "Service",
    "Type",
    "Sender ID",
    "Sender Name",
    "Status",
    "Replying to",
    "Subject",
    "Text",
    "Reactions",
    "Attachment",
    "Attachment type",
)
WHATSAPP_HEADERS = (
    "Chat Session",
    "Message ID",
    "Message Date",
    "Sent Date",
    "Type",
    "Sender ID",
    "Sender Name",
    "Status",
    "Forwarded",
    "Replying to",
    "Text",
    "Reactions",
    "Attachment",
    "Attachment type",
    "Attachment info",
)
PARSER_BY_HEADERS = {
    frozenset(HEADERS): PARSER_VERSION,
    frozenset(WHATSAPP_HEADERS): WHATSAPP_PARSER_VERSION,
}
# A valid CSV record can double each field character through quote escaping.
MAX_RECORD_CHARS = (
    max(len(headers) for headers in PARSER_BY_HEADERS) * (2 * MAX_FIELD_CHARS + 3) + 2
)
ROLES = ("account", "correspondent")
PERSON_FIELDS = {"first_name", "last_name", "phone_number", "email"}
CONTEXT_FIELDS = (
    "source_sha256",
    "source_row",
    "source_start",
    "source_end",
    "parser_version",
    "timezone",
    "raw_sender",
    "raw_recipient",
    "raw_timestamp",
    "native_message_id",
    "native_thread_id",
    "reply_to_id",
    "participants",
    "account_identifiers",
    "attachments",
    "source_context",
)
# Only authored review reasons may enter retained results or downloads.
ROW_REVIEW_REASONS = {
    SYSTEM_NOTIFICATION,
    "Unsupported message Type; choose Incoming or Outgoing.",
    "No message text; attachment references remain in the export.",
    "Message text exceeds the supported byte limit.",
    "A source identifier or timestamp exceeds 255 characters.",
    "Message Date is missing or invalid.",
    "Message Date is ambiguous or does not exist in this timezone.",
    "Message Date is required when limiting the import by date.",
    OUTSIDE_DATE_RANGE,
}


def _identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise AnalysisError("imessage_invalid_confirmation") from error


def _zone(parameters):
    try:
        return ZoneInfo(parameters.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise AnalysisError("imessage_invalid_confirmation") from error


def _text_limit():
    return min(settings.PANDECT_MAX_TEXT_BYTES, 1024 * 1024)


def import_file_limit():
    return min(settings.PANDECT_MAX_FILE_BYTES, MAX_FILE_BYTES)


def _timestamp(raw, zone):
    if not raw:
        return None
    try:
        value = parse_datetime(raw)
    except ValueError as error:
        raise ValueError("Message Date is missing or invalid.") from error
    if value is None:
        raise ValueError("Message Date is missing or invalid.")
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    candidates = {
        candidate.astimezone(UTC)
        for fold in (0, 1)
        if (candidate := value.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == value
    }
    if len(candidates) != 1:
        raise ValueError(
            "Message Date is ambiguous or does not exist in this timezone."
        )
    return candidates.pop()


def _people(parameters, case):
    preview_id = _identifier(parameters.get("preview_run_id"))
    selections = parameters.get("people")
    if not isinstance(selections, dict) or set(selections) != set(ROLES):
        raise AnalysisError("imessage_invalid_confirmation")
    people = {}
    for role in ROLES:
        selected = selections[role]
        if not isinstance(selected, dict) or set(selected) != {"id", "create"}:
            raise AnalysisError("imessage_invalid_confirmation")
        identifier = _identifier(selected["id"])
        create = selected["create"]
        if create is None:
            if not Person.objects.filter(case=case, pk=identifier).exists():
                raise AnalysisError("imessage_invalid_confirmation")
        else:
            if (
                identifier != uuid5(preview_id, role)
                or not isinstance(create, dict)
                or set(create) - PERSON_FIELDS
                or any(not isinstance(value, str) for value in create.values())
            ):
                raise AnalysisError("imessage_invalid_confirmation")
            try:
                Person(pk=identifier, case=case, **create).full_clean(
                    validate_unique=False
                )
            except ValidationError as error:
                raise AnalysisError("imessage_invalid_confirmation") from error
        people[role] = {"id": str(identifier), "create": create}
    return people


class _BoundedReader(io.RawIOBase):
    """Hash original storage bytes within the message CSV import limit."""

    def __init__(self, original):
        self.original = original
        self.size = 0
        self.digest = hashlib.sha256()
        self.limit = import_file_limit()

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.original.read(min(len(buffer), self.limit - self.size + 1))
        self.size += len(chunk)
        if self.size > self.limit:
            raise AnalysisError("imessage_import_limit")
        self.digest.update(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _CSVLines:
    """Track exact decoded character offsets and bound logical CSV records."""

    def __init__(self, text, *, offset=0, byte_offset=None):
        self.text = text
        self.offset = offset
        self.byte_offset = byte_offset
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.text.readline(MAX_RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > MAX_RECORD_CHARS:
            raise AnalysisError("imessage_import_limit")
        self.offset += len(line)
        if self.byte_offset is not None:
            self.byte_offset += len(line.encode("utf-8"))
        return line


def _raw_rows(source, *, review=None, checkpoint=None):
    """Yield logical records with their profile and verify file integrity at EOF."""
    if source.item.byte_size > import_file_limit():
        raise AnalysisError("imessage_import_limit")
    try:
        with private_file(source.item, source.item.case_id).open("rb") as original:
            path = _local_path(source) if review is not None else None
            byte_offset = None
            offset, number = 0, 1
            if checkpoint:
                if _file_identity(original, path) != review["source_marker"]:
                    raise AnalysisError("imessage_source_changed")
                byte_offset, offset, number, _, _ = checkpoint
                original.seek(byte_offset)
            elif review is not None:
                if (marker := _file_identity(original, path)) is not None:
                    byte_offset = 3 if original.read(3) == b"\xef\xbb\xbf" else 0
                    original.seek(0)
                    review["source_marker"] = marker
            bounded = _BoundedReader(original)
            with io.TextIOWrapper(
                io.BufferedReader(bounded),
                encoding="utf-8" if checkpoint else "utf-8-sig",
                newline="",
            ) as text:
                lines = _CSVLines(text, offset=offset, byte_offset=byte_offset)
                reader = csv.reader(lines, strict=True)
                header = review["headers"] if checkpoint else next(reader, [])
                header_names = frozenset(header)
                parser_version = PARSER_BY_HEADERS.get(header_names)
                if parser_version is None or len(header) != len(header_names):
                    raise AnalysisError("imessage_invalid_format")
                if review is not None and byte_offset is not None:
                    review["headers"] = header
                while True:
                    lines.record_chars = 0
                    start = lines.offset
                    byte_start = lines.byte_offset
                    cells = next(reader, None)
                    if cells is None:
                        break
                    if not cells:
                        continue
                    number += 1
                    if len(cells) != len(header) or any(
                        "\x00" in value for value in cells
                    ):
                        raise AnalysisError("imessage_invalid_format")
                    if any(len(value) > MAX_FIELD_CHARS for value in cells):
                        raise AnalysisError("imessage_import_limit")
                    if review is not None:
                        review["byte_start"] = byte_start
                    yield (
                        number,
                        start,
                        lines.offset,
                        dict(zip(header, cells, strict=True)),
                        parser_version,
                    )
                if checkpoint:
                    return
                if number == 1:
                    raise AnalysisError("imessage_invalid_format")
                if (
                    bounded.digest.hexdigest() != source.item.sha256
                    or bounded.size != source.item.byte_size
                ):
                    raise AnalysisError("imessage_source_changed")
                if review and review.get("source_marker") != _file_identity(
                    original, path
                ):
                    raise AnalysisError("imessage_source_changed")
    except csv.Error as error:
        code = (
            "imessage_import_limit"
            if str(error).startswith("field larger than field limit")
            else "imessage_invalid_format"
        )
        raise AnalysisError(code) from error
    except UnicodeDecodeError as error:
        raise AnalysisError("imessage_invalid_format") from error
    except (OSError, ValueError) as error:
        raise AnalysisError("source_unavailable") from error


def _row(
    source,
    number,
    start,
    end,
    raw,
    parser_version,
    zone,
    text_limit,
    people,
    date_bounds,
):
    direction = raw["Type"]
    sender_role = "account" if direction == "Outgoing" else "correspondent"
    recipient_role = "correspondent" if direction == "Outgoing" else "account"
    system_notification = (
        parser_version == WHATSAPP_PARSER_VERSION and direction == "Notification"
    )
    raw_sender = raw["Sender ID"] or direction
    if system_notification:
        sender_role = recipient_role = ""
        raw_sender = raw["Sender ID"]
    attachment = {
        "href": raw["Attachment"],
        "type": raw["Attachment type"],
        "availability": "not ingested; source reference only",
    }
    if attachment_info := raw.get("Attachment info"):
        attachment["info"] = attachment_info
    values = {
        "source_document_id": str(source.item.pk),
        "source_sha256": source.item.sha256,
        "source_row": number,
        "source_start": start,
        "source_end": end,
        "parser_version": parser_version,
        "timezone": zone.key,
        "direction": direction,
        "sender_role": sender_role,
        "recipient_role": recipient_role,
        "raw_sender": raw_sender,
        "raw_recipient": "",
        "raw_timestamp": raw["Message Date"],
        "native_message_id": raw["Message ID"],
        "native_thread_id": "",
        "reply_to_id": raw["Replying to"],
        "participants": [],
        "account_identifiers": {
            "service": raw.get("Service", "WhatsApp"),
            "sender_id": raw["Sender ID"],
            "sender_name": raw["Sender Name"],
        },
        "attachments": [attachment]
        if attachment["href"] or attachment["type"] or attachment_info
        else [],
        "source_context": {
            "format": parser_version,
            "raw_fields": {
                name: value for name, value in raw.items() if name != "Text"
            },
        },
    }
    try:
        if system_notification:
            raise ValueError(SYSTEM_NOTIFICATION)
        if direction not in {"Incoming", "Outgoing"}:
            raise ValueError("Unsupported message Type; choose Incoming or Outgoing.")
        if not raw["Text"]:
            raise ValueError(
                "No message text; attachment references remain in the export."
            )
        if (
            len(raw["Text"]) > MAX_FIELD_CHARS
            or len(raw["Text"].encode("utf-8")) > text_limit
        ):
            raise ValueError("Message text exceeds the supported byte limit.")
        if any(
            len(values[name]) > 255
            for name in (
                "raw_sender",
                "raw_timestamp",
                "native_message_id",
                "reply_to_id",
            )
        ):
            raise ValueError("A source identifier or timestamp exceeds 255 characters.")
        event = _timestamp(raw["Message Date"], zone)
        occurred_at = event.isoformat() if event is not None else None
        values["occurred_at"] = occurred_at
        start_date, end_date = date_bounds
        if start_date or end_date:
            if event is None:
                raise ValueError(
                    "Message Date is required when limiting the import by date."
                )
            export_date = event.astimezone(zone).date()
            if (start_date and export_date < start_date) or (
                end_date and export_date > end_date
            ):
                raise ValueError(OUTSIDE_DATE_RANGE)
        if people is not None:
            values["import_message"] = {
                "sender_id": people[sender_role]["id"],
                "recipient_id": people[recipient_role]["id"],
                "occurred_at": occurred_at,
            }
    except (ValueError, TypeError, OverflowError) as error:
        values["unresolved_reason"] = next(
            (reason for reason in ROW_REVIEW_REASONS if reason == str(error)),
            "Message fields could not be interpreted; review the source row.",
        )
    return {
        "label": ("Excluded message" if "unresolved_reason" in values else "Message")
        + f" row {number}",
        "text": raw["Text"],
        "source_ids": [str(source.id)],
        "anchors": [source.anchor(source_row=number, csv_start=start, csv_end=end)],
        "values": values,
    }


def _update_digest(digest, item):
    values = item["values"]
    digest.update(
        canonical_bytes(
            {
                "text": item["text"],
                "values": {
                    name: values.get(name)
                    for name in (
                        *CONTEXT_FIELDS,
                        "source_document_id",
                        "direction",
                        "sender_role",
                        "recipient_role",
                        "occurred_at",
                        "unresolved_reason",
                    )
                },
            }
        )
    )
    digest.update(b"\n")


def _date_scope(parameters):
    date_parameters = {
        field: parameters[field] for field in DATE_BOUND_FIELDS if field in parameters
    }
    try:
        start_date, end_date = date_bounds = tuple(
            date.fromisoformat(date_parameters[field])
            if field in date_parameters
            else None
            for field in DATE_BOUND_FIELDS
        )
    except (ValueError, TypeError) as error:
        raise AnalysisError("imessage_invalid_confirmation") from error
    if start_date and end_date and end_date < start_date:
        raise AnalysisError("imessage_invalid_confirmation")
    return date_parameters, date_bounds


def _stream_rows(source, parameters, state):
    zone = _zone(parameters)
    date_parameters, date_bounds = _date_scope(parameters)
    phase = parameters.get("phase", "preview")
    if phase not in ("preview", "import"):
        raise AnalysisError("imessage_invalid_confirmation")
    people = _people(parameters, source.item.case) if phase == "import" else None
    limit = (
        parameters.get("_imessage_text_limit_bytes", _text_limit())
        if phase == "preview"
        else _text_limit()
    )
    if type(limit) is not int or not 0 < limit <= 1024 * 1024:
        raise AnalysisError("imessage_invalid_confirmation")
    summary = {
        "chat_session": "",
        "sender_id": "",
        "sender_names": [],
        "sender_names_omitted": False,
        "sender_names_truncated": False,
        "chat_session_truncated": False,
        "sender_id_truncated": False,
        "source_document_id": str(source.item.pk),
        "source_sha256": source.item.sha256,
        "source_byte_size": source.item.byte_size,
        "timezone": zone.key,
        "text_limit_bytes": limit,
        "incoming_rows": 0,
        "outgoing_rows": 0,
    }
    metrics = {"source_rows": 0, "ready_rows": 0, "unresolved_rows": 0}
    if date_parameters:
        metrics["date_excluded_rows"] = 0
    digest = hashlib.sha256()
    session_label = sender_identifier = ""
    sender_name_digests = []
    review = state.get("review")
    checkpoints = []
    for number, start, end, raw, parser_version in _raw_rows(source, review=review):
        if (
            review
            and (byte_start := review["byte_start"]) is not None
            and (
                not checkpoints or byte_start - checkpoints[-1][0] >= REVIEW_STEP_BYTES
            )
        ):
            checkpoints.append(
                [
                    byte_start,
                    start,
                    number - 1,
                    metrics["source_rows"],
                    metrics["unresolved_rows"] + metrics.get("date_excluded_rows", 0),
                ]
            )
        if metrics["source_rows"] == 0:
            summary["parser_version"] = parser_version
            digest.update(
                canonical_bytes(
                    {
                        "parser_version": parser_version,
                        "timezone": zone.key,
                        "text_limit_bytes": limit,
                        **date_parameters,
                    }
                )
            )
        session = raw["Chat Session"]
        if session:
            if session_label and session_label != session:
                raise AnalysisError("imessage_multiple_chats")
            session_label = session
            summary["chat_session"] = session[:255]
            summary["chat_session_truncated"] = len(session) > 255
        if raw["Type"] == "Incoming":
            summary["incoming_rows"] += 1
            identifier = raw["Sender ID"]
            if identifier:
                if sender_identifier and sender_identifier != identifier:
                    raise AnalysisError("imessage_multiple_chats")
                sender_identifier = identifier
                summary["sender_id"] = identifier[:255]
                summary["sender_id_truncated"] = len(identifier) > 255
            name = raw["Sender Name"]
            name_digest = hashlib.sha256(name.encode("utf-8")).digest()
            if name and name_digest not in sender_name_digests:
                if len(summary["sender_names"]) < MAX_SENDER_NAMES:
                    summary["sender_names"].append(name[:255])
                    sender_name_digests.append(name_digest)
                    summary["sender_names_truncated"] |= len(name) > 255
                else:
                    summary["sender_names_omitted"] = True
        elif raw["Type"] == "Outgoing":
            summary["outgoing_rows"] += 1
        item = _row(
            source,
            number,
            start,
            end,
            raw,
            parser_version,
            zone,
            limit,
            people,
            date_bounds,
        )
        metrics["source_rows"] += 1
        reason = item["values"].get("unresolved_reason")
        if reason == OUTSIDE_DATE_RANGE:
            metrics["date_excluded_rows"] += 1
        elif reason:
            metrics["unresolved_rows"] += 1
        else:
            metrics["ready_rows"] += 1
        _update_digest(digest, item)
        yield item
    summary["interpretation_sha256"] = digest.hexdigest()
    expected = parameters.get("_imessage_interpretation_sha256")
    if expected is not None and expected != summary["interpretation_sha256"]:
        raise AnalysisError("imessage_invalid_confirmation")
    if checkpoints:
        summary["review"] = {
            key: review[key] for key in ("source_marker", "headers")
        } | {"checkpoints": checkpoints, "dates": date_parameters}
    state.update(imessage=summary, metrics=metrics)


def iter_import_rows(source, parameters):
    """Yield row items; exhaust to verify checksum, chat shape, and optional digest.

    Historical review may supply `_imessage_text_limit_bytes` and
    `_imessage_interpretation_sha256` from its retained compact summary. Import
    publication always uses the current operator text limit.
    """
    return _stream_rows(source, parameters, {})


def read_import_page(
    source, parameters, *, page, per_page=25, excluded_only=False, retained=None
):
    """Page verified rows, optionally showing only rows omitted by the review."""
    if (
        type(page) is not int
        or page < 1
        or type(per_page) is not int
        or not 1 <= per_page <= 100
    ):
        raise AnalysisError("imessage_invalid_confirmation")
    rows, samples = [], []
    start = (page - 1) * per_page
    if retained and (review := retained["imessage"].get("review")):
        summary = retained["imessage"]
        marker = review["source_marker"]
        dates, date_bounds = _date_scope(parameters)
        if (
            summary["source_sha256"] == source.item.sha256
            and summary["timezone"] == parameters["timezone"]
            and summary["text_limit_bytes"] == parameters["_imessage_text_limit_bytes"]
            and summary["interpretation_sha256"]
            == parameters["_imessage_interpretation_sha256"]
            and dates == review["dates"]
            and _storage_identity(source) == marker
        ):
            rank = 4 if excluded_only else 3
            checkpoint = next(
                item for item in reversed(review["checkpoints"]) if item[rank] <= start
            )
            total = checkpoint[rank]
            stream = _raw_rows(source, review=review.copy(), checkpoint=checkpoint)
            try:
                zone = _zone(parameters)
                for number, left, right, raw, parser in stream:
                    item = _row(
                        source,
                        number,
                        left,
                        right,
                        raw,
                        parser,
                        zone,
                        summary["text_limit_bytes"],
                        None,
                        date_bounds,
                    )
                    if excluded_only and not item["values"].get("unresolved_reason"):
                        continue
                    if total >= start:
                        rows.append(item)
                    total += 1
                    if len(rows) >= per_page:
                        break
            finally:
                stream.close()
            if _storage_identity(source) != marker:
                raise AnalysisError("imessage_source_changed")
            metrics = retained["metrics"]
            total = metrics["source_rows"]
            if excluded_only:
                total = metrics["unresolved_rows"] + metrics.get(
                    "date_excluded_rows", 0
                )
            return rows, total, retained["imessage_samples"]
    total = 0
    for index, item in enumerate(iter_import_rows(source, parameters)):
        if index < 3:
            samples.append(_sample(item))
        if excluded_only and not item["values"].get("unresolved_reason"):
            continue
        if start <= total < start + per_page:
            rows.append(item)
        total += 1
    return rows, total, samples


def _sample(item):
    values = item["values"]
    return {
        "text": item["text"][:240],
        "values": {
            "source_row": values["source_row"],
            "direction": values["direction"][:255],
            "raw_timestamp": values["raw_timestamp"][:255],
            "unresolved_reason": values.get("unresolved_reason", ""),
        },
    }


def prepare_import(sources, parameters, *, publication=None):
    """Retain a compact summary and digest; never store a per-row result payload."""
    sources = list(sources)
    if len(sources) != 1 or sources[0].kind != "document" or sources[0].attachment:
        raise AnalysisError("imessage_invalid_format")
    state = {}
    # Retained display overrides belong only to read_import_page/iter_import_rows.
    parameters = {
        key: value
        for key, value in parameters.items()
        if not key.startswith("_imessage_")
    }
    samples = []
    retain_samples = parameters.get("phase", "preview") == "preview"
    if retain_samples:
        state["review"] = {}
    for item in _stream_rows(sources[0], parameters, state):
        if retain_samples and len(samples) < 3:
            samples.append(_sample(item))
        if publication is not None and "import_message" in item["values"]:
            row = {"text": item["text"], "values": item["values"]}
            publication.write(canonical_bytes(row) + b"\n")
    unresolved = state["metrics"]["unresolved_rows"]
    data = output(
        "Prepared message CSV rows for participant and timezone review."
        if parameters.get("phase", "preview") == "preview"
        else "Prepared confirmed CSV messages for atomic publication.",
        metrics=state["metrics"],
        warnings=[
            "The original CSV remains the retained source. Text preserves the "
            "decoded CSV Text field; attachments are references and are never "
            "opened or fetched.",
            "Confirm that this is one direct chat. Source labels do not verify "
            "identity or establish group membership. Edited/deleted markers do "
            "not recover earlier content.",
        ],
        outcome="partial" if unresolved else "complete",
    )
    data["imessage"] = state["imessage"]
    if retain_samples:
        data["imessage_samples"] = samples
    return data


def _local_path(source):
    try:
        return private_file(source.item, source.item.case_id).path
    except NotImplementedError:
        return None


def _file_identity(original, path):
    """Identify an open file only when it is the original at the storage path."""
    if path is None:
        return None
    try:
        stat = os.fstat(original.fileno())
        path_stat = os.stat(path)
    except AttributeError, OSError, ValueError:
        return None
    if S_ISREG(stat.st_mode) and os.path.samestat(stat, path_stat):
        fields = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        return hashlib.sha256(canonical_bytes(fields)).hexdigest()
    return None


def _storage_identity(source):
    try:
        path = _local_path(source)
        if path is None:
            return None
        with private_file(source.item, source.item.case_id).open("rb") as original:
            return _file_identity(original, path)
    except (OSError, ValueError) as error:
        raise AnalysisError("source_unavailable") from error


def stage_import(sources, parameters):
    """Prepare a verified local spool, handing its open file to the caller."""
    if len(sources) != 1 or sources[0].kind != "document":
        raise AnalysisError("imessage_invalid_format")
    before = _storage_identity(sources[0])
    if before is None:
        return prepare_import(sources, parameters), None, None
    publication = TemporaryFile()
    try:
        with gzip.GzipFile(fileobj=publication, mode="wb", compresslevel=1) as spool:
            data = prepare_import(sources, parameters, publication=spool)
        if before != _storage_identity(sources[0]):
            raise AnalysisError("imessage_source_changed")
        publication.seek(0)
        return data, publication, before
    except BaseException:
        publication.close()
        raise


def _unpublished(data):
    return {
        key: value
        for key, value in data.items()
        if key not in {"import_publication_version", "metrics"}
    } | {
        "metrics": {
            key: value
            for key, value in data["metrics"].items()
            if key not in {"created_rows", "reused_rows"}
        }
    }


def _publication_batches(rows):
    batch, characters = [], 0
    for item in rows:
        values = item["values"]
        if "import_message" not in values:
            continue
        size = values["source_end"] - values["source_start"]
        if batch and (len(batch) >= BATCH_SIZE or characters + size > BATCH_CHARS):
            yield batch
            batch, characters = [], 0
        batch.append(item)
        characters += size
    if batch:
        yield batch


def _create_people(case, people, contexts):
    for selected in people.values():
        if selected["create"] is None:
            continue
        identifier = UUID(selected["id"])
        existing = Person.objects.filter(pk=identifier).first()
        if existing is not None:
            if (
                existing.case_id != case.pk
                or not contexts.filter(
                    Q(message__sender_id=identifier)
                    | Q(message__recipient_id=identifier)
                ).exists()
            ):
                raise AnalysisError("imessage_conflict")
            continue
        person = Person(pk=identifier, case=case, **selected["create"])
        person.full_clean()
        person.save()


def apply_import(run, data, sources, *, publication=None, source_marker=None):
    """Publish bounded batches atomically; any failure rolls back the import."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Message CSV publication requires a transaction.")
    case = get_case(run.requested_by, run.case_id, edit=True, lock=True)
    if (
        run.operation != "imessage_import"
        or run.method_version != "2"
        or run.parameters.get("phase") != "import"
    ):
        raise AnalysisError("imessage_invalid_confirmation")
    if (
        len(sources) != 1
        or sources[0].kind != "document"
        or sources[0].item.case_id != case.pk
    ):
        raise AnalysisError("source_unavailable")
    source = sources[0]
    if (
        len(run.inputs) != 1
        or run.inputs[0].get("evidence_id") != str(source.item.pk)
        or run.inputs[0].get("sha256") != source.item.sha256
    ):
        raise AnalysisError("imessage_source_changed")
    try:
        preview = AnalysisRun.objects.select_related("result").get(
            pk=_identifier(run.parameters.get("preview_run_id")),
            case=case,
            operation="imessage_import",
            method_version="2",
            status__in=("complete", "partial"),
            result__case=case,
        )
    except AnalysisRun.DoesNotExist as error:
        raise AnalysisError("imessage_invalid_confirmation") from error
    if (
        preview.parameters.get("phase") != "preview"
        or len(preview.inputs) != 1
        or preview.inputs[0].get("evidence_id") != str(source.item.pk)
        or preview.inputs[0].get("sha256") != source.item.sha256
        or any(
            run.parameters.get(field) != preview.parameters.get(field)
            for field in ("timezone", *DATE_BOUND_FIELDS)
        )
    ):
        raise AnalysisError("imessage_invalid_confirmation")
    # The normal worker stages one exact parse before taking the case lock.
    # Other storage backends without a file marker require full revalidation.
    if publication is not None and source_marker is not None:
        if _storage_identity(source) != source_marker:
            raise AnalysisError("imessage_source_changed")
        expected = _unpublished(data)
        items = _staged_items(publication)
    else:
        expected = prepare_import(sources, run.parameters)
        if _unpublished(data) != expected:
            raise AnalysisError("imessage_invalid_confirmation")
        items = iter_import_rows(
            source,
            run.parameters
            | {
                "_imessage_interpretation_sha256": expected["imessage"][
                    "interpretation_sha256"
                ]
            },
        )
    if expected["imessage"]["interpretation_sha256"] != preview.result.data.get(
        "imessage", {}
    ).get("interpretation_sha256"):
        raise AnalysisError("imessage_invalid_confirmation")
    people = _people(run.parameters, case)
    contexts = CommunicationContext.objects.filter(source_document=source.item)
    scope = Q(case=case, message__in=scoped_evidence(case, "text-message"))
    if contexts.exclude(scope).exists():
        raise AnalysisError("imessage_conflict")
    contexts = contexts.filter(scope)
    if expected["metrics"]["ready_rows"]:
        _create_people(case, people, contexts)
    parser_version = expected["imessage"]["parser_version"]
    submission_prefix = (
        "whatsapp" if parser_version == WHATSAPP_PARSER_VERSION else "imessage"
    )
    from core.case_search.indexing import add_sources

    created = reused = 0
    for batch in _publication_batches(items):
        existing_rows = {
            context.source_row: context
            for context in contexts.filter(
                source_row__in=[item["values"]["source_row"] for item in batch]
            ).select_related("message")
        }
        new_messages, new_contexts = [], []
        for item in batch:
            values = item["values"]
            prepared = values["import_message"]
            fields = {name: values[name] for name in CONTEXT_FIELDS}
            existing = existing_rows.get(values["source_row"])
            if existing is not None:
                message = existing.message
                if (
                    message.body != item["text"]
                    or str(message.sender_id) != prepared["sender_id"]
                    or str(message.recipient_id) != prepared["recipient_id"]
                    or (
                        message.occurred_at.isoformat() if message.occurred_at else None
                    )
                    != prepared["occurred_at"]
                    or any(
                        getattr(existing, name) != value
                        for name, value in fields.items()
                    )
                ):
                    raise AnalysisError("imessage_conflict")
                reused += 1
                continue
            message = TextMessageEvidence(
                case=case,
                uploaded_by=run.requested_by,
                title=f"Imported message · row {values['source_row']}",
                body=item["text"],
                sender_id=prepared["sender_id"],
                recipient_id=prepared["recipient_id"],
                occurred_at=parse_datetime(prepared["occurred_at"])
                if prepared["occurred_at"] is not None
                else None,
                source=source.item.source,
                source_description=(
                    f"Retained message CSV export {source.item.pk}, "
                    f"row {values['source_row']}; {parser_version}"
                ),
                submission_id=uuid5(
                    source.item.pk, f"{submission_prefix}:{values['source_row']}"
                ),
            )
            message._prepare_original()
            new_messages.append(message)
            new_contexts.append(
                CommunicationContext(
                    case=case,
                    message=message,
                    source_document=source.item,
                    import_run=run,
                    **fields,
                )
            )
        if new_messages:
            TextMessageEvidence.objects.bulk_create(new_messages, batch_size=BATCH_SIZE)
            CommunicationContext.objects.bulk_create(
                new_contexts, batch_size=BATCH_SIZE
            )
            add_sources(case.pk, "message", [message.pk for message in new_messages])
            created += len(new_messages)
    data["import_publication_version"] = 2
    data["metrics"].update(created_rows=created, reused_rows=reused)
    return created


def _staged_items(publication):
    with gzip.GzipFile(fileobj=publication, mode="rb") as spool:
        yield from map(json.loads, spool)
