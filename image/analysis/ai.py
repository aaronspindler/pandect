"""Bounded local printed-English OCR; images never leave the parser."""

from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.analysis.sources import source_item as _item
from core.analysis.sources import unsupported_source as _unsupported
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser

MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def ocr(sources, parameters, *, authorize):
    items, warnings = [], []
    artifacts = []
    preview_bytes = omitted_previews = 0
    preview_limit_reached = False
    unsupported = partial = 0
    total_text = 0
    if parameters.get("mode", "printed") != "printed":
        return output(
            "Local OCR supports printed English only. Handwriting and mixed "
            "profiles are unsupported; no external processing was attempted.",
            outcome="unsupported",
        )
    if parameters.get("language", "en") != "en":
        return output("Local OCR supports English only.", outcome="unsupported")
    for source in sources:
        if source.kind not in {"image", "document"}:
            items.append(
                _unsupported(
                    source, "A JPEG or PNG image/scanned-page file is required."
                )
            )
            unsupported += 1
            continue
        content, checksum = read_source(source)
        if not (
            content.startswith(b"\xff\xd8\xff")
            or content.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            items.append(
                _unsupported(
                    source,
                    "Upload a JPEG/PNG page image. Direct PDF rendering is "
                    "unsupported.",
                )
            )
            unsupported += 1
            continue
        authorize()
        prepared = run_parser("ocr", content)
        authorize()
        if prepared["status"] == "unsupported":
            items.append(_unsupported(source, prepared["reason"]))
            unsupported += 1
            continue
        result = prepared
        encoded = prepared["image"]
        decoded_size = len(encoded) * 3 // 4 - (len(encoded) - len(encoded.rstrip("=")))
        if (
            not preview_limit_reached
            and preview_bytes + decoded_size <= MAX_PREVIEW_BYTES
        ):
            artifacts.append(
                {
                    "source_id": str(source.id),
                    "content_type": "image/jpeg",
                    "data_base64": encoded,
                    "width": prepared["prepared_width"],
                    "height": prepared["prepared_height"],
                    "coordinate_space": "original_unoriented_pixels",
                }
            )
            preview_bytes += decoded_size
        else:
            preview_limit_reached = True
            omitted_previews += 1
        status = result["status"]
        source_partial = status in {"partial", "unreadable"}
        regions = sorted(result["regions"], key=lambda x: x["order"])
        if not regions:
            items.append(
                _item(
                    source,
                    "OCR coverage",
                    values={
                        "OCR profile": "Printed English (local)",
                        "Recognition status": (
                            "No text recognized; inspect the original"
                        ),
                        "OCR engine": prepared["engine"],
                        "Input SHA-256": checksum,
                    },
                )
            )
        for region in regions:
            text = region["text"]
            total_text += len(text.encode())
            if total_text > MAX_TEXT:
                raise AnalysisError("extracted_text_limit")
            items.append(
                _item(
                    source,
                    f"Text candidate · region {region['order'] + 1}",
                    text=text,
                    anchors=[
                        source.anchor(
                            region=region["bbox"],
                            coordinate_space="original_unoriented_pixels",
                        )
                    ],
                    values={
                        "OCR profile": "Printed English (local)",
                        "OCR engine": prepared["engine"],
                        "Language setting": "English; not language detection",
                        "Reading order": region["order"],
                        "Minimum word confidence (0–100)": region["confidence"],
                        "Original pixel width": prepared["width"],
                        "Original pixel height": prepared["height"],
                        "Prepared pixel width": prepared["prepared_width"],
                        "Prepared pixel height": prepared["prepared_height"],
                        "Preparation parser": prepared["version"],
                        "Input SHA-256": checksum,
                    },
                )
            )
        partial += source_partial
        if len(items) > 5000:
            raise AnalysisError("result_limit")
    warnings.append(
        "Printed English OCR runs locally; no image is sent to an external service. "
        "Candidate text requires comparison with the original. Confidence scores "
        "are engine scores, not probabilities of correctness. Missing text, "
        "handwriting, other languages, rotation and complex layouts may go "
        "unrecognized. No recognized text does not prove a blank image. "
        "Regions use the original unrotated pixel coordinate space. The private "
        "preview is the resized, metadata-free JPEG used for recognition."
    )
    if omitted_previews:
        warnings.append(
            f"{omitted_previews} prepared image previews were omitted because "
            "the cumulative 2 MiB preview limit was reached. OCR text and "
            "region coordinates remain available; download the retained "
            "originals to inspect those regions."
        )
    outcome = (
        "partial"
        if partial or (unsupported and total_text)
        else "unsupported"
        if unsupported
        else "complete"
    )
    result = output(
        f"Produced {len(items)} source-linked OCR findings.",
        items=items,
        metrics={
            "Selected sources": len(sources),
            "Unsupported inputs": unsupported,
            "Partial/unreadable inputs": partial,
            "UTF-8 text bytes": total_text,
            "OCR profile": "Printed English (local)",
        },
        warnings=warnings,
        outcome=outcome,
    )
    result["artifacts"] = artifacts
    return result
