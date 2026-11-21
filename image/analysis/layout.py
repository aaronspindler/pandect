"""Infer visible structure from the bounded, printed-English OCR word regions."""

import math
import re
from collections import Counter
from statistics import median

from core.analysis.errors import AnalysisError
from image.analysis import MAX_LAYOUT_LINES, MAX_LAYOUT_WORDS

GEOMETRY_LIMIT = (
    "Layout was omitted because this image exceeds the word or line limit. "
    "The OCR lines remain available."
)
CLOCK = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d(?:[ap]\.?m\.?)?", re.I)
NUMBER = re.compile(r"[$£€]?\d+(?:[.,]\d+)*%?$")
TABLE_HEADINGS = {
    "amount",
    "category",
    "code",
    "date",
    "description",
    "item",
    "name",
    "price",
    "quantity",
    "qty",
    "role",
    "status",
    "type",
    "unit",
    "value",
}


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _rectangle(box):
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(_number(n) for n in box)
        and 0 <= box[0] < box[2] <= 1
        and 0 <= box[1] < box[3] <= 1
    )


def _words(regions, first_item):
    """Validate each reference against its own canonical line before using it."""
    words, literals = [], []
    for index, line in enumerate(regions):
        box = line["bbox"]
        parts = line.get("words")
        paragraph = line.get("paragraph")
        if (
            not _rectangle(box)
            or type(line["order"]) is not int
            or line["order"] != index
            or not isinstance(line["text"], str)
            or not isinstance(paragraph, list)
            or len(paragraph) != 3
            or any(type(group) is not int or group < 1 for group in paragraph)
            or not isinstance(parts, list)
            or not parts
            or len(words) + len(parts) > MAX_LAYOUT_WORDS
        ):
            raise AnalysisError("ocr_invalid_output")
        cursor = 0
        for part in parts:
            start, end = part["start"], part["end"]
            region, score = part["region"], part["confidence"]
            if (
                type(start) is not int
                or type(end) is not int
                or start != cursor
                or not start < end <= len(line["text"])
                or (start and line["text"][start - 1] != " ")
                or not line["text"][start:end].strip()
                or not _number(score)
                or not 0 <= score <= 100
                or not _rectangle(region)
                or not (box[0] <= region[0] < region[2] <= box[2])
                or not (box[1] <= region[1] < region[3] <= box[3])
            ):
                raise AnalysisError("ocr_invalid_output")
            words.append(
                {
                    "item": first_item + index,
                    "start": start,
                    "end": end,
                    "region": region,
                    "confidence": score,
                }
            )
            literals.append(line["text"][start:end])
            cursor = end + 1
        if cursor != len(line["text"]) + 1 or line["confidence"] != min(
            word["confidence"] for word in parts
        ):
            raise AnalysisError("ocr_invalid_output")
    return words, literals


def _visual_rows(boxes, literals):
    """Group words on a common baseline, then split at broad horizontal gutters."""
    rows = []
    centers = [(top + bottom) / 2 for _, top, _, bottom in boxes]
    char_width = median(
        (right - left) / len(literal)
        for (left, _, right, _), literal in zip(boxes, literals, strict=True)
    )
    for index in sorted(range(len(boxes)), key=centers.__getitem__):
        top, bottom = boxes[index][1::2]
        if not rows or centers[index] - centers[rows[-1][0]] > (bottom - top) * 0.6:
            rows.append([])
        rows[-1].append(index)
    grouped = []
    for row in rows:
        row.sort(key=lambda i: boxes[i][0])
        cells = []
        for i in row:
            if not cells or boxes[i][0] - boxes[cells[-1][-1]][2] > max(
                0.025, char_width * 3.5
            ):
                cells.append([])
            cells[-1].append(i)
        grouped.append(cells)
    return grouped


def _clock_headers(rows, literals):
    """Find short, time-labelled headers, withholding conflicting readings."""
    headers = {}
    for index, row in enumerate(rows):
        times = [j for j, i in enumerate(row) if CLOCK.fullmatch(literals[i])]
        if times and 0 < times[0] <= 4:
            split = times[0]
            suffix = [literals[i] for i in row[split + 1 :]]
            if all(
                CLOCK.fullmatch(token)
                or token.casefold().replace(".", "") in {"am", "pm"}
                for token in suffix
            ):
                headers[index] = (row[:split], row[split:], len(times) > 1)
    return headers


def _form_label(row, literals):
    ends = [j for j, i in enumerate(row) if literals[i].endswith(":")]
    return ends[0] + 1 if len(ends) == 1 and ends[0] < 6 else 0


