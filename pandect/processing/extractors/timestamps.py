"""Conservative parsing of metadata timestamps into reviewable claims."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

_DATE_TIME_PATTERN = re.compile(
    r"^\s*(?P<year>\d{4})[:-](?P<month>\d{2})[:-](?P<day>\d{2})"
    r"(?:[ T](?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2})(?:\.(?P<fraction>\d{1,9}))?)?)?"
    r"(?:\s*(?P<offset>Z|UTC|[+-]\d{2}:?\d{2}))?\s*$",
    re.IGNORECASE,
)
_PARTIAL_DATE_PATTERN = re.compile(r"^\s*(?P<year>\d{4})(?:[:-](?P<month>\d{2}))?\s*$")
_OFFSET_PATTERN = re.compile(r"^(?P<sign>[+-])(?P<hours>\d{2}):?(?P<minutes>\d{2})$")

_TIMESTAMP_TAGS = {
    "datetime",
    "datetimeoriginal",
    "datetimedigitized",
    "createdate",
    "modifydate",
    "creationdate",
    "creationtime",
    "date",
    "datecreated",
    "digitalcreationdatetime",
    "mediacreatedate",
    "mediamodifydate",
    "trackcreatedate",
    "trackmodifydate",
    "contentcreatedate",
    "encodeddate",
    "taggeddate",
    "gpsdatetime",
    "gpsdatestamp",
    "filecreatedate",
    "filemodifydate",
    "fileaccessdate",
    "fileinodechangedate",
    "comapplequicktimecreationdate",
}


def _suffix(tag: str) -> str:
    return tag.rsplit(":", 1)[-1]


def _normalized_tag_name(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _suffix(tag).casefold())


def _namespace(tag: str) -> str:
    return tag.rsplit(":", 1)[0] if ":" in tag else "metadata"


def _timezone(value: str | None) -> timezone | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if normalized in {"Z", "UTC", "+00:00", "+0000"}:
        return UTC
    match = _OFFSET_PATTERN.match(normalized)
    if not match:
        return None
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    if hours > 23 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    if match.group("sign") == "-":
        delta = -delta
    try:
        return timezone(delta)
    except ValueError:
        return None


@dataclass(frozen=True)
class ParsedTimestamp:
    value: datetime | date | None
    precision: str
    timezone_evidence: str

    @property
    def normalized_utc(self) -> datetime | None:
        if not isinstance(self.value, datetime) or self.value.tzinfo is None:
            return None
        return self.value.astimezone(UTC)


@dataclass(frozen=True)
class TimestampClaimValue:
    """A candidate timestamp; never an instruction to update ``occurred_at``."""

    namespace: str
    tag: str
    raw_value: Any
    parsed_value: datetime | date | None
    precision: str
    timezone_evidence: str
    semantic: str
    confidence: float
    operational: bool = False
    conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        normalized_utc = None
        if isinstance(self.parsed_value, datetime) and self.parsed_value.tzinfo:
            normalized_utc = self.parsed_value.astimezone(UTC).isoformat()
        return {
            "namespace": self.namespace,
            "tag": self.tag,
            "raw_value": self.raw_value,
            "parsed_value": (
                self.parsed_value.isoformat() if self.parsed_value else None
            ),
            "normalized_utc": normalized_utc,
            "precision": self.precision,
            "timezone_evidence": self.timezone_evidence,
            "semantic": self.semantic,
            "confidence": self.confidence,
            "operational": self.operational,
            "conflicts": list(self.conflicts),
        }


def parse_timestamp_value(
    value: Any,
    *,
    offset: str | None = None,
    subsecond: str | int | None = None,
) -> ParsedTimestamp:
    """Parse common EXIF/QuickTime forms without guessing a timezone."""
    if not isinstance(value, str):
        return ParsedTimestamp(None, "unknown", "unparsed")

    partial = _PARTIAL_DATE_PATTERN.match(value)
    if partial:
        year = int(partial.group("year"))
        month_value = partial.group("month")
        try:
            parsed_date = date(year, int(month_value or 1), 1)
        except ValueError:
            return ParsedTimestamp(None, "unknown", "unparsed")
        return ParsedTimestamp(
            parsed_date,
            "month" if month_value else "year",
            "not_applicable",
        )

    match = _DATE_TIME_PATTERN.match(value)
    if not match:
        return ParsedTimestamp(None, "unknown", "unparsed")

    hour = match.group("hour")
    minute = match.group("minute")
    second = match.group("second")
    fraction = match.group("fraction")
    if fraction is None and subsecond is not None:
        fraction = re.sub(r"\D", "", str(subsecond)) or None
    microsecond = int((fraction or "")[:6].ljust(6, "0")) if fraction else 0
    embedded_offset = match.group("offset")
    supplied_offset = embedded_offset or offset
    unknown_offset = (supplied_offset or "").strip() in {"-00:00", "-0000"}
    tzinfo = _timezone(None if unknown_offset else supplied_offset)
    if supplied_offset and tzinfo is None and not unknown_offset:
        return ParsedTimestamp(None, "unknown", "invalid_offset")

    try:
        if hour is None:
            parsed: datetime | date = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            return ParsedTimestamp(parsed, "date", "not_applicable")
        parsed = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(hour),
            int(minute),
            int(second or 0),
            microsecond,
            tzinfo=tzinfo,
        )
    except ValueError:
        return ParsedTimestamp(None, "unknown", "unparsed")

    precision = "subsecond" if fraction else ("second" if second else "minute")
    return ParsedTimestamp(
        parsed,
        precision,
        "explicit_offset"
        if tzinfo
        else ("unknown_offset" if unknown_offset else "naive"),
    )


def _matching_tag(tags: Mapping[str, Any], source_tag: str, suffix: str) -> Any:
    source_namespace = _namespace(source_tag)
    for key, value in tags.items():
        if _namespace(key) == source_namespace and _normalized_tag_name(key) == suffix:
            return value
    for key, value in tags.items():
        if _normalized_tag_name(key) == suffix:
            return value
    return None


def _auxiliary_suffixes(tag_suffix: str) -> tuple[str | None, str | None]:
    normalized = _normalized_tag_name(tag_suffix)
    if normalized == "datetimeoriginal":
        return "offsettimeoriginal", "subsectimeoriginal"
    if normalized in {"datetimedigitized", "createdate"}:
        return "offsettimedigitized", "subsectimedigitized"
    if normalized in {"datetime", "modifydate"}:
        return "offsettime", "subsectime"
    return None, None


def _semantic(tag_suffix: str) -> str:
    normalized = _normalized_tag_name(tag_suffix)
    if normalized.startswith("file"):
        return "filesystem"
    if normalized.startswith("gps") or normalized in {
        "datetimeoriginal",
        "datetimedigitized",
        "contentcreatedate",
        "datecreated",
        "digitalcreationdatetime",
    }:
        return "capture"
    if "create" in normalized or normalized in {"creationdate", "creationtime"}:
        return "creation"
    if "modify" in normalized or "encoded" in normalized or "tagged" in normalized:
        return "modification"
    return "unspecified"


def _gps_claim(tags: Mapping[str, Any]) -> TimestampClaimValue | None:
    date_key = next(
        (key for key in tags if _normalized_tag_name(key) == "gpsdatestamp"),
        None,
    )
    time_key = next(
        (key for key in tags if _normalized_tag_name(key) == "gpstimestamp"),
        None,
    )
    if date_key is None or time_key is None:
        return None
    raw_date = tags[date_key]
    raw_time = tags[time_key]
    if not isinstance(raw_date, str):
        return None
    if isinstance(raw_time, (list, tuple)) and len(raw_time) >= 3:
        raw_time = ":".join(str(part) for part in raw_time[:3])
    if not isinstance(raw_time, str):
        return None
    combined = f"{raw_date.strip()} {raw_time.strip()}Z"
    parsed = parse_timestamp_value(combined)
    if not isinstance(parsed.value, datetime):
        return None
    return TimestampClaimValue(
        namespace=_namespace(date_key),
        tag="GPSDateStamp+GPSTimeStamp",
        raw_value={"date": tags[date_key], "time": tags[time_key]},
        parsed_value=parsed.value,
        precision=parsed.precision,
        timezone_evidence="gps_utc",
        semantic="capture",
        confidence=0.95,
    )


def timestamp_claims_from_tags(
    tags: Mapping[str, Any],
) -> tuple[TimestampClaimValue, ...]:
    """Return timestamp claims while preserving naive and unparsed values."""
    claims: list[TimestampClaimValue] = []
    for tag in sorted(tags):
        tag_suffix = _suffix(tag)
        normalized_suffix = _normalized_tag_name(tag_suffix)
        if normalized_suffix not in _TIMESTAMP_TAGS:
            continue
        offset_suffix, subsecond_suffix = _auxiliary_suffixes(tag_suffix)
        offset_value = (
            _matching_tag(tags, tag, offset_suffix) if offset_suffix else None
        )
        subsecond_value = (
            _matching_tag(tags, tag, subsecond_suffix) if subsecond_suffix else None
        )
        parsed = parse_timestamp_value(
            tags[tag],
            offset=str(offset_value) if offset_value is not None else None,
            subsecond=subsecond_value,
        )
        operational = normalized_suffix.startswith("file")
        if operational:
            # Filesystem timestamps can be precisely offset-aware while still
            # saying nothing reliable about when the evidence was captured.
            confidence = 0.2
        elif parsed.timezone_evidence == "explicit_offset":
            confidence = 0.9
        else:
            confidence = 0.5
        claims.append(
            TimestampClaimValue(
                namespace=_namespace(tag),
                tag=tag_suffix,
                raw_value=tags[tag],
                parsed_value=parsed.value,
                precision=parsed.precision,
                timezone_evidence=parsed.timezone_evidence,
                semantic=_semantic(tag_suffix),
                confidence=confidence,
                operational=operational,
            )
        )

    gps_claim = _gps_claim(tags)
    if gps_claim:
        claims.append(gps_claim)
    return mark_timestamp_conflicts(tuple(claims))


def timestamp_claims_from_ffprobe(
    value: Mapping[str, Any] | Any,
) -> tuple[TimestampClaimValue, ...]:
    """Collect timestamp claims from normalized or raw FFprobe JSON.

    The function accepts ``FFProbeMetadata`` by duck typing to avoid coupling the
    parsing helpers. Container and stream tag namespaces remain distinguishable.
    """
    raw = getattr(value, "raw", value)
    if not isinstance(raw, Mapping):
        raise ValueError("FFprobe metadata must be a mapping")
    tags: dict[str, Any] = {}
    format_value = raw.get("format", {})
    if isinstance(format_value, Mapping) and isinstance(
        format_value.get("tags"), Mapping
    ):
        for key, tag_value in format_value["tags"].items():
            tags[f"FFprobe:Format:{key}"] = tag_value
    streams = raw.get("streams", [])
    if isinstance(streams, list):
        for fallback_index, stream in enumerate(streams):
            if not isinstance(stream, Mapping) or not isinstance(
                stream.get("tags"), Mapping
            ):
                continue
            stream_index = stream.get("index", fallback_index)
            for key, tag_value in stream["tags"].items():
                tags[f"FFprobe:Stream{stream_index}:{key}"] = tag_value
    return timestamp_claims_from_tags(tags)


def mark_timestamp_conflicts(
    claims: tuple[TimestampClaimValue, ...],
    *,
    tolerance_seconds: float = 2.0,
) -> tuple[TimestampClaimValue, ...]:
    """Mark materially different aware capture times without choosing one."""
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds cannot be negative")
    capture = [
        claim
        for claim in claims
        if claim.semantic == "capture"
        and isinstance(claim.parsed_value, datetime)
        and claim.parsed_value.tzinfo is not None
    ]
    result = []
    for claim in claims:
        conflicting_tags: list[str] = []
        if claim in capture:
            claim_utc = claim.parsed_value.astimezone(UTC)
            for other in capture:
                if other is claim:
                    continue
                other_utc = other.parsed_value.astimezone(UTC)
                if abs((claim_utc - other_utc).total_seconds()) > tolerance_seconds:
                    conflicting_tags.append(f"{other.namespace}:{other.tag}")
        result.append(replace(claim, conflicts=tuple(sorted(conflicting_tags))))
    return tuple(result)
