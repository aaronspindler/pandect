"""Reading views for retained image observations and candidate text."""

import json
from collections import defaultdict
from itertools import groupby

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.urls import reverse

from core.analysis.navigation import finding_url
from core.analysis.presentation import group_by_source, prioritize_measurements
from core.models import AnalysisArtifact
from image.analysis import MIN_OCR_WORD_SCORE


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
            group["sections"] = [
                {"tags": tags, "items": list(rows)}
                for tags, rows in groupby(
                    group["items"], lambda item: bool(item.get("metadata_field"))
                )
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


def previews_by_result(case, artifacts):
    previews = defaultdict(dict)
    for artifact in artifacts.filter(kind=AnalysisArtifact.Kind.IMAGE_PREVIEW).values(
        "id", "result_id", "result__run_id", "source_id", "metadata", "width", "height"
    ):
        key = (str(artifact["source_id"]), artifact["metadata"].get("finding_index"))
        previews[artifact["result_id"]][key] = {
            "url": reverse(
                "core:analysis:artifact",
                args=[case.pk, artifact["result__run_id"], artifact["id"]],
            ),
            "width": artifact["width"],
            "height": artifact["height"],
        }
    return previews


def finding_images(finding, index, previews):
    images = {}
    for source_id in finding.get("source_ids", []):
        if crop := previews.get((source_id, index)):
            images["prepared_preview"] = crop
            break
    for anchor in finding.get("anchors", []):
        preview = previews.get((anchor.get("source_id"), None))
        if preview and "region" in anchor:
            images["preview"] = preview | preview_rectangle(anchor["region"], preview)
            break
    return images


def preview_rectangle(region, preview):
    left, top, right, bottom = region
    return {
        "x": left * preview["width"],
        "y": top * preview["height"],
        "region_width": (right - left) * preview["width"],
        "region_height": (bottom - top) * preview["height"],
    }


def _layout_field(field, words, result, preview, history):
    cited = []
    for index in field["words"]:
        word = words[index]
        line = word["item"]
        cited.append(
            {
                "text": result.data["items"][line]["text"][word["start"] : word["end"]],
                "region": word["region"],
                "confidence": word["confidence"],
                "line_number": line + 1,
                "url": finding_url(
                    result.case_id, result.run_id, line, history=history
                ),
                "rect": preview_rectangle(word["region"], preview) if preview else None,
            }
        )
    return {
        "role": field["role"],
        "words": cited,
        "text": " ".join(word["text"] for word in cited),
        "confidence": field["confidence"],
        "low_score": field["confidence"] < MIN_OCR_WORD_SCORE,
    }


def visible_structure(result, sources, previews, query, history, selected_finding):
    """Read saved word spans in layout order, separately from ranked findings."""
    structures = result.data.get("visible_structure", {})
    source_ids = [source_id for source_id in sources if source_id in structures]
    if not source_ids:
        return {}
    if "layout" not in query:
        refs = selected_finding["source_ids"] if selected_finding else source_ids
        return {"first_source": next((ref for ref in refs if ref in source_ids), None)}
    selected = query["layout"]
    if selected not in source_ids:
        raise Http404("Layout source unavailable.")
    position = source_ids.index(selected)
    structure = structures[selected]
    candidates = structure["candidates"]
    orders = structure["orders"]
    order = next(
        (option for option in orders if option["label"] == query.get("layout_order")),
        orders[0] if orders else None,
    )
    indices = order["candidates"] if order else range(len(candidates))
    page = Paginator(indices, 8).get_page(query.get("layout_page"))
    preview = previews.get((selected, None))
    labels = {
        "block": "Text block",
        "table_row": "Table row",
        "form_field": "Form field",
        "message": "Apparent message",
    }
    entries = []
    for index in page:
        candidate = candidates[index]
        entries.append(
            {
                "label": labels[candidate["kind"]],
                "notes": candidate["notes"],
                "fields": [
                    _layout_field(field, structure["words"], result, preview, history)
                    for field in candidate["fields"]
                ],
            }
        )
    return {
        "source": sources[selected],
        "previous": source_ids[position - 1] if position else None,
        "next": source_ids[position + 1] if position + 1 < len(source_ids) else None,
        "page": page,
        "entries": entries,
        "has_messages": any(entry["kind"] == "message" for entry in candidates),
        "notes": structure["notes"],
        "orders": orders,
        "order": order,
        "preview": preview,
    }


def add_finding_previews(case, rows):
    """Read only the current page's crops and this image's source previews."""
    rows = [
        row
        for row in rows
        if not row["unavailable"] and row["operation"] in {"ocr", "plate_ocr"}
    ]
    if not rows:
        return
    artifacts = AnalysisArtifact.objects.filter(
        Q(metadata={})
        | Q(metadata__finding_index__in=[row["item_index"] for row in rows]),
        case=case,
        result__case=case,
        result__run__case=case,
        result_id__in={row["result_id"] for row in rows},
        source_id__in={row["source_id"] for row in rows},
    )
    previews = previews_by_result(case, artifacts)
    for row in rows:
        row["images"] = finding_images(
            row["raw_item"], row["item_index"], previews.get(row["result_id"], {})
        )
