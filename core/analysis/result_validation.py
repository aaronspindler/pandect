"""Validate analysis output size, shape and source anchors before publication."""

import json
import math

from core.analysis.errors import AnalysisError

MAX_RESULT_BYTES = 4 * 1024 * 1024


def validate_output(data, sources, *, operation=None):
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("summary"), str)
        or not isinstance(data.get("metrics", {}), dict)
    ):
        raise AnalysisError("invalid_result")
    if data.get("outcome", "complete") not in (
        "complete",
        "partial",
        "empty",
        "insufficient",
        "unsupported",
    ):
        raise AnalysisError("invalid_result")
    try:
        size = len(json.dumps(data, allow_nan=False).encode())
    except ValueError, TypeError, RecursionError:
        raise AnalysisError("invalid_result") from None
    if size > MAX_RESULT_BYTES and operation == "ocr" and data.get("visible_structure"):
        from image.analysis.ai import omit_structure_at_result_limit

        omit_structure_at_result_limit(data)
        size = len(json.dumps(data, allow_nan=False).encode())
    if size > MAX_RESULT_BYTES:
        raise AnalysisError("result_limit")
    source_map = {str(x.id): x for x in sources}
    items = data.get("items", [])
    if not isinstance(items, list) or len(items) > 10000:
        raise AnalysisError("invalid_result")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("label"), str)
            or not isinstance(item.get("text", ""), str)
            or not isinstance(item.get("values", {}), dict)
        ):
            raise AnalysisError("invalid_result")
        refs = item.get("source_ids", [])
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in source_map for ref in refs)
        ):
            raise AnalysisError("invalid_anchor")
        anchors = item.get("anchors", [])
        if not isinstance(anchors, list):
            raise AnalysisError("invalid_anchor")
        for anchor in anchors:
            if not isinstance(anchor, dict) or anchor.get("source_id") not in refs:
                raise AnalysisError("invalid_anchor")
            source = source_map[anchor["source_id"]]
            if "start" in anchor or "end" in anchor:
                start, end = anchor.get("start"), anchor.get("end")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start <= end <= len(source.text)
                ):
                    raise AnalysisError("invalid_anchor")
            if "page" in anchor and (
                type(anchor["page"]) is not int or not 1 <= anchor["page"] <= 100
            ):
                raise AnalysisError("invalid_anchor")
            if "region" in anchor:
                region = anchor["region"]
                if (
                    not isinstance(region, list)
                    or len(region) != 4
                    or not all(
                        type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
                        for x in region
                    )
                    or region[0] >= region[2]
                    or region[1] >= region[3]
                ):
                    raise AnalysisError("invalid_anchor")
            if "start_seconds" in anchor or "end_seconds" in anchor:
                start, end = anchor.get("start_seconds"), anchor.get("end_seconds")
                if (
                    not all(
                        type(x) in (int, float) and math.isfinite(x)
                        for x in (start, end)
                    )
                    or not 0 <= start <= end
                ):
                    raise AnalysisError("invalid_anchor")
    return data
