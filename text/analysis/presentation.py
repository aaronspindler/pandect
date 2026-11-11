"""Compact text findings and side-by-side source comparisons."""

from core.analysis.presentation import prioritize_measurements


def present(operation, items, formatter):
    for item in items:
        values = item.get("values", {})
        if operation == "copied_text":
            item["comparison_sources"] = [
                source for source in item["sources"] if source.get("excerpt")
            ]
            prioritize_measurements(item, "Token alignment ratio", "Basis")
        elif operation == "style_change" and {"group_a", "group_b"} <= values.keys():
            prioritize_measurements(
                item, "First sample", "Second sample", "Second minus first"
            )
            item["comparison"] = True
        elif operation == "style_change":
            prioritize_measurements(
                item, "Group a occurrences", "Group b occurrences", "Start", "End"
            )
        elif operation in {"content_events", "text_topics"}:
            prioritize_measurements(item, "Status", "Basis", "Occurrences")
        elif operation == "entities" and values.get("candidate") != item.get(
            "text", ""
        ):
            prioritize_measurements(item, "Candidate")
        elif operation == "style_profiles":
            prioritize_measurements(
                item, "Usable words", "Sentences", "Vocabulary ratio"
            )

    if operation == "text_topics":
        keywords = [item for item in items if "occurrences" in item.get("values", {})]
        suggestions = [
            item for item in items if "occurrences" not in item.get("values", {})
        ]
        return [
            {"title": "Observed keywords", "items": keywords, "table": True},
            {"title": "Topic suggestions", "items": suggestions},
        ]
    return [
        {
            "items": items,
            "table": operation == "entities",
            "title": "Source-cited suggestions"
            if operation == "content_events"
            else "",
        }
    ]
