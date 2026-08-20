"""English Tesseract OCR/OSD invocation and deterministic output parsing."""

import csv
import io
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from os import PathLike
from typing import Any

from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ToolFailedError,
)
from pandect.processing.extractors.subprocesses import CommandResult, run_command

TESSERACT_TIMEOUT_SECONDS = 60
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_.+-]+$")
_REQUIRED_TSV_FIELDS = {
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
}


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class OCRMetadata:
    """Timestamp-free OCR observations with source pixel geometry."""

    text: str
    words: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    blocks: list[dict[str, Any]]
    page_count: int
    mean_confidence: float | None
    orientation: dict[str, Any] | None = None
    orientation_error_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(
            {
                "text": self.text,
                "words": self.words,
                "lines": self.lines,
                "blocks": self.blocks,
                "page_count": self.page_count,
                "mean_confidence": self.mean_confidence,
                "orientation": self.orientation,
                "orientation_error_code": self.orientation_error_code,
                "language": "eng",
            }
        )


def parse_tesseract_tsv(value: str | bytes) -> OCRMetadata:
    """Parse Tesseract TSV into ordered words, lines, and plain text."""
    try:
        text_value = value.decode("utf-8") if isinstance(value, bytes) else value
    except UnicodeDecodeError as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(text_value, str):
        raise InvalidExtractorOutputError

    reader = csv.DictReader(
        io.StringIO(text_value),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    )
    if not reader.fieldnames or not _REQUIRED_TSV_FIELDS.issubset(reader.fieldnames):
        raise InvalidExtractorOutputError

    words: list[dict[str, Any]] = []
    line_words: dict[tuple[int, int, int, int], list[str]] = {}
    line_boxes: dict[tuple[int, int, int, int], list[tuple[int, int, int, int]]] = {}
    pages: set[int] = set()
    confidences: list[float] = []
    for row in reader:
        numeric = {
            field: _integer(row.get(field))
            for field in (
                "level",
                "page_num",
                "block_num",
                "par_num",
                "line_num",
                "word_num",
                "left",
                "top",
                "width",
                "height",
            )
        }
        if any(result is None for result in numeric.values()):
            raise InvalidExtractorOutputError
        page = numeric["page_num"]
        assert page is not None
        pages.add(page)
        word_text = row.get("text") or ""
        if numeric["level"] != 5 or not word_text.strip():
            continue
        confidence = _number(row.get("conf"))
        if confidence is not None and confidence >= 0:
            confidences.append(confidence)
        word = {
            **numeric,
            "confidence": confidence,
            "text": word_text,
        }
        words.append(word)
        line_key = (
            page,
            numeric["block_num"],
            numeric["par_num"],
            numeric["line_num"],
        )
        line_words.setdefault(line_key, []).append(word_text)
        left = numeric["left"]
        top = numeric["top"]
        width = numeric["width"]
        height = numeric["height"]
        assert None not in (left, top, width, height)
        line_boxes.setdefault(line_key, []).append((left, top, width, height))

    lines = []
    for key, parts in line_words.items():
        boxes = line_boxes[key]
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[0] + box[2] for box in boxes)
        bottom = max(box[1] + box[3] for box in boxes)
        lines.append(
            {
                "page_num": key[0],
                "block_num": key[1],
                "paragraph_num": key[2],
                "line_num": key[3],
                "text": " ".join(parts),
                "box": {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                },
            }
        )
    lines.sort(
        key=lambda line: (
            line["page_num"],
            line["block_num"],
            line["paragraph_num"],
            line["line_num"],
        )
    )

    block_lines: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for line in lines:
        block_lines.setdefault((line["page_num"], line["block_num"]), []).append(line)
    blocks = []
    for (page_num, block_num), values in sorted(block_lines.items()):
        left = min(value["box"]["left"] for value in values)
        top = min(value["box"]["top"] for value in values)
        right = max(value["box"]["left"] + value["box"]["width"] for value in values)
        bottom = max(value["box"]["top"] + value["box"]["height"] for value in values)
        blocks.append(
            {
                "page_num": page_num,
                "block_num": block_num,
                "text": "\n".join(value["text"] for value in values),
                "line_count": len(values),
                "box": {
                    "left": left,
                    "top": top,
                    "width": right - left,
                    "height": bottom - top,
                },
            }
        )

    plain_text_parts: list[str] = []
    previous_page = None
    for line in lines:
        if previous_page is not None and line["page_num"] != previous_page:
            plain_text_parts.append("")
        plain_text_parts.append(line["text"])
        previous_page = line["page_num"]

    return OCRMetadata(
        text="\n".join(plain_text_parts),
        words=words,
        lines=lines,
        blocks=blocks,
        page_count=len(pages),
        mean_confidence=(
            round(sum(confidences) / len(confidences), 6) if confidences else None
        ),
    )


def parse_tesseract_osd(value: str | bytes) -> dict[str, Any]:
    """Parse stable key/value fields from Tesseract orientation output."""
    try:
        text_value = value.decode("utf-8") if isinstance(value, bytes) else value
    except UnicodeDecodeError as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(text_value, str):
        raise InvalidExtractorOutputError
    fields: dict[str, str] = {}
    for line in text_value.splitlines():
        key, separator, raw_value = line.partition(":")
        if separator:
            fields[key.strip().casefold()] = raw_value.strip()
    if not fields:
        raise InvalidExtractorOutputError

    return {
        "page_number": _integer(fields.get("page number")),
        "orientation_degrees": _integer(fields.get("orientation in degrees")),
        "rotate_degrees": _integer(fields.get("rotate")),
        "orientation_confidence": _number(fields.get("orientation confidence")),
        "script": fields.get("script"),
        "script_confidence": _number(fields.get("script confidence")),
    }


def extract_ocr(
    path: str | PathLike[str],
    *,
    language: str = "eng",
    page_segmentation_mode: int = 3,
    include_orientation: bool = True,
    timeout_seconds: float = TESSERACT_TIMEOUT_SECONDS,
    tesseract_binary: str = "tesseract",
    runner: Callable[..., CommandResult] = run_command,
) -> OCRMetadata:
    """Run English OCR and optionally orientation/script detection."""
    if not _LANGUAGE_PATTERN.fullmatch(language):
        raise ValueError("invalid Tesseract language")
    if not 0 <= page_segmentation_mode <= 13:
        raise ValueError("page_segmentation_mode must be between 0 and 13")
    result = runner(
        (
            tesseract_binary,
            path,
            "stdout",
            "-l",
            language,
            "--psm",
            str(page_segmentation_mode),
            "tsv",
        ),
        timeout_seconds=timeout_seconds,
    )
    parsed = parse_tesseract_tsv(result.stdout)

    orientation = None
    orientation_error_code = None
    if include_orientation:
        try:
            osd_result = runner(
                (
                    tesseract_binary,
                    path,
                    "stdout",
                    "-l",
                    "osd",
                    "--psm",
                    "0",
                ),
                timeout_seconds=timeout_seconds,
            )
            orientation = parse_tesseract_osd(osd_result.stdout)
        except ToolFailedError as error:
            # OSD commonly rejects images with too little text. OCR remains a
            # valid partial result; missing binaries and timeouts still fail.
            orientation_error_code = error.code

    return OCRMetadata(
        text=parsed.text,
        words=parsed.words,
        lines=parsed.lines,
        blocks=parsed.blocks,
        page_count=parsed.page_count,
        mean_confidence=parsed.mean_confidence,
        orientation=orientation,
        orientation_error_code=orientation_error_code,
    )
