"""Bounded geometric plate candidates and local OCR; no Django or networks."""

import base64
import hashlib
import io
import math

from PIL import Image, ImageOps

from core.parser_contract import ParserFailure
from image.analysis.ai_parser import _tesseract, text_regions
from image.parsers import _image

MAX_CANDIDATES = 5
MAX_CONTOURS = 1000
MAX_CROP_PREVIEW_BYTES = 1024 * 1024


def _overlap(first, second):
    intersection = max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1])
    )
    first_area = (first[2] - first[0]) * (first[3] - first[1])
    second_area = (second[2] - second[0]) * (second[3] - second[1])
    return intersection / (first_area + second_area - intersection)


def candidate_quads(preview):
    import cv2
    import numpy as np

    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    gray = cv2.cvtColor(np.asarray(preview.convert("RGB")), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    candidates = []
    for contour in contours[:MAX_CONTOURS]:
        area = cv2.contourArea(contour)
        if not 300 <= area <= gray.size * 0.85:
            continue
        (_, _), (width, height), _ = cv2.minAreaRect(contour)
        long, short = max(width, height), min(width, height)
        if short < 12 or not 1.5 <= long / short <= 6 or area / (width * height) < 0.65:
            continue
        points = cv2.approxPolyDP(contour, 0.025 * cv2.arcLength(contour, True), True)
        if len(points) != 4 or not cv2.isContourConvex(points):
            continue
        points = points.reshape(4, 2)
        sums, differences = points.sum(axis=1), np.diff(points, axis=1).ravel()
        quad = points[
            [sums.argmin(), differences.argmin(), sums.argmax(), differences.argmax()]
        ]
        if len(set(map(tuple, quad))) != 4:
            continue
        tl, tr, br, bl = quad
        if tr[0] <= tl[0] or abs(float(tr[1] - tl[1])) > (tr[0] - tl[0]) * 0.6:
            continue
        left, top = quad.min(axis=0)
        right, bottom = quad.max(axis=0)
        bbox = [
            left / preview.width,
            top / preview.height,
            right / preview.width,
            bottom / preview.height,
        ]
        # Suppress near-duplicate panel outlines, while keeping a smaller plate
        # inside a larger rectangular object as a separate candidate.
        if any(_overlap(bbox, item["bbox"]) > 0.7 for item in candidates):
            continue
        candidates.append(
            {
                "bbox": [float(x) for x in bbox],
                "corners": [
                    [float(x / preview.width), float(y / preview.height)]
                    for x, y in quad
                ],
            }
        )
        if len(candidates) > MAX_CANDIDATES:
            break
    return candidates[:MAX_CANDIDATES], len(candidates) > MAX_CANDIDATES or len(
        contours
    ) > MAX_CONTOURS


def parse(operation, content, options):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise ParserFailure("plate_detector_unavailable") from None

    prepared = _image(content, prepare=True)
    if prepared["status"] == "unsupported":
        return prepared
    with Image.open(io.BytesIO(base64.b64decode(prepared["image"]))) as preview:
        candidates, limited = candidate_quads(preview)
    engine = _tesseract(["--version"]).splitlines()[0][:100]
    preview_bytes = 0
    with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as original:
        for candidate in candidates:
            points = [
                (x * original.width, y * original.height)
                for x, y in candidate["corners"]
            ]
            tl, tr, br, bl = points
            width = max(math.dist(tl, tr), math.dist(bl, br))
            height = max(math.dist(tl, bl), math.dist(tr, br))
            scale = min(1, 1024 / width, 512 / height)
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            left = max(0, math.floor(min(x for x, y in points)))
            top = max(0, math.floor(min(y for x, y in points)))
            right = min(original.width, math.ceil(max(x for x, y in points)) + 1)
            bottom = min(original.height, math.ceil(max(y for x, y in points)) + 1)
            patch = original.crop((left, top, right, bottom))
            if "A" in patch.getbands() or "transparency" in patch.info:
                rgba = patch.convert("RGBA")
                patch = Image.alpha_composite(
                    Image.new("RGBA", rgba.size, "white"), rgba
                ).convert("RGB")
            else:
                patch = patch.convert("RGB")
            relative = np.float32([(x - left, y - top) for x, y in points])
            destination = np.float32(
                [
                    (0, 0),
                    (size[0] - 1, 0),
                    (size[0] - 1, size[1] - 1),
                    (0, size[1] - 1),
                ]
            )
            transform = cv2.getPerspectiveTransform(relative, destination)
            rectified = Image.fromarray(
                cv2.warpPerspective(
                    np.asarray(patch),
                    transform,
                    size,
                    flags=cv2.INTER_CUBIC,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=(255, 255, 255),
                )
            )
            attempts = []
            for enlargement, inset in ((2, 0), (2, 0.02), (3, 0.02)):
                text_crop = [inset, 0.22, 1 - inset, 0.8]
                line = rectified.crop(
                    tuple(
                        round(value * size[index % 2])
                        for index, value in enumerate(text_crop)
                    )
                )
                gray = ImageOps.autocontrast(ImageOps.grayscale(line))
                factor = min(enlargement, 1200 / gray.width, 400 / gray.height)
                pixels = gray.resize(
                    (
                        max(1, round(gray.width * factor)),
                        max(1, round(gray.height * factor)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                pixels = ImageOps.expand(pixels, border=20, fill="white")
                buffer = io.BytesIO()
                pixels.save(buffer, format="JPEG", quality=95)
                encoded = buffer.getvalue()
                raw = _tesseract(
                    [
                        "stdin",
                        "stdout",
                        "-l",
                        "eng",
                        "--psm",
                        "7",
                        "-c",
                        "load_system_dawg=0",
                        "-c",
                        "load_freq_dawg=0",
                        "tsv",
                    ],
                    encoded,
                )
                regions = text_regions(raw, pixels.width, pixels.height)
                attempt = {
                    "text": " ".join(region["text"] for region in regions),
                    "confidence": min(
                        (region["confidence"] for region in regions), default=0
                    ),
                    "width": pixels.width,
                    "height": pixels.height,
                    "scale": factor,
                    "border": 20,
                    "text_crop": text_crop,
                    "image_sha256": hashlib.sha256(encoded).hexdigest(),
                }
                if len(attempt["text"].encode()) > 4096:
                    raise ParserFailure("extracted_text_limit")
                if preview_bytes + len(encoded) <= MAX_CROP_PREVIEW_BYTES:
                    attempt["image"] = base64.b64encode(encoded).decode("ascii")
                    preview_bytes += len(encoded)
                attempts.append(attempt)
            candidate |= {
                "rectified_size": list(size),
                "attempts": attempts,
            }
    return prepared | {
        "candidates": candidates,
        "limited": limited,
        "engine": engine,
        "detector": f"OpenCV {cv2.__version__} geometric quadrilaterals v1",
    }
