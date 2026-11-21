"""Readable measurements and source examples; full technical data stays retained."""

import hashlib
import json
import re
from itertools import groupby
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware

from document.models import DocumentEvidence
from people.models import Person
from vehicle.models import Vehicle

UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
REPORTED_PARTICIPANT = re.compile(
    r"\b(?P<article>the\s+)?(?P<prefix>reported\s+(?P<role>sender|recipient)\s+)"
    r"\*{0,2}(?P<id>" + UUID.pattern + r")\*{0,2}",
    re.I,
)
INTERNAL = {
    "source_id",
    "source_ids",
    "source_document_id",
    "source_sha256",
    "source_context",
    "url",
    "parser_version",
    "extraction_ranges",
    "derived_result_id",
    "import_message",
    "contributing_source_ids",
    "automatic_batch",
    "automatic_followup",
    "import_run_id",
    "batch_id",
}
TECHNICAL = {
    "parser",
    "method",
    "input sha-256",
    "input_sha256",
    "audio sha-256",
    "record sha-256",
}


def participant_names(sources):
    """Use only explicit participants of already validated communication sources."""
    names = {}
    for source in sources:
        if source.kind not in {"text-message", "phone-call", "facetime"}:
            continue
        for role in ("sender", "recipient"):
            person = getattr(source.item, role)
            if person.case_id == source.item.case_id:
                names[(role, str(person.pk))] = str(person)
    return names


def participant_text(text, names, *, include_sender_role=True):
    """Display explicit reported-participant references without rewriting results."""

    def display(match):
        role = match["role"].lower()
        name = names.get((role, match["id"].lower()))
        if name is None:
            return match[0]
        if role == "sender" and not include_sender_role:
            return name
        return (match["article"] or "") + match["prefix"] + name

    return REPORTED_PARTICIPANT.sub(display, text)


