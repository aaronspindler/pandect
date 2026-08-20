import pytest

from pandect.processing.extractors.derived_media import (
    SceneChange,
    artwork_output_for_codec,
    attached_picture_pixel_preflight,
    detect_scene_change_times,
    detect_scene_changes,
    embedded_artwork_streams_from_probe,
    extract_embedded_artwork,
    extract_faithful_audio_stream,
    extract_normalized_audio_chunk,
    extract_normalized_audio_stream,
    extract_representative_frames,
    extract_subtitle_stream,
    extract_video_frame,
    faithful_audio_output_for_codec,
    parse_scene_changes,
    parse_showinfo_timestamps,
    representative_frame_selection,
    representative_frame_times,
    subtitle_output_for_codec,
    subtitle_streams_from_probe,
)
from pandect.processing.extractors.errors import ToolFailedError
from pandect.processing.extractors.hashing import fingerprint_file
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


def test_representative_times_include_bounds_scenes_and_even_coverage():
    result = representative_frame_times(
        100,
        maximum_frames=6,
        scene_change_times=(75, 25, 25, -1, 101),
    )

    assert result == (0.0, 16.666667, 25.0, 33.333333, 75.0, 99.999)


def test_representative_selection_uses_strongest_scenes_then_returns_chronological():
    result = representative_frame_selection(
        100,
        maximum_frames=5,
        scene_changes=(
            SceneChange(timestamp_seconds=10, score=0.31),
            SceneChange(timestamp_seconds=80, score=0.91),
            SceneChange(timestamp_seconds=70, score=0.91),
            SceneChange(timestamp_seconds=60, score=0.72),
        ),
    )

    assert [value.timestamp_seconds for value in result] == [
        0.0,
        60.0,
        70.0,
        80.0,
        99.999,
    ]
    assert [value.reason for value in result] == [
        "first_frame",
        "scene_change",
        "scene_change",
        "scene_change",
        "last_frame",
    ]
    assert [value.scene_score for value in result] == [
        None,
        0.72,
        0.91,
        0.91,
        None,
    ]


def test_full_keyframe_budget_keeps_strongest_changes_instead_of_earliest():
    result = representative_frame_selection(
        200,
        maximum_frames=120,
        scene_changes=tuple(
            SceneChange(timestamp_seconds=index, score=index / 200)
            for index in range(1, 131)
        ),
    )

    timestamps = [value.timestamp_seconds for value in result]
    assert len(result) == 120
    assert timestamps == sorted(timestamps)
    assert 12.0 not in timestamps
    assert 13.0 in timestamps
    assert 130.0 in timestamps


def test_representative_times_are_bounded_and_handle_empty_video():
    assert representative_frame_times(0, maximum_frames=12) == (0.0,)
    with pytest.raises(ValueError):
        representative_frame_times(10, maximum_frames=121)
    with pytest.raises(ValueError):
        representative_frame_times(float("nan"), maximum_frames=1)


def test_scene_change_parser_deduplicates_nonnegative_timestamps():
    assert parse_showinfo_timestamps(
        b"n:1 pts:100 pts_time:4.25\nn:2 pts:200 pts_time:8\n"
        b"n:3 pts:200 pts_time:8\nn:4 pts:-1 pts_time:-0.1"
    ) == (4.25, 8.0)


def test_scene_change_parser_pairs_scores_and_ranks_with_timestamp_tie_break():
    result = parse_scene_changes(
        b"[metadata] frame:0 pts:10 pts_time:8\n"
        b"[metadata] lavfi.scene_score=0.700000\n"
        b"[metadata] frame:1 pts:20 pts_time:4.25\n"
        b"[metadata] lavfi.scene_score=0.900000\n"
        b"[metadata] frame:2 pts:30 pts_time:3\n"
        b"[metadata] lavfi.scene_score=0.900000\n"
        b"[metadata] frame:3 pts:40 pts_time:4.25\n"
        b"[metadata] lavfi.scene_score=0.950000\n"
        b"[metadata] frame:4 pts:-1 pts_time:-0.1\n"
        b"[metadata] lavfi.scene_score=1.000000\n"
    )

    assert result == (
        SceneChange(timestamp_seconds=4.25, score=0.95),
        SceneChange(timestamp_seconds=3.0, score=0.9),
        SceneChange(timestamp_seconds=8.0, score=0.7),
    )