def _form_reading(row, cells, split, repeated):
    """Retain competing values on the same baseline as separate readings."""
    notes = []
    if split == len(row):
        notes.append("A value is missing or unreadable.")
    if repeated:
        notes.append("Repeated label. Its intended value is unresolved.")
    value_words = set(row[split:])
    values = [
        [word for word in cell if word in value_words]
        for cell in cells
        if cell[-1] in value_words
    ]
    role = "Value"
    if len(values) > 1:
        role = "Possible value"
        notes.append("Several separated values could belong to this label.")
    return [("Label", row[:split]), *((role, cell) for cell in values)], notes


def _aligned_columns(a, b, boxes):
    return len(a) == len(b) and all(
        min(
            abs(boxes[x[0]][0] - boxes[y[0]][0]),
            abs(boxes[x[-1]][2] - boxes[y[-1]][2]),
        )
        <= 0.04
        for x, y in zip(a, b, strict=True)
    )


def _table_columns(cells, boxes, nearby, reserved):
    """Find recurring columns, allowing one intervening incomplete row."""
    tables = {}
    for i, row in enumerate(cells):
        if len(row) < 2 or i in reserved:
            continue
        for j in range(i + 1, min(i + 3, len(cells))):
            if j in reserved or not nearby(j - 1, j):
                break
            if _aligned_columns(row, cells[j], boxes):
                for k in range(i, j + 1):
                    tables[k] = row
                break
    return tables


def _numeric_or_headed(run, literals):
    headings = [
        " ".join(literals[i].casefold() for i in part).strip(":") for part in run[0]
    ]
    if len(headings) >= 2 and all(heading in TABLE_HEADINGS for heading in headings):
        return True
    return (
        sum(
            any(
                NUMBER.fullmatch("".join(literals[i] for i in cell))
                for cell in parts[1:]
            )
            for parts in run
        )
        >= 2
    )


def _table_reading(cells, columns, literals, boxes):
    notes = []
    if not _aligned_columns(cells, columns, boxes):
        notes.append("The visible cells do not match the surrounding columns.")
    if any(
        sum(bool(NUMBER.fullmatch(literals[i])) for i in cell) > 1 for cell in cells[1:]
    ):
        notes.append("A cell contains competing numbers.")
    fields = [
        ("Visible cell" if notes else f"Cell {col}", cell)
        for col, cell in enumerate(cells, 1)
    ]
    return fields, notes


