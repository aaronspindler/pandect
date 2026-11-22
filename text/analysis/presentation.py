"""Compact text findings and side-by-side source comparisons."""

from itertools import groupby

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
        elif operation == "claim_comparison":
            prioritize_measurements(
                item,
                "Reason",
                "Source independence",
                "Text basis",
                "Reported sender",
                "Reported record time",
            )
        elif operation == "entities" and values.get("candidate") != item.get(
            "text", ""
        ):
            prioritize_measurements(item, "Candidate")
        elif operation == "style_profiles":
            prioritize_measurements(
                item, "Usable words", "Sentences", "Vocabulary ratio"
            )
        if operation == "style_change":
            prioritize_measurements(
                item,
                *(
                    label
                    for key, label in (
                        ("supporting_examples", "Supporting examples"),
                        ("contrary_examples", "Contrary examples"),
                    )
                    if values.get(key)
                ),
            )

    if operation == "question_synthesis":
        titles = {
            "supported": "Supported observations · AI suggestions",
            "contrary": "Contrary evidence · AI suggestions",
        }
        return [
            {
                "title": titles.get(section, "Other retained suggestions"),
                "items": list(group),
            }
            for section, group in groupby(
                items, lambda item: item.get("values", {}).get("section")
            )
        ]
    if operation == "text_topics":
        return [
            {
                "title": "Observed keywords" if keywords else "Topic suggestions",
                "items": list(group),
                "table": keywords,
            }
            for keywords, group in groupby(
                items, lambda item: "occurrences" in item.get("values", {})
            )
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
