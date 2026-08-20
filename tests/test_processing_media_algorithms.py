import pytest

from pandect.processing.extractors.audio import (
    analyze_acoustics,
    detect_silence,
    extract_audio_chunk,
    ffmpeg_timeout_seconds,
    parse_loudnorm,
    parse_silencedetect,
    parse_volumedetect,
)
from pandect.processing.extractors.audio_splits import (
    AudioChunk,
    TimeRange,
    plan_audio_splits,
    split_audio_chunk_for_payload_limit,
)
from pandect.processing.extractors.errors import (
    ResourceLimitError,
    ToolFailedError,
    UnsupportedPixelFormatError,
)
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.extractors.images import _open_image, analyze_image
from pandect.processing.extractors.subprocesses import CommandResult


def _has_arguments(argv, *expected):
    width = len(expected)
    return any(
        tuple(argv[index : index + width]) == expected for index in range(len(argv))
    )


def _assert_single_threaded_ffmpeg(argv, *, encodes_output=False):
    assert _has_arguments(argv, "-filter_threads", "1")
    assert _has_arguments(argv, "-filter_complex_threads", "1")
    assert argv.count("-threads") == (2 if encodes_output else 1)
    assert all(
        argv[index + 1] == "1"
        for index, value in enumerate(argv)
        if value == "-threads"
    )


def test_audio_plan_honors_chapters_then_prefers_sustained_silence():
    plan = plan_audio_splits(
        1300,
        chapters=(TimeRange(0, 700), TimeRange(700, 1300)),
        silences=(TimeRange(590, 594),),
    )

    assert [
        (chunk.start_seconds, chunk.end_seconds, chunk.end_reason)
        for chunk in plan.chunks
    ] == [
        (0.0, 592.0, "silence"),
        (592.0, 700.0, "chapter"),
        (700.0, 1300.0, "end"),
    ]
    assert all(chunk.overlap_before_seconds == 0 for chunk in plan.chunks)


def test_audio_plan_overlaps_only_unavoidable_hard_duration_cuts():
    plan = plan_audio_splits(1201)

    assert [chunk.end_reason for chunk in plan.chunks] == [
        "max_duration",
        "max_duration",
        "end",
    ]
    assert plan.chunks[0].start_seconds == 0
    assert plan.chunks[0].end_seconds == 600
    assert plan.chunks[1].start_seconds == 599
    assert plan.chunks[1].overlap_before_seconds == 1
    assert plan.chunks[2].start_seconds == 1198
    assert plan.chunks[2].end_seconds == 1201


def test_audio_plan_applies_provider_byte_cap_with_safety_margin():
    plan = plan_audio_splits(
        200,
        provider_max_bytes=1000,
        estimated_bytes_per_second=10,
    )

    assert plan.provider_cap_applied is True
    assert plan.effective_max_chunk_seconds == 95
    assert plan.chunks[0].end_reason == "provider_limit"
    assert max(chunk.duration_seconds for chunk in plan.chunks) <= 95


def test_post_encode_provider_split_preserves_offsets_and_fixed_overlap():
    left, right = split_audio_chunk_for_payload_limit(
        AudioChunk(
            index=3,
            start_seconds=599,
            end_seconds=1200,
            overlap_before_seconds=1,
            end_reason="end",
        )
    )

    assert (left.start_seconds, left.end_seconds) == (599.0, 899.5)
    assert left.overlap_before_seconds == 1
    assert left.end_reason == "provider_payload_limit"
    assert (right.start_seconds, right.end_seconds) == (898.5, 1200.0)
    assert right.overlap_before_seconds == 1
    assert right.end_reason == "end"


def test_post_encode_provider_split_rejects_segment_too_short_for_overlap():
    with pytest.raises(ValueError, match="too short"):
        split_audio_chunk_for_payload_limit(
            AudioChunk(
                index=0,
                start_seconds=0,
                end_seconds=2,
                overlap_before_seconds=0,
                end_reason="end",
            )
        )


def test_audio_plan_accepts_normalized_ffprobe_chapter_mappings():
    plan = plan_audio_splits(
        20,
        chapters=(
            {"start_time_seconds": 0, "end_time_seconds": 5},
            {"start_time_seconds": 5, "end_time_seconds": 20},
        ),
    )

    assert [(chunk.start_seconds, chunk.end_seconds) for chunk in plan.chunks] == [
        (0.0, 5.0),
        (5.0, 20.0),
    ]


@pytest.mark.parametrize(
    "options",
    [
        {"max_chunk_seconds": 0},
        {"hard_cut_overlap_seconds": 600},
        {"provider_max_bytes": 1000},
        {"provider_max_bytes": -1, "estimated_bytes_per_second": 10},
    ],
)
def test_audio_plan_rejects_unsafe_limits(options):
    with pytest.raises(ValueError):
        plan_audio_splits(100, **options)