def measurement_timestamp(key, value):
    """Interpret structured time fields, preserving quoted and raw source values."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    if not (
        "(utc" in key
        or key.endswith(("_at", "_time", "_observed", "_as_of"))
        or key
        in {
            "period_a_start",
            "period_a_end",
            "period_b_start",
            "period_b_end",
            "first_usable_observation",
            "last_usable_observation",
            "last_usable_observation_before_gap",
            "first_usable_observation_after_gap",
            "window_start_(inclusive)",
            "window_end_(exclusive)",
        }
    ):
        return None
    try:
        stamp = parse_datetime(value)
    except ValueError:
        return None
    return stamp if stamp is not None and is_aware(stamp) else None


def label(value):
    labels = {
        "group_a": "First sample",
        "group_b": "Second sample",
        "difference_b_minus_a": "Second minus first",
        "sender_id": "Reported sender",
        "recipient_id": "Reported recipient",
        "person_id": "Person",
        "vehicle_id": "Vehicle",
        "occurred_at": "Reported event time",
        "counts_by_sender_id": "Counts by reported sender",
        "source_measurement": "Sample measurement",
        "usable_source_words": "Usable words",
        "native_message_id": "Native message ID",
        "native_thread_id": "Native thread ID",
        "reply_to_id": "Native reply reference",
    }
    normalized = str(value).lower().replace(" ", "_")
    if normalized in labels:
        return labels[normalized]
    value = str(value).replace("_", " ")
    return value[:1].upper() + value[1:]


def measurement_formatter(data, *, case, sources=(), query=None):
    source_map = {str(source.id): source for source in sources}
    serialized = json.dumps(data, default=str)
    ids = list(dict.fromkeys(UUID.findall(serialized)))[:2000]
    names = {}
    for person in Person.objects.filter(case=case, pk__in=ids):
        names[str(person.pk)] = str(person)
        names[f"person:{person.pk}"] = str(person)
    for vehicle in Vehicle.objects.filter(case=case, pk__in=ids):
        names[str(vehicle.pk)] = str(vehicle)
        names[f"vehicle:{vehicle.pk}"] = str(vehicle)
    for source in sources:
        names[str(source.id)] = source.item.title
        names[str(source.item.pk)] = source.item.title
    if '"native_thread_references":' in serialized:
        exports = DocumentEvidence.objects.filter(
            case=case,
            pk__in=ids,
            communication_contexts__case=case,
            communication_contexts__message_id__in=[
                source.item.pk for source in sources if source.kind == "text-message"
            ],
        ).distinct()
        for document_id, title in exports.values_list("pk", "title"):
            names[str(document_id)] = title

    def scalar(value, *, reference=False, native_thread=False):
        if value is None:
            return "Unknown"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if native_thread and isinstance(value, str):
            document_id, separator, thread_id = value.partition(":")
            if separator and UUID.fullmatch(document_id):
                export = names.get(document_id, "Unavailable export")
                return f"{export} · Thread {thread_id}"
        return names.get(str(value), str(value)) if reference else str(value)

    def render(values, *, duplicate_text=""):
        root = hashlib.sha256(
            json.dumps(values, default=str, sort_keys=True).encode()
        ).hexdigest()[:12]

        def value_page(collection, path):
            key = (
                "values_"
                + root
                + "_"
                + hashlib.sha256(str(path).encode()).hexdigest()[:8]
            )
            page = Paginator(collection, 200).get_page((query or {}).get(key))
            links = {"page": page, "anchor": key}
            for name, number in (
                (
                    "previous",
                    page.previous_page_number() if page.has_previous() else None,
                ),
                ("next", page.next_page_number() if page.has_next() else None),
            ):
                if number is not None:
                    params = query.copy() if query is not None else {}
                    params[key] = number
                    links[name] = (
                        "?"
                        + (
                            params.urlencode()
                            if hasattr(params, "urlencode")
                            else urlencode(params)
                        )
                        + "#"
                        + key
                    )
            return page, links

        def rows(
            mapping, depth=0, reference_keys=False, reference_values=False, path=()
        ):
            result = []
            mapping_page, mapping_links = value_page(list(mapping.items()), path)
            for key, value in mapping_page:
                normalized = str(key).lower().replace(" ", "_")
                if normalized in INTERNAL or str(key).lower() in TECHNICAL:
                    continue
                if normalized == "text" and value == duplicate_text:
                    continue
                reference = reference_values or normalized in {
                    "sender_id",
                    "recipient_id",
                    "person_id",
                    "vehicle_id",
                    "subject",
                    "subject_association",
                    "subject_a",
                    "subject_b",
                    "group_a",
                    "group_b",
                    "break_before",
                    "join_before",
                    "source_order",
                    "participant_filter",
                    "other_participant_filter",
                }
                display_key = names.get(str(key), key) if reference_keys else key
                row = {"label": label(display_key)}
                row["display_label"] = (
                    row["label"].replace(" (UTC)", "").replace("UTC, ", "")
                )
                if depth >= 5:
                    row["text"] = "See the downloaded result for the complete detail."
                elif isinstance(value, dict):
                    row["children"] = rows(
                        value,
                        depth + 1,
                        reference_keys=normalized
                        in {
                            "counts_by_sender_id",
                            "apparent_session_initiations_by_sender",
                            "subjects",
                            "contexts",
                            "source_contexts",
                            "exclusions",
                        },
                        reference_values=normalized in {"sender_map", "recipient_map"},
                        path=(*path, key),
                    )
                elif isinstance(value, list):
                    entries, links = value_page(value, (*path, key))
                    if entries.has_other_pages():
                        row["pagination"] = links
                    if not value:
                        row["text"] = "None observed"
                    elif all(not isinstance(entry, (dict, list)) for entry in value):
                        row["text"] = ", ".join(
                            scalar(
                                entry,
                                reference=reference,
                                native_thread=normalized == "native_thread_references",
                            )
                            for entry in entries
                        )
                    else:
                        row["examples"] = []
                        for index, entry in enumerate(
                            entries, start=entries.start_index()
                        ):
                            if not isinstance(entry, dict):
                                row["examples"].append({"text": scalar(entry)})
                                continue
                            source = source_map.get(str(entry.get("source_id", "")))
                            example = {
                                "quote": entry.get("excerpt") or entry.get("text"),
                                "children": rows(
                                    {
                                        k: v
                                        for k, v in entry.items()
                                        if k not in {"excerpt", "text", "start", "end"}
                                    },
                                    depth + 1,
                                    path=(*path, key, index),
                                ),
                            }
                            if source:
                                example |= {
                                    "source_label": source.item.title,
                                    "url": source.url,
                                }
                                if "start" in entry and "end" in entry:
                                    example["position"] = (
                                        f"Characters {entry['start']}–{entry['end']}"
                                    )
                            row["examples"].append(example)
                else:
                    row["text"] = scalar(value, reference=reference)
                    row["timestamp"] = measurement_timestamp(normalized, value)
                if row.get("children") == []:
                    row["text"] = "No additional recorded values"
                result.append(row)
            if mapping_page.has_other_pages():
                result.append(
                    {
                        "label": "Value pages",
                        "pagination": mapping_links,
                    }
                )
            return result

        return rows(values)

    return render


def group_by_source(items):
    """Share a source heading only between adjacent findings in review order."""
    groups = []
    for _, group in groupby(
        items, key=lambda item: (item["sources"] or [{}])[0].get("id")
    ):
        rows = list(group)
        groups.append({"source": (rows[0]["sources"] or [{}])[0], "items": rows})
    return groups


def prioritize_measurements(item, *labels):
    """Promote available fields in task order and keep the rest inspectable."""
    if not labels:
        return
    measurements = item["measurements"]
    primary = [row for key in labels for row in measurements if row["label"] == key]
    if primary:
        item.setdefault("primary_measurements", []).extend(primary)
        item["measurements"] = [
            row for row in measurements if row["label"] not in labels
        ]


def table_rows(items, columns):
    """Reports use an explicit list of retained keys in each column."""
    displayed = {key for _, keys in columns for key in keys}
    for item in items:
        measurements = {row["label"]: row for row in item["measurements"]}
        item["cells"] = [
            [measurements[key] for key in keys if key in measurements]
            for _, keys in columns
        ]
        item["measurements"] = [
            row for row in item["measurements"] if row["label"] not in displayed
        ]
    return [title for title, _ in columns]