def test_scene_change_detection_is_bounded_and_stream_addressed(tmp_path):
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        return CommandResult(
            b"",
            b"frame:0 pts:1 pts_time:2.5\n"
            b"lavfi.scene_score=0.5\n"
            b"frame:1 pts:2 pts_time:9.999\n"
            b"lavfi.scene_score=0.9\n"
            b"frame:2 pts:3 pts_time:10\n"
            b"lavfi.scene_score=0.8",
            0,
        )

    result = detect_scene_change_times(
        tmp_path / "source.mp4",
        duration_seconds=10,
        threshold=0.4,
        video_stream_index=2,
        runner=fake_runner,
    )

    assert result == (2.5, 9.999)
    argv, options = calls[0]
    assert _has_arguments(argv, "-map", "0:2")
    assert "select=gt(scene\\,0.4),metadata=mode=print:key=lavfi.scene_score" in argv
    _assert_single_threaded_ffmpeg(argv)
    assert options == {"timeout_seconds": 320}


def test_scored_scene_detection_returns_strongest_candidate_first(tmp_path):
    def fake_runner(_argv, **_options):
        return CommandResult(
            b"",
            b"frame:0 pts:1 pts_time:2.5\n"
            b"lavfi.scene_score=0.5\n"
            b"frame:1 pts:2 pts_time:9.999\n"
            b"lavfi.scene_score=0.9",
            0,
        )

    assert detect_scene_changes(
        tmp_path / "source.mp4",
        duration_seconds=10,
        runner=fake_runner,
    ) == (
        SceneChange(timestamp_seconds=9.999, score=0.9),
        SceneChange(timestamp_seconds=2.5, score=0.5),
    )


def test_extract_video_frame_strips_metadata_and_returns_hash_offsets(tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "frame.jpg"
    source.write_bytes(b"canonical-video")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"derived-jpeg")
        return CommandResult(b"", b"", 0)

    result = extract_video_frame(
        source,
        destination,
        timestamp_seconds=4.25,
        frame_index=3,
        duration_seconds=10,
        video_stream_index=1,
        runner=fake_runner,
    )

    argv, options = calls[0]
    assert _has_arguments(argv, "-ss", "4.250000", "-i", source)
    assert _has_arguments(argv, "-map", "0:1")
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv
    assert argv[-2:] == ("-n", str(destination.resolve()))
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 320}
    assert result.mime_type == "image/jpeg"
    assert result.stream_index == 1
    assert result.frame_index == 3
    assert result.source_start_seconds == 4.25
    assert result.source_end_seconds == 4.25
    assert result.sha256 == fingerprint_file(destination).sha256
    assert result.metadata == {
        "metadata_stripped": True,
        "jpeg_quality": 2,
        "source_stream_index": 1,
        "selection_index": 3,
        "selection_requested_timestamp_seconds": 4.25,
        "frame_index_semantics": (
            "representative_selection_ordinal_not_decoded_source_frame_index"
        ),
        "source_offset_semantics": "requested_seek_time_not_decoded_source_pts",
        "decoded_source_pts": "not_available",
        "decoded_source_timestamp_seconds": "not_available",
        "decoded_source_frame_index": "not_available",
        "decoded_coordinate_limitation": (
            "bounded_single_frame_extraction_does_not_expose_absolute_"
            "decoded_source_coordinates"
        ),
    }
    assert source.read_bytes() == b"canonical-video"


def test_extract_video_frame_never_overwrites_source_or_destination(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"canonical")

    with pytest.raises(ValueError):
        extract_video_frame(
            source,
            source,
            timestamp_seconds=0,
            frame_index=0,
            duration_seconds=1,
        )


