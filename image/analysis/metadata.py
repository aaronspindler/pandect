"""Decoded image metadata and retained source claims."""

import json

from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.analysis.sources import source_item as _item
from core.analysis.sources import unsupported_source as _unsupported
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser


def image_metadata(sources, parameters):
    items, warnings = [], []
    supported = partial = 0
    retained_metadata_bytes = 0
    for source in sources:
        if source.kind != "image":
            items.append(
                _unsupported(source, "A retained JPEG or PNG image is required.")
            )
            continue
        content, checksum = read_source(source)
        parsed = run_parser("image", content)
        if parsed["status"] == "unsupported":
            items.append(_unsupported(source, parsed["reason"]))
            continue
        supported += 1
        retained_metadata_bytes += len(json.dumps(parsed, ensure_ascii=False).encode())
        if retained_metadata_bytes > MAX_TEXT:
            raise AnalysisError("result_limit")
        values = {
            "Parser": parsed["version"],
            "Input SHA-256": checksum,
            "Decoded format": parsed["format"],
            "Decoded width": parsed["width"],
            "Decoded height": parsed["height"],
            "Color mode": parsed["mode"],
            "Orientation claim": parsed.get("orientation") or "Not supplied",
            "Capture time raw": parsed.get("capture_time_raw") or "Not supplied",
            "Capture offset raw": parsed.get("capture_offset_raw") or "Not supplied",
            "Capture time candidate": parsed.get("capture_time_candidate") or "Unknown",
            "GPS candidate": parsed.get("gps_candidate") or "Unknown",
        }
        image_count = parsed.get("image_count", 1)
        if image_count > 1:
            partial += 1
            values["Embedded images"] = image_count
            values["Inspected image"] = f"Primary image (1 of {image_count})"
            warnings.append(
                f"Only the primary image of {source.item.title} was inspected; "
                f"{image_count - 1} additional embedded images were not analyzed."
            )
        for field in ("width", "height"):
            supplied = getattr(source.item, field, None)
            values[f"Supplied {field}"] = (
                supplied if supplied is not None else "Unknown"
            )
            if supplied is not None and supplied != parsed[field]:
                warnings.append(
                    f"Supplied and decoded {field} disagree for {source.item.title}."
                )
        raw = parsed.get("raw", {})
        values["Embedded metadata"] = (
            "Present" if any(raw.values()) else "Absent or stripped"
        )
        items.append(_item(source, "Decoded image and metadata claims", values=values))
        for name, value in raw.items():
            if value not in (None, {}, ""):
                items.append(
                    _item(
                        source,
                        f"Raw metadata · {name}",
                        text=json.dumps(value, ensure_ascii=False),
                        values={
                            "Basis": "Embedded unverified metadata",
                            "Input SHA-256": checksum,
                        },
                    )
                )
        if parsed.get("xmp_fields"):
            items.append(
                _item(
                    source,
                    "Interpreted XMP attributes",
                    values=parsed["xmp_fields"]
                    | {
                        "Trailing XMP NUL bytes ignored for interpretation": parsed.get(
                            "xmp_trailing_nuls", 0
                        )
                    },
                )
            )
        if parsed.get("xmp_warning"):
            warnings.append(parsed["xmp_warning"])
    warnings.append(
        "Metadata is a source claim and may be edited or stripped. A capture "
        "time without an offset has no assumed timezone; it differs from upload "
        "and collection time. GPS candidates do not verify presence."
    )
    return output(
        f"Decoded metadata from {supported} supported images.",
        items=items,
        metrics={"Supported images": supported, "Selected sources": len(sources)},
        warnings=warnings,
        outcome="complete"
        if supported == len(sources) and not partial
        else "partial"
        if supported
        else "unsupported",
    )
