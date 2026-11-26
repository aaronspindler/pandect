"""Source-linked recognition of bounded page images using the approved vision model."""

import base64
import hashlib

from core.analysis.errors import AnalysisError
from core.analysis.sources import output, source_item, unsupported_source
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser

MAX_IMAGES_PER_RUN = 16
MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def ocr(sources, parameters, *, authorize, client):
    if (
        parameters.get("mode", "printed") != "printed"
        or parameters.get("language", "en") != "en"
    ):
        return output(
            "Choose the printed English recognition profile.", outcome="unsupported"
        )
    items, warnings, artifacts = [], [], []
    preview_bytes = 0
    requests = omitted = uncertain = size = supported = additional_pictures = 0
    for source in sources:
        authorize()
        content, checksum = read_source(source)
        pdf = source.kind == "document" and content.startswith(b"%PDF-")
        if pdf:
            parsed = run_parser("document", content)
            pages = [p["page"] for p in parsed.get("pages", []) if not p["native_text"]]
        else:
            parsed = run_parser("ocr", content)
            pages = [None]
        if parsed["status"] == "unsupported":
            items.append(unsupported_source(source, parsed["reason"]))
            continue
        supported += 1
        if pdf and not pages:
            items.append(
                source_item(
                    source,
                    "All pages have native text",
                    values={"Input SHA-256": checksum},
                )
            )
        if not pdf and parsed.get("image_count", 1) > 1:
            warnings.append("Only the primary embedded picture was examined.")
            additional_pictures += parsed["image_count"] - 1
        for page in pages:
            anchors = [source.anchor(page=page)] if page else [source.anchor()]
            if requests >= MAX_IMAGES_PER_RUN:
                omitted += 1
                items.append(
                    source_item(source, "Recognition limit reached", anchors=anchors)
                )
                continue
            prepared = (
                run_parser(
                    "document_preview", content, options={"render": True, "page": page}
                )
                if pdf
                else parsed
            )
            encoded = prepared["image"]
            raw = base64.b64decode(encoded, validate=True)
            if not pdf and preview_bytes + len(raw) <= MAX_PREVIEW_BYTES:
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
                preview_bytes += len(raw)
            elif not pdf and "Preview byte limit reached." not in warnings:
                warnings.append("Preview byte limit reached.")
            if not pdf:
                anchors = [
                    source.anchor(
                        region=[0, 0, 1, 1],
                        coordinate_space="original_unoriented_pixels",
                    )
                ]

            text, incomplete = client.read_image(encoded)
            requests += 1
            uncertain += incomplete or not text
            size += len(text.encode())
            if size > MAX_TEXT:
                raise AnalysisError("extracted_text_limit")
            items.append(
                source_item(
                    source,
                    f"Page {page} · candidate text"
                    if page
                    else "Image · candidate text",
                    text=text,
                    anchors=anchors,
                    values={
                        "Input SHA-256": checksum,
                        "Recognition JPEG SHA-256": hashlib.sha256(raw).hexdigest(),
                        "Preparation parser": prepared["version"],
                        "OCR model": parameters["_execution"]["model"],
                        "Coverage": "Uncertain or unreadable"
                        if incomplete or not text
                        else "Candidate transcription",
                    },
                )
            )
    warnings.append(
        "Compare candidate text with the original for omissions and misread characters."
    )
    if omitted:
        warnings.append(
            f"{omitted} images or pages exceeded the limit of {MAX_IMAGES_PER_RUN}."
        )
    result = output(
        f"Read {requests} images or scanned pages.",
        items=items,
        warnings=warnings,
        metrics={
            "Selected sources": len(sources),
            "Images read": requests,
            "Images omitted": omitted,
            "Additional embedded pictures omitted": additional_pictures,
            "Uncertain or unreadable": uncertain,
            "UTF-8 text bytes": size,
        },
        outcome="unsupported"
        if not supported
        else "partial"
        if omitted or uncertain or additional_pictures or supported != len(sources)
        else "complete",
    )
    result["artifacts"] = artifacts
    return result
