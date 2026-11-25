"""Interpret the seven-column phone and FaceTime call history CSV profile."""

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from core.storage import canonical_bytes

HEADERS = ("Call type", "Date", "Duration", "Number", "Contact", "Location", "Service")
PARSER_VERSION = "call-history-csv.v1"
MAX_ROWS = 20_000
MAX_BYTES = 16 * 1024 * 1024
MAX_FIELD_CHARS = 4096
MAX_RECORD_CHARS = 64 * 1024
UUID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
SERVICE_PATTERN = re.compile(
    rf"(Phone|FaceTime Audio|FaceTime Video)(?:: {UUID_PATTERN})?"
)
TIMESTAMP_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
DURATION_PATTERN = re.compile(r"([0-9]{2}):([0-5][0-9]):([0-5][0-9])")
NUMBER_PATTERN = re.compile(r"\+?[0-9(). -]{1,64}")


@dataclass
class ParsedCall:
    number: int
    raw: dict
    kind: str = ""
    direction: str = ""
    occurred_at: datetime | None = None
    duration: timedelta | None = None
    identifier: str = ""
    fingerprint: str = ""
    issue: str = ""


class _RecordLines:
    """Limit each logical record before the CSV reader allocates its cells."""

    def __init__(self, stream):
        self.stream = stream
        self.record_chars = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.stream.readline(MAX_RECORD_CHARS + 1)
        if not line:
            raise StopIteration
        self.record_chars += len(line)
        if self.record_chars > MAX_RECORD_CHARS:
            raise ValidationError("The CSV contains an oversized record.")
        return line


def import_file_limit():
    return min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES)


def export_zone(name):
    try:
        return ZoneInfo(name)
    except TypeError, ValueError, ZoneInfoNotFoundError:
        raise ValidationError("Choose a valid export timezone.") from None


def _timestamp(raw, zone):
    try:
        if not TIMESTAMP_PATTERN.fullmatch(raw):
            raise ValueError
        local = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        candidates = set()
        for fold in (0, 1):
            utc = local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates.add(utc)
    except ValueError, OverflowError:
        raise ValueError(
            "Date must be a valid local timestamp such as 2025-02-10 15:30:00."
        ) from None
    if len(candidates) == 2:
        raise ValueError("This date occurs twice in the export timezone.")
    if not candidates:
        raise ValueError("This date does not exist in the export timezone.")
    return candidates.pop()


def _counterparty(raw):
    value = raw.strip()
    if NUMBER_PATTERN.fullmatch(value) and any(
        character.isdigit() for character in value
    ):
        return value
    if len(value) <= 254 and "@" in value:
        try:
            validate_email(value)
        except ValidationError:
            pass
        else:
            return value
    return ""


def _parse_row(number, header, cells, zone):
    row = ParsedCall(number, dict(zip(header, cells, strict=False)))
    if len(cells) != len(header):
        if len(cells) > len(header):
            row.raw["Extra columns"] = cells[len(header) :]
        row.issue = "This row must contain seven columns."
        return row
    direction = row.raw["Call type"]
    service = SERVICE_PATTERN.fullmatch(row.raw["Service"])
    duration = DURATION_PATTERN.fullmatch(row.raw["Duration"])
    if direction not in ("Incoming", "Outgoing"):
        row.issue = "Call type must be Incoming or Outgoing."
    elif service is None:
        row.issue = "Service must report Phone, FaceTime Audio, or FaceTime Video."
    elif duration is None:
        row.issue = "Duration must use hours, minutes, and seconds such as 00:01:30."
    else:
        try:
            row.occurred_at = _timestamp(row.raw["Date"], zone)
        except ValueError as error:
            row.issue = str(error)
    if row.issue:
        return row
    hours, minutes, seconds = map(int, duration.groups())
    row.duration = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    row.kind = "phone-call" if service[1] == "Phone" else "facetime"
    row.direction = direction.lower()
    row.identifier = _counterparty(row.raw["Number"])
    if row.identifier:
        row.fingerprint = hashlib.sha256(
            canonical_bytes(
                [
                    row.occurred_at.isoformat(),
                    row.identifier,
                    row.direction,
                    row.raw["Service"],
                    int(row.duration.total_seconds()),
                ]
            )
        ).hexdigest()
    return row


def parse_call_history(content, timezone_name):
    """Read an already byte-bounded original and give held rows their source ordinal."""
    zone = export_zone(timezone_name)
    if not 0 < len(content) <= import_file_limit():
        raise ValidationError("Choose a CSV within the call history import size limit.")
    try:
        with io.StringIO(content.decode("utf-8-sig"), newline="") as stream:
            lines = _RecordLines(stream)
            reader = csv.reader(lines, strict=True)
            header = next(reader, [])
            if len(header) != len(HEADERS) or set(header) != set(HEADERS):
                raise ValidationError("Use the seven-column call history CSV export.")
            lines.record_chars = 0
            for number, cells in enumerate(reader, 2):
                lines.record_chars = 0
                if number > MAX_ROWS + 1:
                    raise ValidationError(
                        "Import up to 20,000 call history rows at a time."
                    )
                if not cells:
                    continue
                if any(
                    len(value) > MAX_FIELD_CHARS or "\0" in value for value in cells
                ):
                    raise ValidationError(
                        "The CSV contains an oversized field or null character."
                    )
                yield _parse_row(number, header, cells, zone)
    except UnicodeDecodeError, csv.Error:
        raise ValidationError("Use a well-formed UTF-8 CSV.") from None