def build_structure(regions, first_item):
    """Attach only indices into these sources' OCR items and normalized word boxes."""
    empty = {"words": [], "candidates": [], "orders": [], "notes": []}
    if not regions:
        return {}
    if regions[0].get("layout_omitted") or len(regions) > MAX_LAYOUT_LINES:
        return empty | {"notes": [GEOMETRY_LIMIT]}
    try:
        words, literals = _words(regions, first_item)
    except KeyError, TypeError, ValueError, OverflowError:
        raise AnalysisError("ocr_invalid_output") from None

    boxes = [word["region"] for word in words]
    cells = _visual_rows(boxes, literals)
    rows = [[i for cell in row for i in cell] for row in cells]
    candidates, alternative_order = [], []

    def add(kind, fields, notes=()):
        position = len(candidates)
        fields = [
            {
                "role": role,
                "words": indices,
                "confidence": min(words[i]["confidence"] for i in indices),
            }
            for role, indices in fields
            if indices
        ]
        problems = list(notes)
        if any(
            min(boxes[i][0], boxes[i][1], 1 - boxes[i][2], 1 - boxes[i][3]) <= 0.005
            for field in fields
            for i in field["words"]
        ):
            problems.append("Text reaches the image edge. The context may be cropped.")
        candidates.append({"kind": kind, "fields": fields, "notes": problems})
        alternative_order.append(position)
        return position

    def nearby(a, b, gap=3):
        a_box, b_box = boxes[rows[a][0]], boxes[rows[b][0]]
        height = max(a_box[3] - a_box[1], b_box[3] - b_box[1])
        return b_box[1] - a_box[3] <= height * gap

    # A time in ordinary prose is insufficient. Look for repeated, short
    # participant/time header rows with body text immediately below.
    headers = _clock_headers(rows, literals)

    def label_text(row, split):
        return " ".join(literals[i].casefold() for i in row[:split])

    forms = {
        i: split for i, row in enumerate(rows) if (split := _form_label(row, literals))
    }
    labels = Counter(label_text(rows[i], split) for i, split in forms.items())
    bodies = {
        i: i + 1
        for i in headers
        if i + 1 < len(rows)
        and i + 1 not in headers
        and i + 1 not in forms
        and len(cells[i + 1]) == 1
        and nearby(i, i + 1)
        and abs(boxes[rows[i + 1][0]][0] - boxes[rows[i][0]][0]) <= 0.08
    }
    chat = len(headers) >= 2 and bool(bodies)
    reserved = set(forms)
    if chat:
        reserved.update(headers)
        reserved.update(bodies.values())
    tables = _table_columns(cells, boxes, nearby, reserved)
    paragraphs = [
        {tuple(regions[words[i]["item"] - first_item]["paragraph"]) for i in row}
        for row in rows
    ]

    def paragraph_lines(start, *, message=False):
        stop = start
        content = list(rows[start])
        notes = []
        while stop + 1 < len(rows) and nearby(stop, stop + 1):
            after = stop + 1
            if after in reserved or after in tables or len(cells[after]) > 1:
                break
            same_group = (
                len(paragraphs[start]) == 1 and paragraphs[after] == paragraphs[start]
            )
            if message:
                inset = abs(boxes[rows[after][0]][0] - boxes[rows[start][0]][0])
                if inset > 0.08:
                    break
                if not same_group or inset > 0.035 or not nearby(stop, after, gap=1.25):
                    notes.append("Message incomplete. The next line may continue it.")
                    break
            elif not same_group:
                break
            content.extend(rows[after])
            stop = after
        return stop, content, notes

    index = 0
    while index < len(rows):
        row = rows[index]
        if chat and index in headers:
            label, time, conflict = headers[index]
            notes = []
            fields = [("Participant label", label), ("Time label", time)]
            # Preserve a conflicting header and withhold the body association.
            if conflict:
                notes.append(
                    "Multiple visible time labels leave the association unresolved."
                )
            elif index in bodies:
                index, body, notes = paragraph_lines(bodies[index], message=True)
                fields.append(("Body", body))
            else:
                notes.append("The visible header has no unambiguous adjacent body.")
            add("message", fields, notes)
        elif index in forms:
            split = forms[index]
            repeated = labels[label_text(row, split)] > 1
            add("form_field", *_form_reading(row, cells[index], split, repeated))
        elif index in tables:
            columns = tables[index]
            stop = index + 1
            while (
                stop < len(rows)
                and stop in tables
                and _aligned_columns(tables[stop], columns, boxes)
                and nearby(stop - 1, stop)
            ):
                stop += 1
            run = cells[index:stop]
            # Pure text columns can be prose read down the page or rows read
            # across it. Keep both orders without assigning a value to a label.
            aligned_rows = [
                parts for parts in run if _aligned_columns(parts, columns, boxes)
            ]
            if aligned_rows and _numeric_or_headed(aligned_rows, literals):
                # A trailing row with a missing or extra cell may belong to
                # this table. Keep it unresolved when its left edge still fits.
                if (
                    stop < len(rows)
                    and stop not in reserved
                    and stop not in tables
                    and nearby(stop - 1, stop)
                    and len(cells[stop]) != len(columns)
                    and abs(boxes[rows[stop][0]][0] - boxes[row[0]][0]) <= 0.04
                ):
                    run.append(cells[stop])
                    stop += 1
                for parts in run:
                    add("table_row", *_table_reading(parts, columns, literals, boxes))
            else:
                by_column = []
                begin = len(candidates)
                for parts in run:
                    for col, part in enumerate(parts):
                        ref = add(
                            "block",
                            [("Text", part)],
                            ["Aligned text may be read across rows or down columns."],
                        )
                        by_column.append((col, ref))
                alternative_order[begin:] = [ref for _, ref in sorted(by_column)]
            index = stop - 1
        elif len(cells[index]) > 1:
            for part in cells[index]:
                add(
                    "block",
                    [("Text", part)],
                    ["The relationship between these separated blocks is unresolved."],
                )
        else:
            # Keep adjacent lines from one engine paragraph as an apparent
            # block. A specialist candidate always starts its own group.
            index, block, _ = paragraph_lines(index)
            add("block", [("Text", block)])
        index += 1

    original_order = list(range(len(candidates)))
    orders = []
    if alternative_order != original_order:
        orders = [
            {"label": "Across rows", "candidates": original_order},
            {"label": "Down columns", "candidates": alternative_order},
        ]
    return {"words": words, "candidates": candidates, "orders": orders, "notes": []}
