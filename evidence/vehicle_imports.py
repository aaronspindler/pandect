"""Bounded, deterministic parsing of Asset Location CSV exports; no network calls."""

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError

HEADERS = (
    "Asset",
    "Serial Number",
    "Address",
    "Speed",
    "Heading",
    "Trigger",
    "Time Summary",
    "Created Time",
    "Received Time",
)
MAX_ROWS = 250
MAX_BYTES = 1024 * 1024
PARSER_VERSION = "asset-location.v1"


@dataclass
class ImportRow:
    number: int
    raw: dict
    values: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    duplicate: bool = False

    @property
    def asset(self):
        return self.raw.get("Asset", "")

    @property
    def serial(self):
        return self.raw.get("Serial Number", "")

    @property
    def group(self):
        return (self.asset, self.serial)

    @property
    def fingerprint(self):
        return row_fingerprint(self.raw)


def row_fingerprint(raw):
    return json.dumps(raw, sort_keys=True, ensure_ascii=False)


def parse_timestamp(value, timezone_name):
    try:
        local = datetime.strptime(value.strip(), "%Y-%m-%d, %I:%M:%S %p")
    except ValueError:
        raise ValueError("Use a timestamp such as 2026-09-16, 2:57:22 PM.") from None
    zone = ZoneInfo(timezone_name)
    # A round trip distinguishes both repeated autumn times and missing spring times.
    candidates = {
        aware.astimezone(UTC)
        for fold in (0, 1)
        if (aware := local.replace(tzinfo=zone, fold=fold))
        .astimezone(UTC)
        .astimezone(zone)
        .replace(tzinfo=None)
        == local
    }
    if len(candidates) != 1:
        raise ValueError(
            "This local time is ambiguous or does not exist in the export timezone. "
            "Obtain an export with unambiguous times before importing."
        )
    return candidates.pop()


def parse_csv(content, timezone_name):
    if not content or len(content) > min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES):
        raise ValidationError("Choose a nonempty CSV within the import size limit.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValidationError("Save the CSV as UTF-8 and select it again.") from None
    if "\x00" in text:
        raise ValidationError("The CSV contains unsupported null characters.")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = []
    try:
        header = next(reader, [])
        if len(header) != len(HEADERS) or set(header) != set(HEADERS):
            raise ValidationError("Use the nine-column Asset Location CSV export.")
        for number, cells in enumerate(reader, 2):
            if not cells:
                continue
            if len(rows) >= MAX_ROWS:
                raise ValidationError(
                    "Import up to 250 rows at a time. Split this CSV."
                )
            row = ImportRow(number, dict(zip(header, cells, strict=False)))
            rows.append(row)
            if len(cells) != len(header):
                row.errors.append("This row must contain exactly nine columns.")
                continue
            for name, value in row.raw.items():
                limit = (
                    20000 if name == "Address" else 2000 if name == "Trigger" else 255
                )
                if len(value) > limit:
                    row.errors.append(f"{name} exceeds {limit:,} characters.")
            if not row.asset.strip() and not row.serial.strip():
                row.errors.append("An asset label or serial number is required.")
            if not row.raw["Address"].strip():
                row.errors.append("A reported address is required.")
            row.values["address"] = row.raw["Address"]
            for column, name in (
                ("Created Time", "observed_at"),
                ("Received Time", "received_at"),
            ):
                if column == "Received Time" and not row.raw[column].strip():
                    row.values[name] = None
                    continue
                try:
                    row.values[name] = parse_timestamp(row.raw[column], timezone_name)
                except ValueError as error:
                    row.errors.append(f"{column}: {error}")
            speed = row.raw["Speed"].strip()
            row.values["speed_kmh"] = None
            if speed:
                match = re.fullmatch(r"(\d{1,6}(?:\.\d{1,3})?)\s*km/h", speed)
                if match:
                    row.values["speed_kmh"] = Decimal(match[1])
                else:
                    row.errors.append(
                        "Speed must be a nonnegative km/h value, or blank."
                    )
            heading = row.raw["Heading"].strip().upper()
            if heading and heading not in {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}:
                row.errors.append(
                    "Heading must be N, NE, E, SE, S, SW, W, NW, or blank."
                )
    except csv.Error:
        raise ValidationError(
            "The CSV is malformed or contains an oversized field."
        ) from None
    if not rows:
        raise ValidationError("The CSV contains no observations.")
    seen = set()
    for row in rows:
        row.duplicate = row.fingerprint in seen
        seen.add(row.fingerprint)
    return rows


def read_import(batch):
    if batch.parser_version != PARSER_VERSION:
        raise ValidationError("This import uses an unsupported parser version.")
    with batch.document.file.open("rb") as original:
        content = original.read(min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES) + 1)
    # Detect corruption before interpreting a retained source again.
    if hashlib.sha256(content).hexdigest() != batch.document.sha256:
        raise ValidationError(
            "The source CSV failed its integrity check. Restore its original bytes."
        )
    return parse_csv(content, batch.timezone)