def test_failed_derivation_removes_only_its_partial_destination(tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "partial.jpg"
    source.write_bytes(b"canonical")

    def failing_runner(_argv, **_options):
        destination.write_bytes(b"partial")
        raise ToolFailedError(returncode=1)

    with pytest.raises(ToolFailedError):
        extract_video_frame(
            source,
            destination,
            timestamp_seconds=0,
            frame_index=0,
            duration_seconds=1,
            runner=failing_runner,
        )

    assert not destination.exists()
    assert source.read_bytes() == b"canonical"

    destination = tmp_path / "existing.jpg"
    destination.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        extract_video_frame(
            source,
            destination,
            timestamp_seconds=0,
            frame_index=0,
            duration_seconds=1,
        )


def test_extract_representative_frames_requires_exact_unique_destinations(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"canonical")
    destination = tmp_path / "only.jpg"

    with pytest.raises(ValueError, match="selected frame count"):
        extract_representative_frames(
            source,
            (destination,),
            duration_seconds=10,
            maximum_frames=2,
        )

    with pytest.raises(ValueError, match="unique"):
        extract_representative_frames(
            source,
            (destination, destination),
            duration_seconds=10,
            maximum_frames=2,
        )


def test_extract_representative_frames_returns_ordered_artifacts(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"canonical")
    destinations = (tmp_path / "first.jpg", tmp_path / "last.jpg")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        output = argv[-1]
        with open(output, "wb") as stream:
            stream.write(f"jpeg-{len(calls)}".encode())
        return CommandResult(b"", b"", 0)

    result = extract_representative_frames(
        source,
        destinations,
        duration_seconds=10,
        maximum_frames=2,
        runner=fake_runner,
    )

    assert [value.frame_index for value in result] == [0, 1]
    assert [value.source_start_seconds for value in result] == [0.0, 9.999]
    assert len({value.sha256 for value in result}) == 2


def test_normalized_audio_stream_is_lossless_metadata_free_and_traceable(tmp_path):
    source = tmp_path / "source.mkv"
    destination = tmp_path / "stream.flac"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"flac")
        return CommandResult(b"", b"", 0)

    result = extract_normalized_audio_stream(
        source,
        destination,
        stream_index=4,
        duration_seconds=8,
        runner=fake_runner,
    )

    argv, options = calls[0]
    assert _has_arguments(argv, "-map", "0:4")
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv
    assert _has_arguments(argv, "-c:a", "flac")
    assert _has_arguments(argv, "-f", "flac")
    assert argv[-2:] == ("-n", str(destination.resolve()))
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 316}
    assert result.mime_type == "audio/flac"
    assert result.stream_index == 4
    assert result.source_start_seconds == 0
    assert result.source_end_seconds == 8
    assert result.metadata == {
        "metadata_stripped": True,
        "sample_rate": 16000,
        "channels": 1,
        "codec": "flac",
        "resampling": "16000_hz",
        "downmix": "mono",
    }


def test_faithful_audio_stream_copies_safe_codec_packets_with_source_provenance(
    tmp_path,
):
    source = tmp_path / "source.mkv"
    destination = tmp_path / "stream.aac"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"copied-aac-packets")
        return CommandResult(b"", b"", 0)

    result = extract_faithful_audio_stream(
        source,
        destination,
        stream_index=4,
        duration_seconds=8,
        codec_name="aac",
        sample_rate=48_000,
        channels=6,
        channel_layout="5.1",
        sample_format="fltp",
        bits_per_raw_sample=24,
        profile="LC",
        codec_tag="mp4a",
        runner=fake_runner,
    )

    argv, options = calls[0]
    assert _has_arguments(argv, "-map", "0:4")
    assert _has_arguments(argv, "-c:a", "copy")
    assert _has_arguments(argv, "-f", "adts")
    assert "-ac" not in argv
    assert "-ar" not in argv
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 316}
    assert result.mime_type == "audio/aac"
    assert result.stream_index == 4
    assert result.metadata["derivative_purpose"] == "source_faithful_audio_stream"
    assert result.metadata["source_codec"] == "aac"
    assert result.metadata["source_sample_rate"] == 48_000
    assert result.metadata["source_channels"] == 6
    assert result.metadata["source_channel_layout"] == "5.1"
    assert result.metadata["extraction_mode"] == "stream_copy"
    assert result.metadata["packet_copy"] is True
    assert result.metadata["reencoding_loss"] == "none"