def test_ffmpeg_timeout_is_duration_relative_and_capped():
    assert ffmpeg_timeout_seconds(None) == 300
    assert ffmpeg_timeout_seconds(60) == 420
    assert ffmpeg_timeout_seconds(10_000) == 7200


def test_parse_silencedetect_closes_final_open_silence_at_known_duration():
    ranges = parse_silencedetect(
        """[silencedetect] silence_start: 2.5
[silencedetect] silence_end: 4.75 | silence_duration: 2.25
[silencedetect] silence_start: 9.0
""",
        duration_seconds=10,
    )

    assert ranges == (
        TimeRange(2.5, 4.75, "silence"),
        TimeRange(9.0, 10.0, "silence"),
    )


def test_detect_silence_selects_stream_and_returns_parsed_intervals(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        return CommandResult(
            b"",
            b"silence_start: 5\nsilence_end: 7 | silence_duration: 2",
            0,
        )

    result = detect_silence(
        tmp_path / "audio.m4a",
        duration_seconds=20,
        stream_index=2,
        runner=fake_runner,
    )

    assert result == (TimeRange(5.0, 7.0, "silence"),)
    argv = calls[0][0]
    assert _has_arguments(argv, "-map", "0:2")
    assert "silencedetect=noise=-35dB:d=1" in argv
    _assert_single_threaded_ffmpeg(argv)
    assert calls[0][1] == {"timeout_seconds": 340}


def test_acoustic_parsers_return_loudness_volume_and_clipping_proxy():
    loudness = parse_loudnorm(
        b"""diagnostic
{
 "input_i": "-19.25",
 "input_tp": "-0.05",
 "input_lra": "6.40",
 "input_thresh": "-29.50",
 "target_offset": "0.25"
}
"""
    )
    volume = parse_volumedetect(
        b"[volumedetect] mean_volume: -21.0 dB\n[volumedetect] max_volume: -0.1 dB\n"
    )

    assert loudness["integrated_loudness_lufs"] == -19.25
    assert loudness["true_peak_dbfs"] == -0.05
    assert loudness["loudness_range_lu"] == 6.4
    assert volume == {
        "mean_volume_dbfs": -21.0,
        "rms_dbfs": -21.0,
        "rms_linear_full_scale": pytest.approx(0.089125093813),
        "max_sample_peak_dbfs": -0.1,
        "possible_clipping": True,
    }


def test_analyze_acoustics_runs_bounded_filters_without_exposing_source(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        if any("loudnorm=" in str(part) for part in argv):
            return CommandResult(
                b"",
                b'{"input_i":"-20","input_tp":"-1","input_lra":"4"}',
                0,
            )
        return CommandResult(
            b"",
            b"mean_volume: -24 dB\nmax_volume: -1 dB",
            0,
        )

    result = analyze_acoustics(
        tmp_path / "audio.wav",
        duration_seconds=10,
        runner=fake_runner,
    )

    assert result.loudness["integrated_loudness_lufs"] == -20
    assert result.volume["possible_clipping"] is False
    assert len(calls) == 2
    assert all(options == {"timeout_seconds": 320} for _, options in calls)
    for argv, _options in calls:
        _assert_single_threaded_ffmpeg(argv)


def test_analyze_acoustics_forwards_lease_progress_to_each_long_filter(tmp_path):
    heartbeats = []

    def heartbeat():
        heartbeats.append(True)

    def fake_runner(argv, **options):
        assert options["progress_callback"] is heartbeat
        options["progress_callback"]()
        if any("loudnorm=" in str(part) for part in argv):
            return CommandResult(
                b"",
                b'{"input_i":"-20","input_tp":"-1","input_lra":"4"}',
                0,
            )
        return CommandResult(b"", b"mean_volume: -24 dB\nmax_volume: -1 dB", 0)

    analyze_acoustics(
        tmp_path / "audio.wav",
        duration_seconds=10,
        runner=fake_runner,
        progress_callback=heartbeat,
    )

    assert len(heartbeats) == 2


def test_extract_audio_chunk_is_exact_non_overwriting_and_fingerprinted(tmp_path):
    source = tmp_path / "source.wav"
    destination = tmp_path / "chunk.flac"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"derived-flac")
        return CommandResult(b"", b"", 0)

    fingerprint = extract_audio_chunk(
        source,
        destination,
        start_seconds=2.25,
        end_seconds=7.75,
        stream_index=1,
        runner=fake_runner,
    )

    argv, options = calls[0]
    assert _has_arguments(argv, "-ss", "2.250000", "-t", "5.500000")
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv
    assert _has_arguments(argv, "-f", "flac")
    assert argv[-2:] == ("-n", destination)
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 311}
    assert fingerprint == fingerprint_file(destination)

    with pytest.raises(FileExistsError):
        extract_audio_chunk(
            source,
            destination,
            start_seconds=0,
            end_seconds=1,
            runner=fake_runner,
        )


