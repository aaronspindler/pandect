"""Bounded, transient image previews; retained originals are never modified."""

import base64
import hashlib

from core.analysis.errors import AnalysisError
from core.parsers import MAX_BYTES, run_parser
from core.storage import open_private_file


def image_preview(item, *, thumbnail=False):
    if item.byte_size > MAX_BYTES:
        raise AnalysisError("input_limit")
    with open_private_file(item, item.case_id) as original:
        content = original.read(MAX_BYTES + 1)
    if (
        len(content) != item.byte_size
        or len(content) > MAX_BYTES
        or hashlib.sha256(content).hexdigest() != item.sha256
    ):
        raise AnalysisError("source_changed")
    parsed = run_parser("image_preview", content, options={"thumbnail": thumbnail})
    if parsed["status"] != "complete":
        raise AnalysisError("unsupported_image_format")
    return base64.b64decode(parsed["image"], validate=True)
