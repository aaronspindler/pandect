from datetime import UTC, datetime, timedelta, timezone

import pytest

from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ToolFailedError,
)
from pandect.processing.extractors.exiftool import (
    extract_metadata,
    parse_exiftool_json,
)
from pandect.processing.extractors.ffprobe import parse_ffprobe_json, probe_media
from pandect.processing.extractors.subprocesses import CommandResult
from pandect.processing.extractors.tesseract import (
    extract_ocr,
    parse_tesseract_osd,
    parse_tesseract_tsv,
)
from pandect.processing.extractors.timestamps import (
    parse_timestamp_value,
    timestamp_claims_from_ffprobe,
    timestamp_claims_from_tags,
)

FFPROBE_JSON = b"""
{
  "streams": [
    {
      "index": 0,
      "codec_name": "h264",
      "codec_type": "video",
      "width": 1920,
      "height": 1080,
      "avg_frame_rate": "30000/1001",
      "duration": "12.500000",
      "tags": {"creation_time": "2026-08-12T12:00:00Z"},
      "unknown_future_field": "preserved"
    },
    {
      "index": 1,
      "codec_name": "aac",
      "codec_type": "audio",
      "sample_rate": "48000",
      "channels": 2
    }
  ],
  "chapters": [
    {"id": 0, "start_time": "0.0", "end_time": "10.0", "tags": {"title": "One"}}
  ],
  "format": {
    "filename": "/private/evidence/clip.mp4",
    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    "duration": "12.500000",
    "size": "1000",
    "bit_rate": "640"
  }
}
"""


def test_parse_ffprobe_preserves_unknown_fields_and_normalizes_numbers():
    metadata = parse_ffprobe_json(FFPROBE_JSON)

    assert metadata.technical["format"]["duration_seconds"] == 12.5
    assert metadata.technical["format"]["size_bytes"] == 1000
    assert metadata.streams[0]["average_frame_rate_value"] == pytest.approx(29.97002997)
    assert metadata.streams[1]["sample_rate"] == 48000
    assert metadata.chapters[0]["end_time_seconds"] == 10.0
    assert metadata.raw["streams"][0]["unknown_future_field"] == "preserved"
    assert "filename" not in metadata.raw["format"]
    assert metadata.technical["source_path_redacted"] is True


@pytest.mark.parametrize(
    "value",
    [
        b"not json",
        b"[]",
        b'{"streams": {}}',
        b'{"chapters": [1]}',
        b'{"format": {"duration": NaN}}',
    ],
)
def test_parse_ffprobe_rejects_invalid_shapes(value):
    with pytest.raises(InvalidExtractorOutputError):
        parse_ffprobe_json(value)


