"""Decode one retained email without opening links or attachments."""

import re
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

from core.parser_contract import MAX_TEXT, ParserFailure

MAX_HEADER_BYTES = 64 * 1024
MAX_MIME_PARTS = 128
MAX_MIME_DEPTH = 12
HEADERS = ("From", "To", "Cc", "Bcc", "Date", "Subject", "Message-ID")
HEADER_END = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def is_email_filename(filename):
    return filename.lower().endswith(".eml")


class _EmailMessage(EmailMessage):
    def is_attachment(self):
        # Some exports name attachments only in Content-Type's name parameter.
        return super().is_attachment() or self.get_filename() is not None


class _HTMLText(HTMLParser):
    """Keep text nodes, with separators for common block elements."""

    OMIT = {"head", "script", "style", "template", "iframe", "object", "svg"}
    BREAK = {"address", "blockquote", "br", "div", "hr", "li", "p", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.excluded = []
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.OMIT:
            if len(self.excluded) >= MAX_MIME_DEPTH:
                raise ParserFailure("email_structure_limit")
            self.excluded.append(tag)
        elif tag in self.BREAK and not self.excluded:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if self.excluded:
            if tag == self.excluded[-1]:
                self.excluded.pop()
        elif tag in self.BREAK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.excluded:
            self.chunks.append(data)


def _inspect_parts(message):
    pending = [(message, 1, False)]
    parts = skipped = defects = 0
    leaves = []
    while pending:
        part, depth, excluded_by_parent = pending.pop()
        parts += 1
        if parts > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH:
            raise ParserFailure("email_structure_limit")
        defects += len(part.defects)
        excluded = part.is_attachment() or part.get_content_maintype() == "message"
        if excluded and not excluded_by_parent:
            skipped += 1
        if part.is_multipart():
            pending.extend(
                (child, depth + 1, excluded_by_parent or excluded)
                for child in part.iter_parts()
            )
        elif not excluded_by_parent and not excluded:
            leaves.append(part)
    return leaves, skipped, defects


def _text_body(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return "", False
    if len(payload) > MAX_TEXT:
        raise ParserFailure("extracted_text_limit")
    charset = part.get_content_charset() or "ascii"
    try:
        text = payload.decode(charset)
        clean_decode = True
    except LookupError, UnicodeError:
        text = payload.decode("utf-8", errors="replace")
        clean_decode = False
    if part.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = "\n".join(
            line for row in "".join(parser.chunks).splitlines() if (line := row.strip())
        )
    return text, clean_decode


def parse_email(content):
    boundary = HEADER_END.search(content, 0, MAX_HEADER_BYTES + 4)
    if boundary is None:
        if len(content) > MAX_HEADER_BYTES:
            raise ParserFailure("email_header_limit")
        return {"status": "unsupported", "reason": "email_headers_missing"}
    if boundary.start() > MAX_HEADER_BYTES:
        raise ParserFailure("email_header_limit")
    try:
        message = BytesParser(
            policy=policy.default.clone(message_factory=_EmailMessage)
        ).parsebytes(content)
    except RecursionError:
        raise ParserFailure("email_structure_limit") from None

    headers = []
    header_defects = False
    for name in HEADERS:
        fields = message.get_all(name, [])
        header_defects |= len(fields) > 1
        for field in fields:
            header_defects |= bool(field.defects)
            headers.append(f"{name}: {' '.join(str(field).split())}")
    if not headers:
        return {"status": "unsupported", "reason": "email_headers_missing"}

    leaves, skipped, defects = _inspect_parts(message)
    body = message.get_body(preferencelist=("plain", "html"))
    body_allowed = any(part is body for part in leaves)
    skipped += len(leaves) - body_allowed
    coverage = []
    body_format = "Unavailable"
    text = "\n".join(headers)
    if body_allowed:
        before_decode = len(body.defects)
        body_text, clean_decode = _text_body(body)
        defects += len(body.defects) - before_decode
        if body.get_content_type() == "text/html":
            body_format = "Text extracted from HTML"
            coverage.append("HTML converted to text. Compare with the original.")
        else:
            body_format = "Plain text"
        if not clean_decode:
            coverage.append("Body encoding could not be decoded as declared.")
        if body_text.strip():
            text = f"{text}\n\n{body_text}"
        else:
            coverage.append("No readable message body found.")
    else:
        coverage.append("No supported message body found.")
    if skipped:
        coverage.append(f"MIME parts skipped (attachments or other bodies): {skipped}.")
    if header_defects or defects:
        coverage.append("The email has header or MIME decoding problems.")
    if "\x00" in text:
        text = text.replace("\x00", "\ufffd")
        coverage.append("Null characters were replaced in decoded text.")
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ParserFailure("extracted_text_limit")
    return {
        "status": "complete",
        "format": "Email",
        "version": f"Python {sys.version_info.major}.{sys.version_info.minor} email",
        "text": text,
        "body_format": body_format,
        "partial": bool(coverage),
        "coverage": " ".join(coverage) or "Listed headers and plain-text body decoded.",
    }
