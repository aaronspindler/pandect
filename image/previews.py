"""Bounded, transient image previews; retained originals are never modified."""

import base64

from core.analysis.errors import AnalysisError
from core.analysis.sources import Source
from core.parsers import MAX_BYTES, read_source, run_parser


def image_preview(item, *, thumbnail=False):
    content, _ = read_source(Source(str(item.pk), "image", item), maximum=MAX_BYTES)
    parsed = run_parser("image_preview", content, options={"thumbnail": thumbnail})
    if parsed["status"] != "complete":
        raise AnalysisError("unsupported_image_format")
    return base64.b64decode(parsed["image"], validate=True)
