"""Transient original-file inspection; no result or derivative is retained."""

from core.analysis.errors import AnalysisError
from core.analysis.sources import Source
from core.parsers import read_source, run_parser

MESSAGES = {
    "encrypted_pdf": "This PDF is encrypted. Download the original to inspect it.",
    "document_page_limit": "PDF previews support documents with 1–100 pages.",
    "invalid_document_page": "That page is not in this document.",
    "input_limit": "Document previews support files up to 16 MiB.",
    "extracted_text_limit": "The decoded text exceeds the 2 MiB reading limit.",
    "unsupported_document_format": "Inline reading supports PDF and UTF-8 text files.",
    "source_changed": "The retained bytes no longer match this original's checksum.",
    "source_unavailable": "The original file is unavailable.",
}


def inspect_document(item, *, page=1, render=False):
    content, _ = read_source(Source(str(item.pk), "document", item))
    parsed = run_parser(
        "document_preview", content, options={"page": page, "render": render}
    )
    if parsed.get("status") != "complete":
        raise AnalysisError(parsed.get("reason", "unsupported_document_format"))
    return parsed


def preview_message(error):
    return MESSAGES.get(
        error.code, "Preview unavailable. Download the original to inspect it."
    )
