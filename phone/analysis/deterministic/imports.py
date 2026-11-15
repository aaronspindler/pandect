"One bounded iMessage HTML profile with explicit participant interpretation."

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from cases.permissions import get_case
from core.analysis.sources import output
from core.evidence.queries import scoped_evidence
from people.models import Person
from phone.models import CommunicationContext, TextMessageEvidence

PARSER_VERSION = "imessage-html-v3"
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500


@dataclass
class _Node:
    tag: str
    attrs: dict
    start: int
    end: int = 0
    children: list = field(default_factory=list)

    def classes(self):
        return set(self.attrs.get("class", "").split())

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        if self.tag == "br":
            return "\n"
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def descendants(self):
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.descendants()


class _ExportParser(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.original = text
        # HTMLParser increments its line number only at LF, not every Unicode
        # line separator recognized by str.splitlines().
        self.offsets = [0] + [
            index + 1 for index, char in enumerate(text) if char == "\n"
        ]
        self.root = _Node("root", {}, 0, len(text))
        self.stack = [self.root]
        self.nodes = 0

    def _offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 20_000 or len(self.stack) >= 40:
            raise ValidationError("The HTML export exceeds its node or nesting limit.")
        # HTMLParser reports valueless attributes as None. Treat them as empty
        # values while retaining their presence in the source context.
        node = _Node(tag, {name: value or "" for name, value in attrs}, self._offset())
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)
        else:
            node.end = node.start + len(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop().end = self._offset() + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop().end = self.original.index(">", self._offset()) + 1
        elif tag not in {"br", "img", "hr", "meta", "input", "link"}:
            raise ValidationError(
                "The supported export profile requires balanced message markup."
            )

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse_time(raw, zone):
    value = parse_datetime(raw)
    if value is None:
        # The inspected iMessageLLM profile displays this timestamp shape.
        value = datetime.strptime(
            raw.split("(", 1)[0].strip(), "%b %d, %Y  %I:%M:%S %p"
        )
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    candidates = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value:
            candidates.append(candidate)
    if not candidates or len({c.utcoffset() for c in candidates}) != 1:
        raise ValueError("Ambiguous or nonexistent local timestamp")
    return candidates[0].astimezone(UTC)


def _field(node, name, *, required=True):
    fields = [c for c in node.descendants() if name in c.classes()]
    if (required and len(fields) != 1) or len(fields) > 1:
        raise ValueError(f"Expected exactly one {name} field")
    return fields[0].text() if fields else ""


def _metadata(node):
    attrs = node.attrs
    for key in ("data-message-id", "data-thread-id", "data-reply-to"):
        if len(attrs.get(key, "")) > 255:
            raise ValueError("A native source identifier exceeds the supported limit")
    participants = []
    if attrs.get("data-participants"):
        participants = json.loads(attrs["data-participants"])
        if (
            not isinstance(participants, list)
            or len(participants) > 100
            or any(
                not isinstance(x, str) or len(x) > 255 or "\x00" in x
                for x in participants
            )
        ):
            raise ValueError("Invalid source participant labels")
    attachments = []
    for child in node.descendants():
        if "attachment" in child.classes():
            attachments.append(
                {
                    "href": child.attrs.get("href", child.attrs.get("src", ""))[:2000],
                    "label": child.text()[:255],
                    "availability": "not ingested; source reference only",
                }
            )
    if len(attachments) > 50:
        raise ValueError("Too many attachment references")
    return {
        "native_message_id": attrs.get("data-message-id", "")[:255],
        "native_thread_id": attrs.get("data-thread-id", "")[:255],
        "reply_to_id": attrs.get("data-reply-to", "")[:255],
        "participants": participants,
        "account_identifiers": {
            k: attrs[k] for k in ("data-account", "data-service") if k in attrs
        },
        "attachments": attachments,
        "source_context": {
            k: v
            for k, v in attrs.items()
            if k
            in {
                "data-reaction",
                "data-edited",
                "data-deleted",
                "data-forwarded",
                "data-group-id",
            }
        },
    }


def _mapping(parameters, case):
    sender_map = parameters.get("sender_map", {})
    recipient_map = parameters.get("recipient_map", {})
    if (
        not isinstance(sender_map, dict)
        or not isinstance(recipient_map, dict)
        or len(sender_map) > 100
        or len(recipient_map) > 100
    ):
        raise ValidationError(
            "Supply explicit sender_map and recipient_map source-label mappings."
        )
    ids = set()
    normalized = []
    for mapping in (sender_map, recipient_map):
        labels = {}
        for label, person_id in mapping.items():
            if not isinstance(label, str) or not 1 <= len(label) <= 255:
                raise ValidationError(
                    "Each source participant label needs 1–255 characters."
                )
            try:
                identifier = UUID(str(person_id))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValidationError(
                    "Map source labels to existing case people."
                ) from error
            labels[label] = str(identifier)
            ids.add(identifier)
        normalized.append(labels)
    if Person.objects.filter(case=case, pk__in=ids).count() != len(ids):
        raise ValidationError(
            "A participant mapping refers to a person outside this case."
        )
    return tuple(normalized)


def prepare_import(sources, parameters):
    sources = list(sources)
    if len(sources) != 1 or sources[0].kind != "document" or sources[0].attachment:
        raise ValidationError("Select one retained HTML Document export.")
    source = sources[0]
    if parameters.get("profile", "imessage-html-v1") != "imessage-html-v1":
        raise ValidationError("Choose the supported imessage-html-v1 profile.")
    try:
        zone = ZoneInfo(parameters.get("timezone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValidationError("Choose an explicit IANA source timezone.") from error
    sender_map, recipient_map = _mapping(parameters, source.item.case)
    try:
        with source.item.file.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except (OSError, ValueError) as error:
        raise ValidationError("The retained source export is unavailable.") from error
    if len(raw) > MAX_BYTES:
        raise ValidationError("HTML exports are limited to 2 MiB per import.")
    if hashlib.sha256(raw).hexdigest() != source.item.sha256:
        raise ValidationError(
            "The retained export checksum does not match its source record."
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValidationError("The supported HTML export must be UTF-8.") from error
    if "\x00" in text:
        raise ValidationError("The HTML export contains unsupported null characters.")
    parser = _ExportParser(text)
    parser.feed(text)
    parser.close()
    if len(parser.stack) != 1:
        raise ValidationError("The HTML export has incomplete markup.")
    nodes = [
        node
        for node in parser.root.descendants()
        if node.tag == "div" and "message" in node.classes()
    ]
    if not nodes or len(nodes) > MAX_ROWS:
        raise ValidationError(
            "The supported profile needs 1–500 div.message records;"
            " split larger exports before retaining them."
        )
    items = []
    errors = 0
    for row, node in enumerate(nodes, 1):
        values = {
            "source_row": row,
            "source_start": node.start,
            "source_end": node.end,
            "parser_version": PARSER_VERSION,
            "timezone": zone.key,
            "source_document_id": str(source.item.pk),
            "source_sha256": source.item.sha256,
        }
        try:
            if any("message" in x.classes() for x in node.descendants()):
                raise ValueError("Nested message records are unsupported")
            sender = _field(node, "sender").strip()
            timestamp = _field(node, "timestamp").strip()
            body = _field(node, "bubble")
            recipient_label = _field(node, "recipient", required=False).strip()
            if (
                not sender
                or len(sender) > 255
                or not body
                or len(body.encode()) > 1024 * 1024
            ):
                raise ValueError("Missing sender, empty message, or oversized body")
            if len(timestamp) > 255:
                raise ValueError("The source timestamp exceeds 255 characters")
            metadata = _metadata(node)
            values |= {
                "raw_sender": sender,
                "raw_recipient": recipient_label,
                "raw_timestamp": timestamp,
                "body_candidate": body,
                **metadata,
            }
            event = _parse_time(timestamp, zone) if timestamp else None
            sender_id = sender_map.get(sender)
            # A displayed recipient can be mapped by its own label; otherwise require
            # an explicit per-sender recipient selection. Me/Them is never guessed.
            recipient_id = (
                sender_map.get(recipient_label)
                if recipient_label
                else recipient_map.get(sender)
            )
            if not sender_id or not recipient_id:
                raise ValueError(
                    "Unresolved source-label mapping; supply explicit "
                    "sender and recipient selections"
                )
            values["import_message"] = {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "occurred_at": event.isoformat() if event else None,
            }
            del values["body_candidate"]
            items.append(
                {
                    "label": f"Message row {row}",
                    "text": body,
                    "source_ids": [str(source.id)],
                    "anchors": [
                        source.anchor(
                            source_row=row, html_start=node.start, html_end=node.end
                        )
                    ],
                    "values": values,
                }
            )
        except (ValueError, TypeError) as error:
            errors += 1
            values["unresolved_reason"] = str(error)
            items.append(
                {
                    "label": f"Unresolved message row {row}",
                    "text": (
                        "This source row needs review before it can create a message."
                    ),
                    "source_ids": [str(source.id)],
                    "anchors": [
                        source.anchor(
                            source_row=row, html_start=node.start, html_end=node.end
                        )
                    ],
                    "values": values,
                }
            )
    return output(
        "Prepared supported export rows for atomic import confirmation.",
        items=items,
        metrics={
            "source_rows": len(nodes),
            "ready_rows": len(nodes) - errors,
            "unresolved_rows": errors,
            "parser_version": PARSER_VERSION,
        },
        warnings=[
            (
                "The original HTML remains the retained source. "
                "Extracted message bodies are decoded visible text, not"
                " original database message bytes. Scripts are never "
                "executed; links/attachment paths are retained "
                "references and never fetched."
            ),
            (
                "The basic inspected profile supplies "
                "timestamp/sender/bubble fields. Tested optional data "
                "attributes preserve native context only when actually "
                "present. Missing IDs, replies, groups, and account "
                "labels stay unknown; a deletion marker does not "
                "recover deleted content."
            ),
        ],
        outcome="partial" if errors else "complete",
    )


def apply_import(run, data, sources):
    "Materialize prepared rows only inside the caller's publication transaction."
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Communication import publication requires a transaction.")
    case = get_case(run.requested_by, run.case_id, edit=True, lock=True)
    if len(sources) != 1 or sources[0].item.case_id != case.pk:
        raise ValidationError("Import source is unavailable in this case.")
    source = sources[0]
    if source.item.sha256 != run.inputs[0]["sha256"]:
        raise ValidationError("Import source changed after selection.")
    sender_map, recipient_map = _mapping(run.parameters, case)
    contexts = CommunicationContext.objects.filter(source_document=source.item)
    scope = Q(case=case, message__in=scoped_evidence(case, "text-message"))
    if contexts.exclude(scope).exists():
        raise ValidationError("The export has an inconsistent retained message link.")
    existing_rows = {
        context.source_row: context
        for context in contexts.filter(scope).select_related("message").order_by()
    }
    created = 0
    data["import_publication_version"] = 1
    for item in data.get("items", []):
        values = item.get("values", {})
        prepared = values.get("import_message")
        if not prepared:
            continue
        if (
            values.get("source_document_id") != str(source.item.pk)
            or values.get("source_sha256") != source.item.sha256
        ):
            raise ValidationError(
                "A prepared row does not belong to the selected export."
            )
        raw_sender = values["raw_sender"]
        raw_recipient = values["raw_recipient"]
        recipient_id = (
            sender_map.get(raw_recipient)
            if raw_recipient
            else recipient_map.get(raw_sender)
        )
        if (
            sender_map.get(raw_sender) != prepared["sender_id"]
            or recipient_id != prepared["recipient_id"]
        ):
            raise ValidationError(
                "The prepared participant mapping no longer agrees with confirmation."
            )
        body = item["text"]
        existing = existing_rows.get(values["source_row"])
        if existing:
            message = existing.message
            if (
                message.body != body
                or str(message.sender_id) != prepared["sender_id"]
                or str(message.recipient_id) != prepared["recipient_id"]
                or (message.occurred_at.isoformat() if message.occurred_at else None)
                != prepared["occurred_at"]
            ):
                raise ValidationError(
                    "This source row was already imported with a "
                    "conflicting interpretation. Review the retained "
                    "message; do not replace it."
                )
            values["import_disposition"] = "already_retained"
            values["message_id"] = str(message.pk)
            continue
        submission_id = uuid5(
            NAMESPACE_URL, f"pandect:imessage:{source.item.pk}:{values['source_row']}"
        )
        message = TextMessageEvidence(
            case=case,
            uploaded_by=run.requested_by,
            title=f"Imported message · row {values['source_row']}",
            body=body,
            sender_id=prepared["sender_id"],
            recipient_id=prepared["recipient_id"],
            occurred_at=parse_datetime(prepared["occurred_at"])
            if prepared["occurred_at"]
            else None,
            source=source.item.source,
            source_description=(
                f"Retained HTML export {source.item.pk}, "
                f"row {values['source_row']}; {PARSER_VERSION}"
            ),
            submission_id=submission_id,
        )
        message.full_clean()
        message.save()
        values["import_disposition"] = "created"
        values["message_id"] = str(message.pk)
        fields = {
            name: values[name]
            for name in (
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
        }
        existing_rows[values["source_row"]] = CommunicationContext.objects.create(
            case=case, message=message, source_document=source.item, **fields
        )
        created += 1
    return created
