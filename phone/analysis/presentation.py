"""Source-linked communications and import results."""

from core.analysis.presentation import prioritize_measurements, table_rows
from core.evidence.types import KIND_LABELS


def addition_label(source, operation):
    addition = source.attachment
    if addition is None:
        return ""
    if (
        addition.transcript
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
        "text": values.get("text"),
        "participants": formatter(
            {key: values[key] for key in ("sender_id", "recipient_id") if key in values}
        ),
        "references": formatter(
            {
                key: values[key]
                for key in ("native_message_id", "native_thread_id", "reply_to_id")
                if values.get(key)
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
                    item["measurements"] = [
                        row
                        for row in item["measurements"]
                        if row["label"] != "Messages"
                    ]
            prioritize_measurements(
                item,
                "First recorded at",
                "Last recorded at",
                "Message count",
                "Elapsed seconds",
                "Shared words",
                "Basis",
            )
        elif operation == "conversation_import":
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
        records = [item for item in items if item["label"] == "Selected message"]
        records.sort(
            key=lambda item: item.get("values", {}).get("occurred_at") or "Unknown"
        )
        return [
            {"title": "Messages on this page", "items": records},
            {
                "title": "Timing and session comparisons",
                "items": [
                    item for item in items if item["label"] != "Selected message"
                ],
            },
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
