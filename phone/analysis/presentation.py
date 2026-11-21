"""Source-linked communications and import results."""

from itertools import groupby
from math import isfinite

from core.analysis.presentation import prioritize_measurements, table_rows
from core.evidence.types import KIND_LABELS

TEMPORAL_TEXTS = {
    "Recorded temporal adjacency",
    "Recorded temporal adjacency and shared wording",
}


def addition_label(source, operation):
    addition = source.attachment
    if addition is None:
        return ""
    has_transcript = getattr(addition, "has_transcript", None)
    if has_transcript is None:
        has_transcript = bool(addition.transcript)
    if (
        has_transcript
        and source.extracted_text is None
        and operation not in {"audio_metadata", "transcription"}
    ):
        return f"Supplied transcript · {addition.created_at:%d %b %Y %H:%M} UTC"
    return str(addition)


def _message(values, sources, formatter):
    source_id = values.get("source_id")
    source = sources.get(source_id) if isinstance(source_id, str) else None
    if source is None:
        return None
    return {
        "source": source,
        "channel": KIND_LABELS.get(values.get("channel"), source["kind_label"]),
        "time": values.get("occurred_at") or "Unknown",
        "time_value": formatter({"occurred_at": values.get("occurred_at")})[0],
        "text": values.get("text"),
        "participants": formatter(
            {key: values[key] for key in ("sender_id", "recipient_id") if key in values}
        ),
        "references": formatter(
            {
                key: value
                for key, value in values.items()
                if key
                not in {
                    "source_id",
                    "channel",
                    "occurred_at",
                    "sender_id",
                    "recipient_id",
                    "text",
                    "url",
                }
                and (
                    key not in {"native_message_id", "native_thread_id", "reply_to_id"}
                    or value
                )
            }
        ),
    }


def present(operation, items, formatter):
    for item in items:
        values = item.get("values", {})
        sources = {source["id"]: source for source in item["sources"]}
        if operation in {"sessions", "conversation_link"}:
            records = (
                (values.get("messages") or [])
                if operation == "sessions"
                else [values.get("before"), values.get("after")]
            )
            item["messages"] = [
                message
                for row in records
                if isinstance(row, dict)
                if (message := _message(row, sources, formatter)) is not None
            ]
            if operation == "sessions":
                item["messages"].sort(key=lambda message: message["time"])
            if item["messages"]:
                rendered = {message["source"]["id"] for message in item["messages"]}
                consumed = set()
                if operation == "sessions" and len(item["messages"]) == len(records):
                    consumed.add("Messages")
                elif operation == "conversation_link":
                    consumed.update(
                        key.title()
                        for key in ("before", "after")
                        if isinstance(values.get(key), dict)
                        and values[key].get("source_id") in rendered
                    )
                item["measurements"] = [
                    row for row in item["measurements"] if row["label"] not in consumed
                ]
                item["remaining_sources"] = [
                    source for source in item["sources"] if source["id"] not in rendered
                ]
                for message in item["messages"]:
                    excerpt = message["source"].get("excerpt")
                    if excerpt and excerpt.removesuffix("…") not in (
                        message["text"] or ""
                    ):
                        item["remaining_sources"].append(message["source"])
                item["message_details"] = [
                    message
                    for message in item["messages"]
                    if message["references"] or message["source"].get("anchors")
                ]
            elapsed = values.get("elapsed_seconds")
            temporal = (
                operation == "conversation_link"
                and item["label"] == "Candidate exchange sequence"
                and "status" not in values
                and type(elapsed) in (int, float)
                and isfinite(elapsed)
                and elapsed >= 0
                and len(item["messages"]) == 2
            )
            if temporal:
                subject = (
                    "Messages"
                    if all(
                        record.get("channel") == "text-message" for record in records
                    )
                    else "Communications"
                )
                unit = "second" if elapsed == 1 else "seconds"
                item["label"] = f"{subject} {elapsed:g} {unit} apart"
                if item.get("text") in TEMPORAL_TEXTS:
                    item["text"] = ""
                item["measurement_note"] = (
                    "Timing and shared wording do not establish a reply, shared event, "
                    "or causal connection."
                    if values.get("shared_words")
                    else "Timing alone does not establish a reply, shared event, "
                    "or causal connection."
                )
                # The concise caution replaces only the current method's exact wording.
                for row in item["measurements"]:
                    if row["label"] == "Interpretation" and row.get("text") == (
                        "Unresolved: proximity in time or subject wording does not "
                        "prove the same event, a reference, discussion content, "
                        "or causation."
                    ):
                        row["label"] = "Recorded interpretation"
            prioritize_measurements(
                item,
                "First recorded at",
                "Last recorded at",
                "Message count",
                *(("Elapsed seconds",) if not temporal else ()),
                *(("Shared words",) if values.get("shared_words") else ()),
            )
        elif operation in {"conversation_import", "imessage_import"}:
            mapped = values.get("import_message")
            prioritize_measurements(
                item,
                "Source row",
                "Raw sender",
                "Raw recipient",
                "Raw timestamp",
                "Unresolved reason",
            )
            if isinstance(mapped, dict):
                item.setdefault("primary_measurements", []).extend(
                    formatter(
                        {
                            key: mapped[key]
                            for key in ("sender_id", "recipient_id", "occurred_at")
                            if key in mapped
                        }
                    )
                )
            if not mapped and values.get("body_candidate"):
                item["candidate_body"] = values["body_candidate"]
        elif operation == "session_summaries":
            prioritize_measurements(item, "Inferred session", "Status", "Basis")
        else:
            prioritize_measurements(
                item,
                "Reported event time",
                "Reported sender",
                "Reported recipient",
                "Channel",
                "First recorded at",
                "Last recorded at",
                "Timed records",
                "Median response interval seconds",
                "Inferred session",
                "Status",
            )
    if operation == "comm_patterns":
        return [
            {
                "title": "Messages on this page"
                if records
                else "Timing and session comparisons",
                "items": list(group),
            }
            for records, group in groupby(
                items, lambda item: item["label"] == "Selected message"
            )
        ]
    return [
        {
            "items": items,
            "title": "Source-cited session suggestions"
            if operation == "session_summaries"
            else "",
        }
    ]


def report_table(data):
    items = sorted(
        data["items"], key=lambda item: item["values"]["Recorded call time (UTC)"]
    )
    return [
        {
            "items": items,
            "columns": table_rows(
                items,
                (
                    ("Recorded event", ("Recorded call time (UTC)",)),
                    ("Participants", ("Participant pair",)),
                    ("Duration", ("Reported duration (seconds)",)),
                ),
            ),
        }
    ]
