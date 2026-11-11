"""Reading views for retained image observations and candidate text."""

import json
from itertools import groupby

from core.analysis.presentation import group_by_source, prioritize_measurements


def present(operation, items, formatter):
    groups = group_by_source(items)
    if operation == "image_metadata":
        for item in items:
            if item["label"].startswith("Raw metadata · "):
                item["metadata_field"] = item["label"].removeprefix("Raw metadata · ")
                try:
                    raw = json.loads(item.get("text", ""))
                except ValueError:
                    continue
                item["text"] = (
                    raw.replace("\x00", r"\u0000")
                    if isinstance(raw, str)
                    else json.dumps(raw, ensure_ascii=False, indent=2)
                )
            else:
                prioritize_measurements(
                    item,
                    "Decoded format",
                    "Decoded width",
                    "Decoded height",
                    "Color mode",
                    "Capture time candidate",
                    "GPS candidate",
                )
        for item in items:
            item["metadata_rows"] = item.get("primary_measurements", []) + sorted(
                item["measurements"], key=lambda row: row["label"]
            )
        for group in groups:
            group["tags"] = [
                item for item in group["items"] if item.get("metadata_field")
            ]
            group["items"] = [
                item for item in group["items"] if not item.get("metadata_field")
            ]
    elif operation == "plate_ocr":
        for group in groups:
            group["regions"] = [
                {"number": number, "items": list(readings)}
                for number, readings in groupby(
                    group["items"],
                    key=lambda item: item.get("values", {}).get("Plate region number"),
                )
            ]
        for item in items:
            prioritize_measurements(
                item,
                "Recognition status",
                "Minimum word confidence (0–100)",
                "Short OCR output",
            )
    else:
        for group in groups:
            group["preview"] = next(
                (item["preview"] for item in group["items"] if item.get("preview")),
                None,
            )
            group["regions"] = [item for item in group["items"] if item.get("preview")]
        for item in items:
            prioritize_measurements(item, "Minimum word confidence (0–100)", "Status")
    if operation != "image_metadata":
        for item in items:
            item["media_details"] = True
    return groups
