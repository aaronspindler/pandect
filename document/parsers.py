"""Bounded document parsing and rendering in the isolated child."""

import base64
import io
import math
import sys

from core.parser_contract import MAX_BYTES, MAX_TEXT, ParserFailure

MAX_PAGES = 100
MAX_STREAM_BYTES = 4 * 1024 * 1024


def _pdf_reader(content):
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
    return PdfReader(io.BytesIO(content), strict=True)


def _pdf(content):
    import pypdf

    reader = _pdf_reader(content)
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


def _preview(content, options):
    if not content.startswith(b"%PDF-"):
        return _document(content)
    reader = _pdf_reader(content)
    if reader.is_encrypted:
        return {"status": "unsupported", "reason": "encrypted_pdf"}
    count = len(reader.pages)
    if not 0 < count <= MAX_PAGES:
        raise ParserFailure("document_page_limit")
    number = options.get("page", 1)
    if type(number) is not int or not 1 <= number <= count:
        raise ParserFailure("invalid_document_page")
    result = {"status": "complete", "format": "PDF", "page_count": count}
    if not options.get("render", False):
        return result

    import pypdfium2 as pdfium

    with pdfium.PdfDocument(content) as document:
        output, _, _ = _render_page(document, number, max_side=2048, max_scale=2)
    return result | {
        "image": base64.b64encode(output).decode("ascii"),
        "version": str(pdfium.PYPDFIUM_INFO),
    }


def _render_page(document, number, *, max_side, max_scale):
    page = document[number - 1]
    try:
        width, height = page.get_size()
        if not all(math.isfinite(x) and 0 < x <= 1_000_000 for x in (width, height)):
            raise ParserFailure("document_page_size_limit")
        scale = min(max_scale, max_side / max(width, height))
        if math.ceil(width * scale) * math.ceil(height * scale) > max_side**2:
            raise ParserFailure("document_page_size_limit")
        bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
        try:
            with bitmap.to_pil() as image, image.convert("RGB") as rgb:
                output = io.BytesIO()
                rgb.save(output, format="JPEG", quality=85)
                return output.getvalue(), rgb.width, rgb.height
        finally:
            bitmap.close()
    finally:
        page.close()


def parse(operation, content, options):
    if options.get("email") is True:
        from document.email_parser import parse_email

        return parse_email(content)
    return (
        _preview(content, options)
        if operation == "document_preview"
        else _document(content)
    )