def test_faithful_audio_stream_uses_lossless_native_shape_fallback(tmp_path):
    source = tmp_path / "source.mkv"
    destination = tmp_path / "stream.wv"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"lossless-wavpack")
        return CommandResult(b"", b"", 0)

    output = faithful_audio_output_for_codec("unsupported-codec")
    result = extract_faithful_audio_stream(
        source,
        destination,
        stream_index=2,
        duration_seconds=5,
        codec_name="unsupported-codec",
        sample_rate=96_000,
        channels=2,
        runner=fake_runner,
    )

    assert output.extension == "wv"
    assert output.stream_copy is False
    argv, options = calls[0]
    assert _has_arguments(argv, "-c:a", "wavpack")
    assert _has_arguments(argv, "-ac", "2")
    assert _has_arguments(argv, "-ar", "96000")
    assert _has_arguments(argv, "-f", "wv")
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 310}
    assert result.mime_type == "audio/wavpack"
    assert result.metadata["extraction_mode"] == "lossless_transcode"
    assert result.metadata["lossless_transcode"] is True
    assert result.metadata["sample_rate_preservation"] == "explicit_source_value"
    assert result.metadata["channel_preservation"] == "explicit_source_count"


def test_faithful_audio_output_copies_high_precision_pcm_without_narrowing():
    output = faithful_audio_output_for_codec("pcm_f64le")

    assert output.extension == "nut"
    assert output.container == "nut"
    assert output.output_codec == "pcm_f64le"
    assert output.stream_copy is True


def test_normalized_audio_chunk_returns_exact_offsets_overlap_and_hash(tmp_path):
    source = tmp_path / "source.mkv"
    destination = tmp_path / "chunk.flac"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"chunk")
        return CommandResult(b"", b"", 0)

    result = extract_normalized_audio_chunk(
        source,
        destination,
        stream_index=3,
        start_seconds=599,
        end_seconds=1200,
        overlap_before_seconds=1,
        runner=fake_runner,
    )

    assert result.stream_index == 3
    assert result.source_start_seconds == 599
    assert result.source_end_seconds == 1200
    assert result.sha256 == fingerprint_file(destination).sha256
    assert result.metadata == {
        "metadata_stripped": True,
        "sample_rate": 16000,
        "channels": 1,
        "codec": "flac",
        "resampling": "16000_hz",
        "downmix": "mono",
        "overlap_before_seconds": 1,
    }
    argv, options = calls[0]
    assert _has_arguments(argv, "-ss", "599.000000", "-t", "601.000000")
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 1502}


def test_embedded_artwork_selector_keeps_each_attached_picture_and_provenance():
    selected = embedded_artwork_streams_from_probe(
        (
            {"index": 0, "codec_type": "audio", "codec_name": "aac"},
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
                "tags": {"title": "Front", "comment": "Cover (front)"},
            },
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "av1",
                "disposition": {"attached_pic": 1},
            },
            {
                "index": 3,
                "codec_type": "video",
                "codec_name": "h264",
                "disposition": {"attached_pic": 0},
            },
        )
    )

    assert selected == (
        {
            "stream_index": 1,
            "source_codec": "mjpeg",
            "extension": "jpg",
            "mime_type": "image/jpeg",
            "stream_copy": True,
            "width": None,
            "height": None,
            "language": None,
            "title": "Front",
            "comment": "Cover (front)",
        },
        {
            "stream_index": 2,
            "source_codec": "av1",
            "extension": "png",
            "mime_type": "image/png",
            "stream_copy": False,
            "width": None,
            "height": None,
            "language": None,
            "title": None,
            "comment": None,
        },
    )
    assert artwork_output_for_codec("PNG") == ("png", "image/png", True)


def test_attached_picture_pixel_preflight_is_fail_closed_when_dimensions_exist():
    assert attached_picture_pixel_preflight(
        {"width": 20, "height": 30},
        maximum_pixels=599,
    ) == {
        "status": "exceeds_limit",
        "declared_width": 20,
        "declared_height": 30,
        "declared_pixel_count": 600,
        "maximum_pixels": 599,
        "limitation": None,
    }
    unknown = attached_picture_pixel_preflight(
        {"width": None, "height": 30},
        maximum_pixels=599,
    )
    assert unknown["status"] == "not_available"
    assert unknown["declared_width"] is None
    assert unknown["declared_height"] == 30
    assert "post_extraction_pixel_preflight_required" in unknown["limitation"]


