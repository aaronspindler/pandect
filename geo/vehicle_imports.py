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
MAX_ROWS = 100_000
MAX_BYTES = 100 * 1024 * 1024
MAX_GROUPS = 100
PARSER_VERSION = "asset-location.v2"
SUPPORTED_PARSER_VERSIONS = {"asset-location.v1", PARSER_VERSION}


@dataclass
class ImportRow:
    number: int
    raw: dict
    values: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

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
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


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


class BoundedReader(io.RawIOBase):
    """Adapt private storage to a streaming UTF-8 reader with a hard byte bound."""

    def __init__(self, original):
        super().__init__()
        self.original = original
        self.size = 0
        self.digest = hashlib.sha256()
        self.limit = min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES)

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.original.read(min(len(buffer), self.limit - self.size + 1))
        self.size += len(chunk)
        if self.size > self.limit:
            raise ValidationError("Choose a CSV within the import size limit.")
        self.digest.update(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


class BoundedCSVLines:
    """Bound physical lines and whole logical records before csv allocates cells."""

    def __init__(self, text):
        self.text = text
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.text.readline(64 * 1024 + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > 64 * 1024:
            raise ValidationError("The CSV contains an oversized record.")
        return line


def iter_csv(
    original,
    timezone_name,
    *,
    expected_sha256=None,
    expected_size=None,
    parser_version=PARSER_VERSION,
):
    """Yield one interpreted record at a time; verify retained bytes at EOF."""
    if parser_version not in SUPPORTED_PARSER_VERSIONS:
        raise ValidationError("This import uses an unsupported parser version.")
    missing_speeds = ("", "-") if parser_version == "asset-location.v2" else ("",)
    bounded = BoundedReader(original)
    with io.TextIOWrapper(
        io.BufferedReader(bounded), encoding="utf-8-sig", newline=""
    ) as text:
        lines = BoundedCSVLines(text)
        reader = csv.reader(lines, strict=True)
        count = 0
        try:
            header = next(reader, [])
            lines.record_chars = 0
            if len(header) != len(HEADERS) or set(header) != set(HEADERS):
                raise ValidationError("Use the nine-column Asset Location CSV export.")
            for number, cells in enumerate(reader, 2):
                lines.record_chars = 0
                if not cells:
                    continue
                count += 1
                if count > MAX_ROWS:
                    raise ValidationError(
                        "Import up to 100,000 observations in one CSV."
                    )
                if any("\x00" in value for value in cells):
                    raise ValidationError(
                        "The CSV contains unsupported null characters."
                    )
                row = ImportRow(number, dict(zip(header, cells, strict=False)))
                if len(cells) != len(header):
                    row.errors.append("This row must contain exactly nine columns.")
                    yield row
                    continue
                for name, value in row.raw.items():
                    limit = (
                        20000
                        if name == "Address"
                        else 2000
                        if name == "Trigger"
                        else 255
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
                        row.values[name] = parse_timestamp(
                            row.raw[column], timezone_name
                        )
                    except ValueError as error:
                        row.errors.append(f"{column}: {error}")
                speed = row.raw["Speed"].strip()
                row.values["speed_kmh"] = None
                if speed not in missing_speeds:
                    match = re.fullmatch(r"(\d{1,6}(?:\.\d{1,3})?)\s*km/h", speed)
                    if match:
                        row.values["speed_kmh"] = Decimal(match[1])
                    else:
                        row.errors.append(
                            "Speed must be a nonnegative km/h value, or blank."
                        )
                heading = row.raw["Heading"].strip().upper()
                if heading and heading not in {
                    "N",
                    "NE",
                    "E",
                    "SE",
                    "S",
                    "SW",
                    "W",
                    "NW",
                }:
                    row.errors.append(
                        "Heading must be N, NE, E, SE, S, SW, W, NW, or blank."
                    )
                yield row
        except UnicodeDecodeError:
            raise ValidationError(
                "Save the CSV as UTF-8 and select it again."
            ) from None
        except csv.Error:
            raise ValidationError(
                "The CSV is malformed or contains an oversized field."
            ) from None
    if not count:
        raise ValidationError("The CSV contains no observations.")
    if expected_sha256 is not None and (
        bounded.digest.hexdigest() != expected_sha256 or bounded.size != expected_size
    ):
        raise ValidationError(
            "The source CSV failed its integrity check. Restore its original bytes."
        )


def iter_import(batch):
    with batch.document.file.open("rb") as original:
        yield from iter_csv(
            original,
            batch.timezone,
            expected_sha256=batch.document.sha256,
            expected_size=batch.document.byte_size,
            parser_version=batch.parser_version,
        )