def test_image_analysis_generates_stable_technical_features(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "generated.png"
    Image.new("RGB", (8, 4), (17, 34, 51)).save(path)

    first = analyze_image(path).as_dict()
    second = analyze_image(path).as_dict()

    assert first["format"] == "PNG"
    assert first["mime_type"] == "image/png"
    assert first["width"] == 8
    assert first["height"] == 4
    assert first["aspect_ratio"] == "2:1"
    assert first["frame_count"] == 1
    assert first["frames_analyzed"] == 1
    assert first["dhash"] == second["dhash"]
    assert first["phash"] == second["phash"]
    assert first["dominant_colors"][0]["hex"] == "#112233"
    assert first["quality"]["entropy_bits"] == 0
    assert first["quality"]["edge_standard_deviation"] >= 0


class _UnavailableImageDecoder:
    @staticmethod
    def open(_path):
        raise OSError

    @staticmethod
    def registered_extensions():
        return {".png": "PNG", ".jpg": "JPEG"}


def test_image_open_distinguishes_unsupported_heif_from_corrupt_supported_input(
    tmp_path,
):
    heif = tmp_path / "unsupported.heic"
    ftyp_body = b"heic" + b"\0\0\0\0" + b"mif1"
    ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
    heif.write_bytes(ftyp + b"\0\0\0\x08mdat")

    with pytest.raises(UnsupportedPixelFormatError):
        _open_image(_UnavailableImageDecoder, heif)

    corrupt_png = tmp_path / "corrupt.png"
    corrupt_png.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
    with pytest.raises(ToolFailedError):
        _open_image(_UnavailableImageDecoder, corrupt_png)


def test_image_open_treats_failed_registered_avif_decoder_as_corrupt(tmp_path):
    class RegisteredAvifDecoder(_UnavailableImageDecoder):
        @staticmethod
        def registered_extensions():
            return {".avif": "AVIF"}

    avif = tmp_path / "corrupt.avif"
    ftyp_body = b"avif" + b"\0\0\0\0" + b"mif1"
    ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
    avif.write_bytes(ftyp + b"\0\0\0\x08mdat")

    with pytest.raises(ToolFailedError):
        _open_image(RegisteredAvifDecoder, avif)


def test_image_analysis_rejects_pixel_count_before_decode(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "generated.png"
    Image.new("RGB", (2, 2), "white").save(path)

    with pytest.raises(ResourceLimitError):
        analyze_image(path, max_pixels=3)


def test_image_analysis_uses_each_tiff_pages_actual_geometry(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "different-page-sizes.tiff"
    first_page = Image.new("RGB", (2, 2), "white")
    second_page = Image.new("RGB", (3, 1), "black")
    first_page.save(path, save_all=True, append_images=[second_page])

    result = analyze_image(path, max_pixels=20).as_dict()

    assert [(frame["width"], frame["height"]) for frame in result["frames"]] == [
        (2, 2),
        (3, 1),
    ]
    assert result["decoded_pixel_count"] == 7


def test_image_analysis_rejects_oversized_later_tiff_page(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "oversized-later-page.tiff"
    first_page = Image.new("RGB", (2, 2), "white")
    second_page = Image.new("RGB", (4, 3), "black")
    first_page.save(path, save_all=True, append_images=[second_page])

    with pytest.raises(ResourceLimitError):
        analyze_image(path, max_pixels=8, max_total_decoded_pixels=100)


def test_image_analysis_stops_before_page_that_exceeds_aggregate_budget(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "aggregate-limit.tiff"
    first_page = Image.new("RGB", (3, 2), "white")
    second_page = Image.new("RGB", (3, 2), "black")
    first_page.save(path, save_all=True, append_images=[second_page])

    result = analyze_image(path, max_pixels=10).as_dict()

    assert result["frames_analyzed"] == 1
    assert result["decoded_pixel_count"] == 6
    assert result["analysis_truncated"] is True
    assert result["truncation_reason"] == "total_pixel_limit"
    assert result["truncation_frame_index"] == 1


def test_image_analysis_decodes_at_most_configured_page_count(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    path = tmp_path / "many-pages.tiff"
    pages = [Image.new("L", (1, 1), index) for index in range(101)]
    pages[0].save(path, save_all=True, append_images=pages[1:])

    result = analyze_image(
        path,
        max_pixels=200,
        max_frames=100,
        max_total_decoded_pixels=200,
    ).as_dict()

    assert result["frame_count"] == 101
    assert result["frames_analyzed"] == 100
    assert result["decoded_pixel_count"] == 100
    assert result["analysis_truncated"] is True
    assert result["truncation_reason"] == "frame_limit"
    assert result["truncation_frame_index"] == 100
