"""Recording properties and time intervals from the retained analysis."""

from core.analysis.presentation import group_by_source, prioritize_measurements


def present(operation, items, formatter):
    for item in items:
        prioritize_measurements(
            item,
            "Model uncertainty",
            "Coverage",
            "Status",
            "Full-scale sample fraction",
            "Decoded duration (seconds)",
            "Decoded sample rate (Hz)",
            "Decoded channel count",
            "Reported call duration is not a recording duration",
        )
        if operation == "audio_metadata" and "Decoded duration (seconds)" in item.get(
            "values", {}
        ):
            item["metadata_rows"] = (
                item.get("primary_measurements", []) + item["measurements"]
            )
        item["media_details"] = True
    groups = group_by_source(items)
    for group in groups:
        group["items"].sort(
            key=lambda item: (item.get("start_seconds", -1), item["index"])
        )
        group["recording_url"] = next(
            (
                item["recording_url"]
                for item in group["items"]
                if item.get("recording_url")
            ),
            None,
        )
    return groups