@pytest.mark.parametrize(
    ("codec_name", "expected_codec", "expected_mime", "metadata_stripped"),
    [
        ("mjpeg", "copy", "image/jpeg", False),
        ("av1", "png", "image/png", True),
    ],
)
def test_extract_embedded_artwork_is_traceable_and_never_overwrites_source(
    tmp_path,
    codec_name,
    expected_codec,
    expected_mime,
    metadata_stripped,
):
    source = tmp_path / "source.m4a"
    destination = tmp_path / ("cover.jpg" if codec_name == "mjpeg" else "cover.png")
    source.write_bytes(b"canonical-audio")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"derived-artwork")
        return CommandResult(b"", b"", 0)

    result = extract_embedded_artwork(
        source,
        destination,
        stream_index=4,
        codec_name=codec_name,
        duration_seconds=8,
        runner=fake_runner,
    )

    argv, options = calls[0]
    assert _has_arguments(argv, "-map", "0:4")
    assert _has_arguments(argv, "-frames:v", "1")
    assert ("-c:v", expected_codec) in tuple(zip(argv, argv[1:], strict=False))
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv
    assert _has_arguments(argv, "-f", "image2")
    assert argv[-2:] == ("-n", str(destination.resolve()))
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 316}
    assert result.mime_type == expected_mime
    assert result.stream_index == 4
    assert result.sha256 == fingerprint_file(destination).sha256
    assert result.metadata["metadata_stripped"] is metadata_stripped
    assert source.read_bytes() == b"canonical-audio"


@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        ("subrip", ("srt", "application/x-subrip")),
        ("webvtt", ("vtt", "text/vtt")),
        ("ass", ("ass", "text/x-ssa")),
        ("hdmv_pgs_subtitle", None),
    ],
)
def test_subtitle_output_support_is_explicit(codec, expected):
    assert subtitle_output_for_codec(codec) == expected


def test_subtitle_streams_from_probe_selects_text_streams_and_tags():
    selected = subtitle_streams_from_probe(
        (
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "eng", "title": "English"},
            },
            {
                "index": 3,
                "codec_type": "subtitle",
                "codec_name": "hdmv_pgs_subtitle",
            },
        )
    )

    assert selected == (
        {
            "stream_index": 2,
            "source_codec": "subrip",
            "extension": "srt",
            "mime_type": "application/x-subrip",
            "language": "eng",
            "title": "English",
        },
    )


def test_extract_subtitle_stream_is_metadata_free_and_skips_bitmap(tmp_path):
    source = tmp_path / "source.mkv"
    destination = tmp_path / "captions.srt"
    source.write_bytes(b"canonical")
    calls = []

    def fake_runner(argv, **options):
        calls.append((argv, options))
        destination.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nText\n")
        return CommandResult(b"", b"", 0)

    result = extract_subtitle_stream(
        source,
        destination,
        stream_index=2,
        codec_name="subrip",
        duration_seconds=60,
        language="eng",
        title="English",
        runner=fake_runner,
    )

    assert result is not None
    argv, options = calls[0]
    assert _has_arguments(argv, "-map", "0:2")
    assert "-map_metadata" in argv
    assert "-map_chapters" in argv
    assert _has_arguments(argv, "-c:s", "subrip", "-f", "srt")
    assert argv[-2:] == ("-n", str(destination.resolve()))
    _assert_single_threaded_ffmpeg(argv, encodes_output=True)
    assert options == {"timeout_seconds": 420}
    assert result.mime_type == "application/x-subrip"
    assert result.metadata == {
        "metadata_stripped": True,
        "source_codec": "subrip",
        "language": "eng",
        "title": "English",
    }

    assert (
        extract_subtitle_stream(
            source,
            tmp_path / "captions.sup",
            stream_index=3,
            codec_name="hdmv_pgs_subtitle",
            duration_seconds=60,
            runner=fake_runner,
        )
        is None
    )
    assert len(calls) == 1
