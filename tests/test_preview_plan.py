from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.plan.renderer import PlaybackPlan, render_playback
from server.playback.segments import MediaSummary, plan_playback


def plan(**overrides: object) -> PlaybackPlan:
    fields: dict = {
        "start_s": 6.0,
        "duration_s": 600.0,
        "segment_s": 6.0,
        "read_ahead_s": 18.0,
        "video_encoder": "libx264",
        "video_stream_index": 2,
        "audio_stream_index": 3,
    }
    fields.update(overrides)
    return PlaybackPlan(**fields)


def test_render_segment() -> None:
    argv = render_playback(plan(), "src.mkv")
    assert argv[argv.index("-ss") + 1] == "6.000000"
    assert argv[argv.index("-t") + 1] == "600.000000"
    assert argv[argv.index("-output_ts_offset") + 1] == "6.000000"
    assert argv[argv.index("-map") + 1] == "0:2"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-hls_time") + 1] == "6.000000"
    assert argv[argv.index("-readrate") + 1] == "1"
    assert argv[argv.index("-readrate_initial_burst") + 1] == "18.000000"
    assert argv[argv.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*6.000000)"
    assert argv[argv.index("-flags") + 1] == "+cgop"
    assert argv[-3:] == ["-hls_segment_filename", "pipe:1", "pipe:2"]


def test_render_audio_only() -> None:
    argv = render_playback(plan(video_stream_index=None), "src.m4a")
    assert argv.count("-map") == 1
    assert argv[argv.index("-map") + 1] == "0:3"
    assert argv[argv.index("-c:a") + 1] == "aac"


def test_render_hardware() -> None:
    argv = render_playback(plan(video_encoder="h264_vaapi"), "src")
    assert argv[argv.index("-init_hw_device") + 1] == "vaapi=va"
    assert argv[argv.index("-vf") + 1] == "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12,hwupload"
    argv = render_playback(plan(video_encoder="h264_nvenc"), "src")
    assert argv[argv.index("-c:v") + 1] == "h264_nvenc"


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc", "h264_vaapi"])
def test_render_bitmap(encoder: str) -> None:
    argv = render_playback(plan(video_encoder=encoder, subtitle_stream_index=5), "src")
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[0:2][0:5]overlay=eof_action=pass,scale=trunc(iw/2)*2:trunc(ih/2)*2")
    assert [argv[index + 1] for index, arg in enumerate(argv) if arg == "-map"] == ["[video]", "0:3"]
    if encoder.endswith("_vaapi"):
        assert graph.endswith(",format=nv12,hwupload[video]")
        assert argv[argv.index("-init_hw_device") + 1] == "vaapi=va"
    else:
        assert graph.endswith("[video]") and argv[argv.index("-pix_fmt") + 1] == "yuv420p"


def test_final_segment() -> None:
    summary = MediaSummary.from_probe(
        {
            "streams": [
                {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}},
                {"index": 2, "codec_type": "video"},
                {"index": 3, "codec_type": "audio"},
            ],
            "format": {"duration": "13", "format_name": "matroska"},
        }
    )
    result = plan_playback(index=2, duration_s=13, segment_s=6, encoder="libx264", summary=summary)
    assert result.start_s == 12 and result.duration_s == 1
    assert result.video_stream_index == 2 and result.audio_stream_index == 3


def test_plan_validation() -> None:
    for invalid in [
        {"video_encoder": "../evil"},
        {"audio_codec": "aac -y"},
        {"duration_s": 0},
        {"start_s": -1},
        {"subtitle_stream_index": -1},
        {"subtitle_stream_index": True},
        {"subtitle_stream_index": 5, "video_stream_index": None},
    ]:
        with pytest.raises(ValidationError):
            plan(**invalid)


def test_audio_tracks() -> None:
    summary = MediaSummary.from_probe(
        {
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 2, "codec_type": "audio", "codec_name": "aac"},
                {
                    "index": 5,
                    "codec_type": "audio",
                    "codec_name": "eac3",
                    "channels": 6,
                    "channel_layout": "5.1",
                    "tags": {"language": "eng", "title": "Surround"},
                    "disposition": {"default": 1},
                },
            ],
        }
    )
    assert summary.audio_stream_index == 5
    first, default = summary.audio_tracks
    assert first.stream_index == 2 and first.lang == "und"
    assert not first.default
    assert default.default and default.name == "Surround" and default.channels == 6
    assert default.lang == "eng" and default.codec == "eac3" and default.channel_layout == "5.1"
    selected = plan_playback(
        index=0, duration_s=13, segment_s=6, encoder="libx264", summary=summary, audio_stream_index=2
    )
    argv = render_playback(selected, "src")
    assert [argv[index + 1] for index, arg in enumerate(argv) if arg == "-map"] == ["0:0", "0:2"]
    assert summary.audio_stream_index == 5


def test_audio_default() -> None:
    summary = MediaSummary.from_probe({"streams": [{"index": 3, "codec_type": "audio"}]})
    assert summary.audio_stream_index == 3 and summary.audio_tracks[0].default
    silent = MediaSummary.from_probe({"streams": [{"index": 0, "codec_type": "video"}]})
    assert silent.audio_stream_index is None and silent.audio_tracks == []


def test_video_diagnostics() -> None:
    summary = MediaSummary.from_probe(
        {
            "streams": [
                {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}, "codec_name": "mjpeg"},
                {
                    "index": 1,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "pix_fmt": "yuv420p10le",
                    "width": 1920,
                    "height": 1080,
                },
            ],
        }
    )
    assert summary.video_codec == "hevc" and summary.video_profile == "Main 10"
    assert summary.video_pix_fmt == "yuv420p10le"
    assert summary.width == 1920 and summary.height == 1080
    audio = MediaSummary.from_probe({"streams": [{"index": 0, "codec_type": "audio"}]})
    assert audio.video_codec is None and audio.width is None and audio.height is None
