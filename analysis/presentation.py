"""Readable measurements and source examples; full technical data stays retained."""

import json
import re

from people.models import Person
from vehicles.models import Vehicle

UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
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
}
TECHNICAL = {
    "parser",
    "method",
    "input sha-256",
    "input_sha256",
    "audio sha-256",
    "record sha-256",
}


def label(value):
    labels = {
        "group_a": "First sample",
        "group_b": "Second sample",
        "difference_b_minus_a": "Second minus first",
        "sender_id": "Reported sender",
        "recipient_id": "Reported recipient",
        "occurred_at": "Reported event time",
        "counts_by_sender_id": "Counts by reported sender",
        "source_measurement": "Sample measurement",
        "usable_source_words": "Usable words",
        "native_message_id": "Native message ID",
        "native_thread_id": "Native thread ID",
        "reply_to_id": "Native reply reference",
    }
    if str(value) in labels:
        return labels[str(value)]
    value = str(value).replace("_", " ")
    return value[:1].upper() + value[1:]


def measurement_formatter(data, *, case, sources=()):
    source_map = {str(source.id): source for source in sources}
    ids = list(dict.fromkeys(UUID.findall(json.dumps(data, default=str))))[:2000]
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

    def scalar(value, *, reference=False):
        if value is None:
            return "Unknown"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        return names.get(str(value), str(value)) if reference else str(value)

    def render(values, *, duplicate_text=""):
        def rows(mapping, depth=0, reference_keys=False, reference_values=False):
            result = []
            for key, value in list(mapping.items())[:200]:
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
                }
                display_key = names.get(str(key), key) if reference_keys else key
                row = {"label": label(display_key)}
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
                    )
                elif isinstance(value, list):
                    if not value:
                        row["text"] = "None observed"
                    elif all(not isinstance(entry, (dict, list)) for entry in value):
                        row["text"] = ", ".join(
                            scalar(entry, reference=reference) for entry in value[:200]
                        )
                    else:
                        row["examples"] = []
                        for entry in value[:200]:
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
                    if len(value) > 200:
                        row["note"] = (
                            "First 200 values shown; the download retains all values."
                        )
                else:
                    row["text"] = scalar(value, reference=reference)
                if row.get("children") == []:
                    row["text"] = "No additional recorded values"
                result.append(row)
            if len(mapping) > 200:
                result.append(
                    {
                        "label": "Further values",
                        "text": "The download retains all remaining values.",
                    }
                )
            return result

        return rows(values)

    return render
