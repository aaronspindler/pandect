"""Pure bounded UTF-8 and native PDF parsing."""

import io
import sys

from core.parser_contract import MAX_BYTES, MAX_TEXT, ParserFailure

MAX_PAGES = 100
MAX_STREAM_BYTES = 4 * 1024 * 1024


def _pdf(content):
    import pypdf
    from pypdf import PdfReader

    pypdf.overwrite_configuration(
        maximum_declared_stream_length=MAX_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
        zlib_maximum_output_length=MAX_STREAM_BYTES,
        lzw_maximum_output_length=MAX_STREAM_BYTES,
        run_length_maximum_output_length=MAX_STREAM_BYTES,
        brotli_maximum_output_length=MAX_STREAM_BYTES,
        stream_decoding_work_maximum_length=MAX_BYTES,
        stream_filters_maximum_length=8,
        page_tree_maximum_depth=20,
        jbig2dec_binary=None,
    )
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted:
        return {"status": "unsupported", "reason": "encrypted_pdf", "pages": []}
    page_count = len(reader.pages)
    if page_count > MAX_PAGES:
        raise ParserFailure("document_page_limit")
    pages = []
    text_bytes = 0
    stream_bytes = 0
    for number, page in enumerate(reader.pages, start=1):
        contents = page.get_contents()
        if contents is not None:
            size = len(contents.get_data())
            stream_bytes += size
            if size > MAX_STREAM_BYTES or stream_bytes > MAX_BYTES:
                raise ParserFailure("document_stream_limit")
        text = page.extract_text() or ""
        text_bytes += len(text.encode("utf-8"))
        if text_bytes > MAX_TEXT:
            raise ParserFailure("extracted_text_limit")
        pages.append({"page": number, "text": text, "native_text": bool(text.strip())})
    return {
        "status": "complete",
        "format": "PDF",
        "version": f"pypdf {pypdf.__version__}",
        "pages": pages,
        "page_count": page_count,
    }


def _document(content):
    if content.startswith(b"%PDF-"):
        from pypdf.errors import LimitReachedError

        try:
            return _pdf(content)
        except LimitReachedError:
            raise ParserFailure("document_stream_limit") from None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return {"status": "unsupported", "reason": "unsupported_document_format"}
    if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        return {"status": "unsupported", "reason": "unsupported_document_format"}
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ParserFailure("extracted_text_limit")
    return {
        "status": "complete",
        "format": "UTF-8",
        "version": f"Python {sys.version_info.major}.{sys.version_info.minor} UTF-8",
        "text": text,
        "line_count": len(text.splitlines()) or 1,
        "bom": content.startswith(b"\xef\xbb\xbf"),
    }


def parse(operation, content, options):
    return _document(content)
