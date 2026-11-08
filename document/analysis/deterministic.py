"""Native document text with inspectable page/line coverage."""

from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser


def native_text(sources, parameters):
    items = []
    warnings = [
        "Native text may differ from the document's rendered appearance. "
        "The extractor does not execute links, scripts, macros or embedded files."
    ]
    unsupported = 0
    empty_pages = 0
    total_pages = 0
    total_text_bytes = 0
    supported = 0
    for source in sources:
        if source.kind != "document" or source.attachment is not None:
            raise AnalysisError("unsupported_input")
        content, checksum = read_source(source)
        parsed = run_parser("document", content)
        if parsed["status"] == "unsupported":
            unsupported += 1
            items.append(
                {
                    "label": "Unsupported document input",
                    "text": "",
                    "source_ids": [source.id],
                    "anchors": [source.anchor()],
                    "values": {"Status": parsed["reason"], "Input SHA-256": checksum},
                }
            )
            continue
        supported += 1
        common = {
            "Method": "Native text extraction",
            "Parser": parsed["version"],
            "Decoded format": parsed["format"],
            "Input SHA-256": checksum,
        }
        if parsed["format"] == "PDF":
            total_pages += parsed["page_count"]
            for page in parsed["pages"]:
                if not page["native_text"]:
                    empty_pages += 1
                total_text_bytes += len(page["text"].encode())
                items.append(
                    {
                        "label": f"Page {page['page']} · native text",
                        "text": page["text"],
                        "source_ids": [source.id],
                        "anchors": [source.anchor(page=page["page"])],
                        "values": {
                            **common,
                            "Page": page["page"],
                            "Coverage": "Native text found"
                            if page["native_text"]
                            else (
                                "No native text; blank, scanned or unsupported "
                                "content needs visual review/OCR"
                            ),
                        },
                    }
                )
        else:
            text = parsed["text"]
            total_text_bytes += len(text.encode())
            items.append(
                {
                    "label": "Native UTF-8 text",
                    "text": text,
                    "source_ids": [source.id],
                    "anchors": [
                        source.anchor(line_start=1, line_end=parsed["line_count"])
                    ],
                    "values": {
                        **common,
                        "Lines": parsed["line_count"],
                        "UTF-8 BOM removed in derived text": parsed["bom"],
                    },
                }
            )
        if total_text_bytes > MAX_TEXT:
            raise AnalysisError("extracted_text_limit")
        if len(items) > 5000:
            raise AnalysisError("result_limit")
    if empty_pages:
        warnings.append(
            f"{empty_pages} of {total_pages} PDF pages have no usable native text. "
            "This does not establish an empty page; scanned or mixed pages require "
            "visual inspection and separate OCR. PDF page images are not rendered here."
        )
    if unsupported:
        warnings.append(
            f"{unsupported} selected documents are outside the supported UTF-8 / "
            "unencrypted native-text PDF set. Office formats require a "
            "separate extractor."
        )
    has_text = any(item["text"].strip() for item in items)
    outcome = (
        "partial"
        if has_text and (unsupported or empty_pages)
        else "complete"
        if has_text
        else "unsupported"
        if unsupported or empty_pages
        else "empty"
    )
    return output(
        f"Extracted native text from {supported} supported documents; "
        f"{total_pages} PDF pages inspected.",
        items=items,
        metrics={
            "Selected documents": len(sources),
            "Supported documents": supported,
            "PDF pages": total_pages,
            "PDF pages without usable native text": empty_pages,
            "UTF-8 text bytes": total_text_bytes,
        },
        warnings=warnings,
        outcome=outcome,
    )
