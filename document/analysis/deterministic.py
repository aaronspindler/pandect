"""Document text with inspectable page and line coverage."""

from time import monotonic

from core.analysis.errors import AnalysisError
from core.analysis.sources import output, source_item
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser
from document.email_parser import is_email_filename

OCR_RUN_SECONDS = 45
OCR_COVERAGE = {
    "recognized": "Candidate text from local OCR",
    "low_score": "Candidate text from local OCR with low-scoring words",
    "no_text": "No text recognized; inspect the original page",
    "parser_timeout": "OCR exceeded the time limit; inspect the original page",
    "ocr_not_installed": "OCR unavailable; ask the operator to install Tesseract",
    "ocr_failed": "OCR failed; ask the operator to check Tesseract and English data",
}


def _pdf_item(source, page, common):
    values = common | {"Page": page["page"]}
    if page["native_text"]:
        label = "native text"
        values["Coverage"] = "Native text found"
    else:
        label = "local OCR"
        values |= {
            "Method": "Local PDF page OCR",
            "Coverage": OCR_COVERAGE.get(
                page["ocr_status"], "OCR could not read this page; inspect the original"
            ),
        }
        if "ocr_engine" in page:
            values |= {
                "OCR engine": page["ocr_engine"],
                "OCR renderer": f"PDFium {page['ocr_renderer']}",
                "OCR language": "English",
            }
        if "ocr_minimum_score" in page:
            values["Minimum OCR word score"] = page["ocr_minimum_score"]
    return source_item(
        source,
        f"Page {page['page']} · {label}",
        text=page["text"],
        anchors=[source.anchor(page=page["page"])],
        values=values,
    )


def _email_item(source, parsed, common):
    return source_item(
        source,
        "Decoded email",
        text=parsed["text"],
        values=common
        | {
            "Method": "Email text extraction",
            "Message body": parsed["body_format"],
            "Coverage": parsed["coverage"],
            "Source attribution": "Sender, recipients and date are claims in the file.",
        },
    )


def native_text(sources, parameters):
    items = []
    warnings = []
    unsupported = 0
    scanned_pages = 0
    incomplete_pages = 0
    incomplete_email = 0
    total_pages = 0
    total_text_bytes = 0
    supported = 0
    ocr_deadline = monotonic() + OCR_RUN_SECONDS
    for source in sources:
        if source.kind != "document" or source.attachment is not None:
            raise AnalysisError("unsupported_input")
        content, checksum = read_source(source)
        parsed = run_parser(
            "document",
            content,
            options={
                "email": is_email_filename(
                    getattr(source.item, "original_filename", "")
                ),
                "ocr_deadline": ocr_deadline,
            },
        )
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
                    scanned_pages += 1
                    incomplete_pages += page["ocr_status"] != "recognized"
                total_text_bytes += len(page["text"].encode())
                items.append(_pdf_item(source, page, common))
        elif parsed["format"] == "Email":
            total_text_bytes += len(parsed["text"].encode())
            incomplete_email += parsed["partial"]
            items.append(_email_item(source, parsed, common))
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
    if total_pages:
        warnings.append(
            "PDF text layers may differ from the page you see and may omit text in "
            "pictures. Compare the text with the original page."
        )
    if scanned_pages:
        warnings.append(
            "Local OCR reads printed English on pages without native text. "
            "Recognition can misread or miss content. No recognized text leaves "
            "the page contents unresolved."
        )
    if incomplete_pages:
        warnings.append(
            f"{incomplete_pages} PDF pages have missing or uncertain OCR text."
        )
    if unsupported:
        warnings.append(
            f"{unsupported} selected documents could not be read as UTF-8 text, "
            "unencrypted PDF or supported .eml."
        )
    has_text = any(item["text"].strip() for item in items)
    outcome = (
        "partial"
        if incomplete_pages or incomplete_email or (has_text and unsupported)
        else "complete"
        if has_text
        else "unsupported"
        if unsupported
        else "empty"
    )
    return output(
        f"Inspected {supported} supported documents and {total_pages} PDF pages.",
        items=items,
        metrics={
            "Selected documents": len(sources),
            "Supported documents": supported,
            "PDF pages": total_pages,
            "PDF pages without usable native text": scanned_pages,
            "PDF pages needing OCR review": incomplete_pages,
            "Emails with partial coverage": incomplete_email,
            "UTF-8 text bytes": total_text_bytes,
        },
        warnings=warnings,
        outcome=outcome,
    )
