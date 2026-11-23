"""Pure local OCR for the bounded parser child; no Django initialization."""

import base64
import csv
import io
import math
import os
import subprocess
import tempfile

from core.parser_contract import MAX_TEXT, ParserFailure
from image.analysis import MAX_LAYOUT_LINES, MAX_LAYOUT_WORDS, MIN_OCR_WORD_SCORE
from image.parsers import _image


def _tesseract(arguments, content=None, *, timeout=15):
    """Run only the installed local engine, with bounded private output."""
    with tempfile.TemporaryFile() as stream:
        try:
            completed = subprocess.run(
                ["tesseract", *arguments],
                input=content,
                stdout=stream,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except FileNotFoundError:
            raise ParserFailure("ocr_not_installed") from None
        except subprocess.TimeoutExpired:
            raise ParserFailure("parser_timeout") from None
        if completed.returncode:
            raise ParserFailure("ocr_failed")
        stream.seek(0)
        raw = stream.read(MAX_TEXT + 1)
    if len(raw) > MAX_TEXT:
        raise ParserFailure("extracted_text_limit")
    return raw.decode("utf-8")


def _ocr(content):
    # Pillow decodes and re-encodes first: Tesseract never receives an arbitrary
    # file path, document, image list, or the original's embedded metadata.
    prepared = _image(content, prepare=True)
    if prepared["status"] == "unsupported":
        return prepared
    version = tesseract_version()
    regions = read_prepared_text(
        base64.b64decode(prepared["image"]),
        prepared["prepared_width"],
        prepared["prepared_height"],
        layout=True,
    )
    return prepared | {
        "engine": version,
        "regions": regions,
        "status": "partial"
        if not regions or any(r["confidence"] < MIN_OCR_WORD_SCORE for r in regions)
        else "complete",
    }


def tesseract_version(*, timeout=15):
    lines = _tesseract(["--version"], timeout=timeout).splitlines()
    if not lines:
        raise ParserFailure("ocr_invalid_output")
    return lines[0][:100]


def read_prepared_text(content, width, height, *, layout=False, timeout=15):
    raw = _tesseract(
        ["stdin", "stdout", "-l", "eng", "--psm", "3", "tsv"],
        content,
        timeout=timeout,
    )
    return text_regions(raw, width, height, layout=layout)


def text_regions(raw, width, height, *, layout=False):
    """Validate Tesseract TSV and group words into source-linked lines."""
    lines = {}
    try:
        rows = csv.DictReader(io.StringIO(raw), delimiter="\t", quoting=csv.QUOTE_NONE)
        expected = (
            "level page_num block_num par_num line_num word_num "
            "left top width height conf text"
        ).split()
        if rows.fieldnames != expected:
            raise ValueError
        for row in rows:
            if row["level"] != "5":
                continue
            if not isinstance(row["text"], str) or None in row:
                raise ValueError
            if not row["text"].strip():
                continue
            left, top, w, h = (
                int(row[key]) for key in ("left", "top", "width", "height")
            )
            confidence = float(row["conf"])
            if not (
                0 <= left < left + w <= width
                and 0 <= top < top + h <= height
                and math.isfinite(confidence)
                and 0 <= confidence <= 100
            ):
                raise ValueError
            key = tuple(
                int(row[name])
                for name in ("page_num", "block_num", "par_num", "line_num")
            )
            if min(key) < 1:
                raise ValueError
            line = lines.setdefault(key, {"words": [], "boxes": [], "confidence": []})
            line["words"].append(row["text"])
            line["boxes"].append((left, top, left + w, top + h))
            line["confidence"].append(confidence)
            if len(lines) > 2000:
                raise ParserFailure("result_limit")
    except KeyError, TypeError, ValueError, csv.Error:
        raise ParserFailure("ocr_invalid_output") from None
    regions = []
    retain_words = (
        layout
        and len(lines) <= MAX_LAYOUT_LINES
        and sum(len(line["words"]) for line in lines.values()) <= MAX_LAYOUT_WORDS
    )
    for key, line in lines.items():
        boxes = line["boxes"]
        regions.append(
            {
                "text": " ".join(line["words"]),
                "bbox": [
                    min(b[0] for b in boxes) / width,
                    min(b[1] for b in boxes) / height,
                    max(b[2] for b in boxes) / width,
                    max(b[3] for b in boxes) / height,
                ],
                "order": len(regions),
                "confidence": min(line["confidence"]),
            }
        )
        if retain_words:
            words = []
            start = 0
            for word, (left, top, right, bottom), score in zip(
                line["words"], boxes, line["confidence"], strict=True
            ):
                end = start + len(word)
                words.append(
                    {
                        "start": start,
                        "end": end,
                        "region": [
                            left / width,
                            top / height,
                            right / width,
                            bottom / height,
                        ],
                        "confidence": score,
                    }
                )
                start = end + 1
            regions[-1].update(paragraph=list(key[:3]), words=words)
    if layout and regions and not retain_words:
        regions[0]["layout_omitted"] = True
    return regions


def parse(operation, content, options):
    return _ocr(content)