def test_probe_media_invokes_ffprobe_with_explicit_input_option(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        return CommandResult(FFPROBE_JSON, b"", 0)

    path = tmp_path / "-untrusted-name.mp4"
    metadata = probe_media(path, runner=fake_runner)

    assert metadata.streams[0]["codec_type"] == "video"
    assert calls[0][0][-2:] == ("-i", path)
    assert calls[0][1] == {"timeout_seconds": 120}


def test_parse_exiftool_preserves_namespaced_tags_and_normalizes_dms_gps():
    metadata = parse_exiftool_json(
        b"""[
          {
            "SourceFile": "/private/evidence/photo.jpg",
            "File:System:Directory": "/private/evidence",
            "EXIF:ExifIFD:DateTimeOriginal": "2026:08:12 10:11:12",
            "EXIF:GPS:GPSLatitude": "43 deg 39' 12.00\\\" N",
            "EXIF:GPS:GPSLongitude": "79 deg 22' 30.00\\\" W",
            "EXIF:GPS:GPSAltitude": "75.5 m",
            "EXIF:GPS:GPSAltitudeRef": "Above Sea Level"
          }
        ]"""
    )

    assert metadata.gps == {
        "latitude": pytest.approx(43.6533333333),
        "longitude": pytest.approx(-79.375),
        "altitude_metres": 75.5,
    }
    assert "EXIF:ExifIFD:DateTimeOriginal" in metadata.raw_tags
    assert not any("Directory" in key for key in metadata.raw_tags)
    assert metadata.local_path_tags_redacted is True


@pytest.mark.parametrize("value", [b"{}", b"[]", b"[1]", b'[{"Value": NaN}]'])
def test_parse_exiftool_rejects_invalid_shapes_and_nonfinite_values(value):
    with pytest.raises(InvalidExtractorOutputError):
        parse_exiftool_json(value)


def test_extract_metadata_uses_complete_namespaced_exiftool_request(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        return CommandResult(b"[{}]", b"", 0)

    path = tmp_path / "photo.jpg"
    extract_metadata(path, runner=fake_runner)

    argv, options = calls[0]
    assert argv[0] == "exiftool"
    assert {"-json", "-G0:1", "-a", "-u", "-struct"}.issubset(argv)
    assert argv[-2:] == ("--", path)
    assert options == {"timeout_seconds": 120}


def test_parse_timestamp_keeps_naive_values_naive_and_applies_explicit_offset():
    naive = parse_timestamp_value("2026:08:12 10:11:12")
    aware = parse_timestamp_value(
        "2026:08:12 10:11:12",
        offset="-04:00",
        subsecond="1234567",
    )

    assert naive.value == datetime(2026, 8, 12, 10, 11, 12)
    assert naive.timezone_evidence == "naive"
    assert aware.value == datetime(
        2026,
        8,
        12,
        10,
        11,
        12,
        123456,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    assert aware.value.utcoffset().total_seconds() == -4 * 60 * 60
    assert aware.precision == "subsecond"


def test_timestamp_claims_pair_offsets_and_gps_utc_and_mark_conflicts():
    claims = timestamp_claims_from_tags(
        {
            "EXIF:ExifIFD:DateTimeOriginal": "2026:08:12 10:00:00",
            "EXIF:ExifIFD:OffsetTimeOriginal": "-04:00",
            "EXIF:GPS:GPSDateStamp": "2026:08:12",
            "EXIF:GPS:GPSTimeStamp": "15:00:00",
            "File:System:FileModifyDate": "2026:08:12 12:00:00-04:00",
        }
    )

    original = next(claim for claim in claims if claim.tag == "DateTimeOriginal")
    gps = next(claim for claim in claims if claim.tag.startswith("GPSDateStamp+"))
    filesystem = next(claim for claim in claims if claim.tag == "FileModifyDate")
    assert original.timezone_evidence == "explicit_offset"
    assert gps.timezone_evidence == "gps_utc"
    assert original.conflicts
    assert gps.conflicts
    assert filesystem.operational is True
    assert filesystem.confidence == 0.2


def test_timestamp_claims_from_ffprobe_preserve_container_and_stream_namespaces():
    metadata = parse_ffprobe_json(FFPROBE_JSON)

    claims = timestamp_claims_from_ffprobe(metadata)

    assert len(claims) == 1
    assert claims[0].namespace == "FFprobe:Stream0"
    assert claims[0].tag == "creation_time"
    assert claims[0].parsed_value == datetime(2026, 8, 12, 12, tzinfo=UTC)
    assert claims[0].timezone_evidence == "explicit_offset"


def test_negative_zero_offset_is_retained_as_unknown_not_treated_as_utc():
    parsed = parse_timestamp_value("2026-08-12 12:00:00-00:00")

    assert parsed.value == datetime(2026, 8, 12, 12)
    assert parsed.timezone_evidence == "unknown_offset"


TESSERACT_TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t10\t10\t40\t20\t90.5\tHello\n"
    "5\t1\t1\t1\t1\t2\t55\t10\t45\t20\t80.5\tworld\n"
    "5\t1\t1\t1\t2\t1\t10\t40\t40\t20\t70\tAgain\n"
)


def test_parse_tesseract_tsv_builds_text_geometry_and_confidence():
    parsed = parse_tesseract_tsv(TESSERACT_TSV)

    assert parsed.text == "Hello world\nAgain"
    assert parsed.mean_confidence == pytest.approx(80.333333)
    assert parsed.page_count == 1
    assert parsed.words[0]["left"] == 10
    assert parsed.lines[0]["box"] == {
        "left": 10,
        "top": 10,
        "width": 90,
        "height": 20,
    }
    assert parsed.blocks == [
        {
            "page_num": 1,
            "block_num": 1,
            "text": "Hello world\nAgain",
            "line_count": 2,
            "box": {"left": 10, "top": 10, "width": 90, "height": 50},
        }
    ]


def test_parse_tesseract_osd_normalizes_known_fields():
    parsed = parse_tesseract_osd(
        """Page number: 0
Orientation in degrees: 90
Rotate: 270
Orientation confidence: 12.34
Script: Latin
Script confidence: 9.87
"""
    )

    assert parsed == {
        "page_number": 0,
        "orientation_degrees": 90,
        "rotate_degrees": 270,
        "orientation_confidence": 12.34,
        "script": "Latin",
        "script_confidence": 9.87,
    }


def test_extract_ocr_treats_osd_failure_as_a_partial_result(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        if argv[-1] == "tsv":
            return CommandResult(TESSERACT_TSV.encode(), b"", 0)
        raise ToolFailedError(returncode=1)

    result = extract_ocr(tmp_path / "scan.png", runner=fake_runner)

    assert result.text == "Hello world\nAgain"
    assert result.orientation is None
    assert result.orientation_error_code == "tool_failed"
    assert len(calls) == 2
