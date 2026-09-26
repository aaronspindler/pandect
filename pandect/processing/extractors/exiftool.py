"""ExifTool invocation and metadata parsing without canonical-field mutation."""

import json
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from os import PathLike
from typing import Any

from pandect.media_runner.routing import isolated
from pandect.processing.extractors.errors import InvalidExtractorOutputError
from pandect.processing.extractors.subprocesses import CommandResult, run_command

EXIFTOOL_TIMEOUT_SECONDS = 120
_DMS_PATTERN = re.compile(
    r"^\s*(?P<degrees>\d+(?:\.\d+)?)\s*(?:deg|°|\s)\s*"
    r"(?P<minutes>\d+(?:\.\d+)?)?\s*(?:['′]|\s)?\s*"
    r"(?P<seconds>\d+(?:\.\d+)?)?\s*(?:[\"″])?\s*"
    r"(?P<direction>[NSEW])?\s*$",
    re.IGNORECASE,
)
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
_LOCAL_PATH_TAGS = {"directory", "filepath", "sourcefile"}


def _invalid_json_constant(_value: str) -> None:
    raise ValueError


def _tag_suffix(key: str) -> str:
    return key.rsplit(":", 1)[-1]


def _find_tag(tags: Mapping[str, Any], *suffixes: str) -> Any:
    wanted = {suffix.casefold() for suffix in suffixes}
    for key, value in tags.items():
        if _tag_suffix(key).casefold() in wanted:
            return value
    return None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _coordinate(value: Any, reference: Any = None) -> float | None:
    number = _finite_float(value)
    direction = str(reference or "").strip().upper()[:1]
    if number is None and isinstance(value, str):
        match = _DMS_PATTERN.match(value)
        if match:
            degrees = float(match.group("degrees"))
            minutes = float(match.group("minutes") or 0)
            seconds = float(match.group("seconds") or 0)
            if minutes >= 60 or seconds >= 60:
                return None
            number = degrees + minutes / 60 + seconds / 3600
            direction = (match.group("direction") or direction).upper()
    if number is None:
        return None
    if direction in {"S", "W"}:
        number = -abs(number)
    return number


def _altitude(value: Any, reference: Any = None) -> float | None:
    number = _finite_float(value)
    if number is None and isinstance(value, str):
        match = _NUMBER_PATTERN.search(value)
        number = _finite_float(match.group(0)) if match else None
    if number is None:
        return None
    normalized_reference = str(reference or "").casefold()
    if normalized_reference in {"1", "below sea level"} or "below" in (
        normalized_reference
    ):
        return -abs(number)
    return number


@dataclass(frozen=True)
class ExifToolMetadata:
    """All extracted namespaced tags plus normalized GPS when defensible."""

    raw_tags: dict[str, Any]
    gps: dict[str, float] | None
    local_path_tags_redacted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_tags": deepcopy(self.raw_tags),
            "gps": deepcopy(self.gps),
            "local_path_tags_redacted": self.local_path_tags_redacted,
        }


def parse_exiftool_json(value: str | bytes) -> ExifToolMetadata:
    """Validate the one-file JSON response and preserve namespaced tags."""
    try:
        decoded = json.loads(value, parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise InvalidExtractorOutputError from error
    if (
        not isinstance(decoded, list)
        or len(decoded) != 1
        or not isinstance(decoded[0], dict)
    ):
        raise InvalidExtractorOutputError

    tags = deepcopy(decoded[0])
    redacted = False
    for key in tuple(tags):
        if _tag_suffix(key).casefold() in _LOCAL_PATH_TAGS:
            del tags[key]
            redacted = True

    latitude = _coordinate(
        _find_tag(tags, "GPSLatitude"),
        _find_tag(tags, "GPSLatitudeRef"),
    )
    longitude = _coordinate(
        _find_tag(tags, "GPSLongitude"),
        _find_tag(tags, "GPSLongitudeRef"),
    )
    gps = None
    if latitude is not None and longitude is not None:
        if -90 <= latitude <= 90 and -180 <= longitude <= 180:
            gps = {"latitude": latitude, "longitude": longitude}
            altitude = _altitude(
                _find_tag(tags, "GPSAltitude"),
                _find_tag(tags, "GPSAltitudeRef"),
            )
            if altitude is not None:
                gps["altitude_metres"] = altitude

    return ExifToolMetadata(
        raw_tags=tags,
        gps=gps,
        local_path_tags_redacted=redacted,
    )


@isolated("extract_metadata", source="path")
def extract_metadata(
    path: str | PathLike[str],
    *,
    timeout_seconds: float = EXIFTOOL_TIMEOUT_SECONDS,
    exiftool_binary: str = "exiftool",
    runner: Callable[..., CommandResult] = run_command,
) -> ExifToolMetadata:
    """Extract namespaced EXIF/IPTC/XMP/container tags from one local file."""
    result = runner(
        (
            exiftool_binary,
            "-json",
            "-G0:1",
            "-a",
            "-u",
            "-struct",
            "-api",
            "RequestAll=3",
            "--",
            path,
        ),
        timeout_seconds=timeout_seconds,
    )
    return parse_exiftool_json(result.stdout)
