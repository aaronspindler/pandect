"""Bounded geometric plate candidates and prepared recognition crops."""

import base64
import hashlib
import io
import math

from PIL import Image, ImageOps

from core.parser_contract import ParserFailure
from image.parsers import _image

MAX_CANDIDATES = 5
MAX_CONTOURS = 1000
MAX_CROP_PREVIEW_BYTES = 1024 * 1024


def plate_likeness(candidate, recognition_image):
    """Fixed ranking heuristic, not an estimated probability of a real plate."""
    import cv2
    import numpy as np

    with Image.open(io.BytesIO(recognition_image)) as image:
        gray = np.asarray(image.convert("L"))[20:-20, 20:-20]
    height, width = gray.shape
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    best = {
        "points": 0,
        "characters": 0,
        "span": 0,
        "baseline_deviation": 1,
        "density": 0,
    }
    for foreground in (binary, cv2.bitwise_not(binary)):
        _, _, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
        stats = stats[1:]
        widths, heights = stats[:, 2], stats[:, 3]
        glyphs = stats[
            (heights >= 0.35 * height)
            & (heights <= 0.98 * height)
            & (widths >= 0.08 * heights)
            & (widths <= 1.2 * heights)
            & (stats[:, 4] >= 0.1 * widths * heights)
        ]
        count = len(glyphs)
        span = (
            float((max(glyphs[:, 0] + glyphs[:, 2]) - min(glyphs[:, 0])) / width)
            if count
            else 0
        )
        deviation = (
            float(
                np.median(
                    np.abs(
                        glyphs[:, 1]
                        + glyphs[:, 3]
                        - np.median(glyphs[:, 1] + glyphs[:, 3])
                    )
                )
                / height
            )
            if count
            else 1
        )
        density = float(np.count_nonzero(foreground) / foreground.size)
        points = (
            60 * min(count / 5, 1) * min(span / 0.5, 1) * max(0, 1 - deviation / 0.15)
            if 3 <= count <= 12 and 0.03 <= density <= 0.65
            else 0
        )
        if points > best["points"] or (
            points == best["points"] and count > best["characters"]
        ):
            best = {
                "points": points,
                "characters": count,
                "span": span,
                "baseline_deviation": deviation,
                "density": density,
            }
    aspect = candidate["aspect_ratio"]
    geometry = 10 * candidate["rectangularity"] + 10 * min(
        1, (aspect - 1) / 0.8, (6.5 - aspect) / 2
    )
    return {
        "plate_score": round(max(0, min(100, 1.25 * (geometry + best["points"])))),
        "score_details": {
            "Scoring method": "plate-likeness.geometry.v2; uncalibrated heuristic",
            "Geometry points (0–20)": round(geometry, 2),
            "Panel aspect ratio": round(aspect, 3),
            "Rectangular fill fraction": round(candidate["rectangularity"], 3),
            "Character row points (0–60)": round(best["points"], 2),
            "Character-like components": best["characters"],
            "Character row width fraction": round(best["span"], 3),
            "Median baseline deviation fraction": round(best["baseline_deviation"], 3),
            "Foreground pixel fraction": round(best["density"], 3),
        },
    }


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
                "aspect_ratio": float(long / short),
                "rectangularity": float(area / (width * height)),
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
            text_crop = [0.02, 0.22, 0.98, 0.8]
            line = rectified.crop(
                tuple(
                    round(value * size[index % 2])
                    for index, value in enumerate(text_crop)
                )
            )
            gray = ImageOps.autocontrast(ImageOps.grayscale(line))
            factor = min(2, 1200 / gray.width, 400 / gray.height)
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
            attempt = {
                "width": pixels.width,
                "height": pixels.height,
                "scale": factor,
                "border": 20,
                "text_crop": text_crop,
                "image_sha256": hashlib.sha256(encoded).hexdigest(),
            }
            if preview_bytes + len(encoded) <= MAX_CROP_PREVIEW_BYTES:
                attempt["image"] = base64.b64encode(encoded).decode("ascii")
                preview_bytes += len(encoded)
            candidate |= {
                "rectified_size": list(size),
                "attempts": [attempt],
            } | plate_likeness(candidate, encoded)
    return prepared | {
        "candidates": candidates,
        "limited": limited,
        "detector": f"OpenCV {cv2.__version__} geometric quadrilaterals v1",
    }
