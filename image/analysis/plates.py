"""Source-linked local plate search; readings remain unverified suggestions."""

from core.analysis.errors import AnalysisError
from core.analysis.sources import output, source_item, unsupported_source
from core.parsers import read_source, run_parser

MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def plate_ocr(sources, parameters, *, authorize):
    if len(sources) != 1 or sources[0].kind != "image" or sources[0].derived_result_id:
        raise AnalysisError("plate_image_required")
    source = sources[0]
    content, checksum = read_source(source)
    authorize()
    parsed = run_parser("plate_ocr", content)
    authorize()
    if parsed["status"] == "unsupported":
        return output(
            "This image format cannot be searched for plates.",
            items=[unsupported_source(source, parsed["reason"])],
            outcome="unsupported",
        )
    items, artifacts = [], []
    preview_bytes = omitted = readings = 0

    def preview(encoded, width, height, *, finding_index=None):
        nonlocal preview_bytes, omitted
        if not encoded:
            omitted += 1
            return
        size = len(encoded) * 3 // 4 - (len(encoded) - len(encoded.rstrip("=")))
        if preview_bytes + size > MAX_PREVIEW_BYTES:
            omitted += 1
            return
        artifact = {
            "source_id": source.id,
            "content_type": "image/jpeg",
            "data_base64": encoded,
            "width": width,
            "height": height,
        }
        if finding_index is not None:
            artifact["metadata"] = {"finding_index": finding_index}
        artifacts.append(artifact)
        preview_bytes += size

    preview(parsed["image"], parsed["prepared_width"], parsed["prepared_height"])
    for region_index, region in enumerate(parsed["candidates"], 1):
        for attempt_index, attempt in enumerate(region["attempts"], 1):
            text = attempt["text"]
            # The single-line plate profile needs at least three letters/digits.
            # Preserve shorter engine output as context, never invent characters.
            candidate = (
                sum(character.isascii() and character.isalnum() for character in text)
                >= 3
            )
            readings += candidate
            values = {
                "Recognition status": "Unverified plate text candidate"
                if candidate
                else "No plate-length text recognized",
                "OCR engine": parsed["engine"],
                "Detection method": parsed["detector"],
                "Preparation parser": parsed["version"],
                "Input SHA-256": checksum,
                "Recognition JPEG SHA-256": attempt["image_sha256"],
                "Original pixel width": parsed["width"],
                "Original pixel height": parsed["height"],
                "Original pixel corners (TL, TR, BR, BL)": [
                    [x * parsed["width"], y * parsed["height"]]
                    for x, y in region["corners"]
                ],
                "Rectified pixel size": region["rectified_size"],
                "Rectified text crop (left, top, right, bottom)": attempt["text_crop"],
                "Preparation": (
                    "Perspective correction, grayscale, autocontrast, Lanczos resize, "
                    "white border, JPEG quality 95"
                ),
                "Scale after rectification": attempt["scale"],
                "White border pixels": attempt["border"],
                "Prepared pixel size": [attempt["width"], attempt["height"]],
                "Segmentation": "Single line (7)",
                "Word dictionaries": "Disabled",
                "Minimum word confidence (0–100)": attempt["confidence"],
            }
            if not candidate:
                values["Short OCR output"] = text or "None"
            index = len(items)
            items.append(
                source_item(
                    source,
                    f"Possible plate · region {region_index} · reading {attempt_index}",
                    text=text if candidate else "",
                    anchors=[
                        source.anchor(
                            region=region["bbox"],
                            coordinate_space="original_unoriented_pixels",
                        )
                    ],
                    values=values,
                )
            )
            preview(
                attempt.get("image"),
                attempt["width"],
                attempt["height"],
                finding_index=index,
            )
    if not items:
        items.append(
            source_item(
                source,
                "Plate search coverage",
                values={
                    "Recognition status": (
                        "No candidate plate regions found; inspect the original"
                    ),
                    "Detection method": parsed["detector"],
                    "OCR engine": parsed["engine"],
                    "Input SHA-256": checksum,
                },
            )
        )
    warnings = [
        "Local plate search returns suggestions. Rectangular objects can be mistaken "
        "for plates, and plates can be missed. Compare every reading with the "
        "original; a reading does not establish a vehicle's identity or ownership. "
        "Engine scores are not probabilities. Alternative readings are retained "
        "without resolving conflicts or substituting characters."
    ]
    if parsed.get("image_count", 1) > 1:
        warnings.append(
            "Only the primary embedded image was searched; additional pictures "
            "were not analyzed."
        )
    if parsed["limited"]:
        warnings.append(
            "The bounded region search reached its contour or five-region limit. "
            "Other plates may have been missed."
        )
    if omitted:
        warnings.append(
            f"{omitted} previews were omitted at the preview byte limit. "
            "Region coordinates and preparation details remain available."
        )
    result = output(
        f"Possible plate regions: {len(parsed['candidates'])}. "
        f"Text candidates: {readings}."
        if parsed["candidates"]
        else "No candidate plate regions found. Inspect the original image.",
        items=items,
        warnings=warnings,
        outcome="partial",
        metrics={
            "Possible plate regions": len(parsed["candidates"]),
            "Text candidates": readings,
            "Processing location": "This server",
            "Search limit reached": parsed["limited"],
        },
    )
    result["artifacts"] = artifacts
    return result
