"""Streaming message CSV review and atomic, case-authorized publication."""

import csv
import hashlib
import io
from datetime import UTC
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
from core.storage import canonical_bytes
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence

PARSER_VERSION = "imazing-csv-v1"
MAX_FIELD_CHARS = 131072
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
# A valid CSV record can double each field character through quote escaping.
MAX_RECORD_CHARS = len(HEADERS) * (2 * MAX_FIELD_CHARS + 3) + 2
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


def _identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise AnalysisError("imazing_invalid_confirmation") from error


def _zone(parameters):
    try:
        return ZoneInfo(parameters.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise AnalysisError("imazing_invalid_confirmation") from error


def _text_limit():
    return min(settings.PANDECT_MAX_TEXT_BYTES, 1024 * 1024)


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
        raise AnalysisError("imazing_invalid_confirmation")
    people = {}
    for role in ROLES:
        selected = selections[role]
        if not isinstance(selected, dict) or set(selected) != {"id", "create"}:
            raise AnalysisError("imazing_invalid_confirmation")
        identifier = _identifier(selected["id"])
        create = selected["create"]
        if create is None:
            if not Person.objects.filter(case=case, pk=identifier).exists():
                raise AnalysisError("imazing_invalid_confirmation")
        else:
            if (
                identifier != uuid5(preview_id, role)
                or not isinstance(create, dict)
                or set(create) - PERSON_FIELDS
                or any(not isinstance(value, str) for value in create.values())
            ):
                raise AnalysisError("imazing_invalid_confirmation")
            try:
                Person(pk=identifier, case=case, **create).full_clean(
                    validate_unique=False
                )
            except ValidationError as error:
                raise AnalysisError("imazing_invalid_confirmation") from error
        people[role] = {"id": str(identifier), "create": create}
    return people


class _BoundedReader(io.RawIOBase):
    """Hash original storage bytes while bounding reads by the normal upload limit."""

    def __init__(self, original):
        self.original = original
        self.size = 0
        self.digest = hashlib.sha256()
        self.limit = settings.PANDECT_MAX_FILE_BYTES

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.original.read(min(len(buffer), self.limit - self.size + 1))
        self.size += len(chunk)
        if self.size > self.limit:
            raise AnalysisError("imazing_import_limit")
        self.digest.update(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


class _CSVLines:
    """Track exact decoded character offsets and bound logical CSV records."""

    def __init__(self, text):
        self.text = text
        self.offset = 0
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.text.readline(MAX_RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > MAX_RECORD_CHARS:
            raise AnalysisError("imazing_import_limit")
        self.offset += len(line)
        return line


def _raw_rows(source):
    """Yield logical records; consuming the iterator verifies file integrity."""
    try:
        with source.item.file.open("rb") as original:
            bounded = _BoundedReader(original)
            with io.TextIOWrapper(
                io.BufferedReader(bounded), encoding="utf-8-sig", newline=""
            ) as text:
                lines = _CSVLines(text)
                reader = csv.reader(lines, strict=True)
                header = next(reader, [])
                if len(header) != len(HEADERS) or set(header) != set(HEADERS):
                    raise AnalysisError("imazing_invalid_format")
                number = 1
                while True:
                    lines.record_chars = 0
                    start = lines.offset
                    cells = next(reader, None)
                    if cells is None:
                        break
                    if not cells:
                        continue
                    number += 1
                    if len(cells) != len(header) or any(
                        "\x00" in value for value in cells
                    ):
                        raise AnalysisError("imazing_invalid_format")
                    if any(len(value) > MAX_FIELD_CHARS for value in cells):
                        raise AnalysisError("imazing_import_limit")
                    yield (
                        number,
                        start,
                        lines.offset,
                        dict(zip(header, cells, strict=True)),
                    )
                if number == 1:
                    raise AnalysisError("imazing_invalid_format")
                if (
                    bounded.digest.hexdigest() != source.item.sha256
                    or bounded.size != source.item.byte_size
                ):
                    raise AnalysisError("imazing_source_changed")
    except csv.Error as error:
        code = (
            "imazing_import_limit"
            if str(error).startswith("field larger than field limit")
            else "imazing_invalid_format"
        )
        raise AnalysisError(code) from error
    except UnicodeDecodeError as error:
        raise AnalysisError("imazing_invalid_format") from error
    except (OSError, ValueError) as error:
        raise AnalysisError("source_unavailable") from error


def _row(source, number, start, end, raw, zone, text_limit, people):
    direction = raw["Type"]
    sender_role = "account" if direction == "Outgoing" else "correspondent"
    recipient_role = "correspondent" if direction == "Outgoing" else "account"
    values = {
        "source_document_id": str(source.item.pk),
        "source_sha256": source.item.sha256,
        "source_row": number,
        "source_start": start,
        "source_end": end,
        "parser_version": PARSER_VERSION,
        "timezone": zone.key,
        "direction": direction,
        "sender_role": sender_role,
        "recipient_role": recipient_role,
        "raw_sender": raw["Sender ID"] or direction,
        "raw_recipient": "",
        "raw_timestamp": raw["Message Date"],
        "native_message_id": raw["Message ID"],
        "native_thread_id": "",
        "reply_to_id": raw["Replying to"],
        "participants": [],
        "account_identifiers": {
            "service": raw["Service"],
            "sender_id": raw["Sender ID"],
            "sender_name": raw["Sender Name"],
        },
        "attachments": [
            {
                "href": raw["Attachment"],
                "type": raw["Attachment type"],
                "availability": "not ingested; source reference only",
            }
        ]
        if raw["Attachment"] or raw["Attachment type"]
        else [],
        "source_context": {
            "format": PARSER_VERSION,
            "raw_fields": {
                name: value for name, value in raw.items() if name != "Text"
            },
        },
    }
    try:
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
        if people is not None:
            values["import_message"] = {
                "sender_id": people[sender_role]["id"],
                "recipient_id": people[recipient_role]["id"],
                "occurred_at": occurred_at,
            }
    except (ValueError, TypeError) as error:
        values["unresolved_reason"] = str(error)
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


def _stream_rows(source, parameters, state):
    zone = _zone(parameters)
    phase = parameters.get("phase", "preview")
    if phase not in ("preview", "import"):
        raise AnalysisError("imazing_invalid_confirmation")
    people = _people(parameters, source.item.case) if phase == "import" else None
    limit = (
        parameters.get("_imazing_text_limit_bytes", _text_limit())
        if phase == "preview"
        else _text_limit()
    )
    if type(limit) is not int or not 0 < limit <= 1024 * 1024:
        raise AnalysisError("imazing_invalid_confirmation")
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
        "parser_version": PARSER_VERSION,
        "timezone": zone.key,
        "text_limit_bytes": limit,
        "incoming_rows": 0,
        "outgoing_rows": 0,
    }
    metrics = {"source_rows": 0, "ready_rows": 0, "unresolved_rows": 0}
    digest = hashlib.sha256()
    digest.update(
        canonical_bytes(
            {
                "parser_version": PARSER_VERSION,
                "timezone": zone.key,
                "text_limit_bytes": limit,
            }
        )
    )
    session_label = sender_identifier = ""
    sender_name_digests = []
    for number, start, end, raw in _raw_rows(source):
        session = raw["Chat Session"]
        if session:
            if session_label and session_label != session:
                raise AnalysisError("imazing_multiple_chats")
            session_label = session
            summary["chat_session"] = session[:255]
            summary["chat_session_truncated"] = len(session) > 255
        if raw["Type"] == "Incoming":
            summary["incoming_rows"] += 1
            identifier = raw["Sender ID"]
            if identifier:
                if sender_identifier and sender_identifier != identifier:
                    raise AnalysisError("imazing_multiple_chats")
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
        item = _row(source, number, start, end, raw, zone, limit, people)
        metrics["source_rows"] += 1
        metrics[
            "unresolved_rows" if "unresolved_reason" in item["values"] else "ready_rows"
        ] += 1
        _update_digest(digest, item)
        yield item
    summary["interpretation_sha256"] = digest.hexdigest()
    expected = parameters.get("_imazing_interpretation_sha256")
    if expected is not None and expected != summary["interpretation_sha256"]:
        raise AnalysisError("imazing_invalid_confirmation")
    state.update(imazing=summary, metrics=metrics)


def iter_import_rows(source, parameters):
    """Yield row items; exhaust to verify checksum, chat shape, and optional digest.

    Historical review may supply `_imazing_text_limit_bytes` and
    `_imazing_interpretation_sha256` from its retained compact summary. Import
    publication always uses the current operator text limit.
    """
    return _stream_rows(source, parameters, {})


def read_import_page(source, parameters, *, page, per_page=25):
    """Return one page, row count, and three samples after full source verification."""
    if (
        type(page) is not int
        or page < 1
        or type(per_page) is not int
        or not 1 <= per_page <= 100
    ):
        raise AnalysisError("imazing_invalid_confirmation")
    rows, samples = [], []
    start = (page - 1) * per_page
    total = 0
    for index, item in enumerate(iter_import_rows(source, parameters)):
        if index < 3:
            values = item["values"]
            samples.append(
                {
                    "text": item["text"][:240],
                    "values": {
                        "source_row": values["source_row"],
                        "direction": values["direction"][:255],
                        "raw_timestamp": values["raw_timestamp"][:255],
                        "unresolved_reason": values.get("unresolved_reason", ""),
                    },
                }
            )
        if start <= index < start + per_page:
            rows.append(item)
        total += 1
    return rows, total, samples


def prepare_import(sources, parameters):
    """Retain a compact summary and digest; never store a per-row result payload."""
    sources = list(sources)
    if len(sources) != 1 or sources[0].kind != "document" or sources[0].attachment:
        raise AnalysisError("imazing_invalid_format")
    state = {}
    # Retained display overrides belong only to read_import_page/iter_import_rows.
    parameters = {
        key: value
        for key, value in parameters.items()
        if not key.startswith("_imazing_")
    }
    for _ in _stream_rows(sources[0], parameters, state):
        pass
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
    data["imazing"] = state["imazing"]
    return data


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
                raise AnalysisError("imazing_conflict")
            continue
        person = Person(pk=identifier, case=case, **selected["create"])
        person.full_clean()
        person.save()


def apply_import(run, data, sources):
    """Publish bounded batches atomically; any failure rolls back the import."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Message CSV publication requires a transaction.")
    case = get_case(run.requested_by, run.case_id, edit=True, lock=True)
    if (
        run.operation != "imazing_import"
        or run.method_version != "2"
        or run.parameters.get("phase") != "import"
    ):
        raise AnalysisError("imazing_invalid_confirmation")
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
        raise AnalysisError("imazing_source_changed")
    try:
        preview = AnalysisRun.objects.select_related("result").get(
            pk=_identifier(run.parameters.get("preview_run_id")),
            case=case,
            operation="imazing_import",
            method_version="2",
            status__in=("complete", "partial"),
            result__case=case,
        )
    except AnalysisRun.DoesNotExist as error:
        raise AnalysisError("imazing_invalid_confirmation") from error
    if (
        preview.parameters.get("phase") != "preview"
        or len(preview.inputs) != 1
        or preview.inputs[0].get("evidence_id") != str(source.item.pk)
        or preview.inputs[0].get("sha256") != source.item.sha256
        or run.parameters.get("timezone") != preview.parameters.get("timezone")
    ):
        raise AnalysisError("imazing_invalid_confirmation")
    expected = prepare_import(sources, run.parameters)
    if _unpublished(data) != expected or expected["imazing"][
        "interpretation_sha256"
    ] != preview.result.data.get("imazing", {}).get("interpretation_sha256"):
        raise AnalysisError("imazing_invalid_confirmation")
    people = _people(run.parameters, case)
    contexts = CommunicationContext.objects.filter(source_document=source.item)
    scope = Q(case=case, message__in=scoped_evidence(case, "text-message"))
    if contexts.exclude(scope).exists():
        raise AnalysisError("imazing_conflict")
    contexts = contexts.filter(scope)
    if expected["metrics"]["ready_rows"]:
        _create_people(case, people, contexts)
    parameters = run.parameters | {
        "_imazing_interpretation_sha256": expected["imazing"]["interpretation_sha256"]
    }
    created = reused = 0
    for batch in _publication_batches(iter_import_rows(source, parameters)):
        existing_rows = {
            context.source_row: context
            for context in contexts.filter(
                source_row__in=[item["values"]["source_row"] for item in batch]
            ).select_related("message")
        }
        for item in batch:
            values = item["values"]
            prepared = values.get("import_message")
            if prepared is None:
                continue
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
                    raise AnalysisError("imazing_conflict")
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
                    f"row {values['source_row']}; {PARSER_VERSION}"
                ),
                submission_id=uuid5(source.item.pk, f"imazing:{values['source_row']}"),
            )
            message.full_clean()
            message.save()
            CommunicationContext.objects.create(
                case=case,
                message=message,
                source_document=source.item,
                import_run=run,
                **fields,
            )
            created += 1
    data["import_publication_version"] = 2
    data["metrics"].update(created_rows=created, reused_rows=reused)
    return created
