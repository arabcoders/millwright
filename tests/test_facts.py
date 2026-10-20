from __future__ import annotations

from pathlib import Path
from typing import Any

from common.facts import builtin_facts
from common.media.probe import ProbeData, probe_from_raw

SIZE_4GIB = 4 * 2**30
# 4 GiB over 3000 s -> 11_453_246 bps -> 11_453 kbps.
ESTIMATED_KBPS = 11_453


def video_stream(**overrides: Any) -> dict:
    stream: dict = {
        "index": 0,
        "codec_name": "hevc",
        "codec_type": "video",
        "profile": "Main 10",
        "pix_fmt": "yuv420p10le",
        "color_transfer": "smpte2084",
        "bit_rate": "5000000",
        "disposition": {},
        "tags": {},
    }
    stream.update(overrides)
    return stream


def audio_stream(index: int = 1, language: str | None = "eng", **overrides: Any) -> dict:
    stream: dict = {
        "index": index,
        "codec_name": "ac3",
        "codec_type": "audio",
        "channels": 6,
        "disposition": {},
        "tags": {"language": language} if language else {},
    }
    stream.update(overrides)
    return stream


def subtitle_stream(index: int = 5, language: str | None = "eng", **overrides: Any) -> dict:
    stream: dict = {
        "index": index,
        "codec_name": "subrip",
        "codec_type": "subtitle",
        "disposition": {},
        "tags": {"language": language} if language else {},
    }
    stream.update(overrides)
    return stream


def build_probe(streams: list[dict], duration: str | None = "3000.0") -> ProbeData:
    fmt = {"duration": duration} if duration is not None else {}
    return probe_from_raw(Path("/lib/show.mkv"), SIZE_4GIB, {"streams": streams, "format": fmt})


def facts(streams: list[dict], duration: str | None = "3000.0") -> dict:
    return builtin_facts(build_probe(streams, duration))


def test_stream_bitrate_source() -> None:
    result = facts([video_stream()])
    assert result["media.video.bitrate_kbps"] == 5000
    assert result["media.video.bitrate_source"] == "stream"
    assert result["media.file.estimated_bitrate_kbps"] == ESTIMATED_KBPS


def test_estimated_bitrate_source() -> None:
    result = facts([video_stream(bit_rate=None)])
    assert result["media.video.bitrate_kbps"] == ESTIMATED_KBPS
    assert result["media.video.bitrate_source"] == "estimated"
    assert result["media.video.bitrate_kbps"] == result["media.file.estimated_bitrate_kbps"]


def test_bitrate_no_duration() -> None:
    result = facts([video_stream()], duration=None)
    assert result["media.video.bitrate_kbps"] == 5000
    assert result["media.video.bitrate_source"] == "stream"
    assert "media.file.estimated_bitrate_kbps" not in result


def test_bitrate_omitted_duration() -> None:
    result = facts([video_stream(bit_rate=None)], duration=None)
    assert "media.file.estimated_bitrate_kbps" not in result
    assert "media.video.bitrate_kbps" not in result
    assert "media.video.bitrate_source" not in result
    assert result["media.file.size_bytes"] == SIZE_4GIB


def test_codec_family_mapping() -> None:
    for codec, family in [
        ("h264", "avc"),
        ("h265", "hevc"),
        ("av01", "av1"),
        ("mpeg2video", "mpeg2"),
        ("vp9", "vp9"),
    ]:
        assert facts([video_stream(codec_name=codec)])["media.video.codec_family"] == family
        assert facts([video_stream(codec_name=codec)])["media.video.codec"] == codec


def test_omits_bit_depth() -> None:
    raw = video_stream()
    del raw["pix_fmt"], raw["profile"]
    assert "media.video.bit_depth" not in facts([raw])


def test_hdr_format_detection() -> None:
    assert facts([video_stream()])["media.video.hdr_format"] == "hdr10"
    assert facts([video_stream(color_transfer="arib-std-b67")])["media.video.hdr_format"] == "hlg"
    assert facts([video_stream(color_transfer="bt709")])["media.video.hdr_format"] == "sdr"
    assert facts([video_stream(color_transfer=None)])["media.video.hdr_format"] == "sdr"


def test_dolby_vision_detection() -> None:
    by_tag = facts([video_stream(codec_tag_string="dvh1")])
    assert by_tag["media.video.hdr_format"] == "dolby_vision"
    by_side_data = facts(
        [video_stream(side_data_list=[{"side_data_type": "DOVI configuration record", "dv_profile": 5}])]
    )
    assert by_side_data["media.video.hdr_format"] == "dolby_vision"


def test_audio_language_lists() -> None:
    streams = [
        video_stream(),
        audio_stream(index=1, language="jpn"),
        audio_stream(index=2, language="eng"),
        audio_stream(index=3, language="jpn"),
        audio_stream(index=4, language=None),
    ]
    assert facts(streams)["media.audio.languages"] == ["jpn", "eng"]
    assert facts([video_stream()])["media.audio.languages"] == []


def test_descriptive_stream_indices() -> None:
    streams = [
        video_stream(),
        audio_stream(index=1, language="eng"),
        audio_stream(index=2, language="eng", disposition={"commentary": 1}),
        audio_stream(index=3, language="eng", tags={"language": "eng", "title": "English SDH"}),
        audio_stream(index=4, language="eng", tags={"language": "eng", "title": "Surround 5.1"}),
    ]
    assert facts(streams)["media.audio.descriptive_stream_indices"] == [2, 3]
    assert facts([video_stream(), audio_stream()])["media.audio.descriptive_stream_indices"] == []


def test_subtitle_facts() -> None:
    streams = [
        video_stream(),
        subtitle_stream(index=5, language="eng"),
        subtitle_stream(index=6, language="eng"),
        subtitle_stream(index=7, language="jpn"),
        subtitle_stream(index=8, language=None, disposition={"forced": 1}),
    ]
    result = facts(streams)
    assert result["media.subtitle.languages"] == ["eng", "jpn"]
    assert result["media.subtitle.forced_present"] is True
    plain = facts([video_stream(), subtitle_stream()])
    assert plain["media.subtitle.forced_present"] is False
    assert facts([video_stream()])["media.subtitle.forced_present"] is False


def test_cover_art_excluded() -> None:
    cover = {"index": 0, "codec_name": "mjpeg", "codec_type": "video", "disposition": {"attached_pic": 1}, "tags": {}}
    result = facts([cover, audio_stream(index=1)])
    assert result["media.video.count"] == 0
    assert not any(key in result for key in ("media.video.codec", "media.video.width", "media.video.bitrate_kbps"))


def test_stream_metadata_parsed() -> None:
    video = build_probe([video_stream()]).real_video[0]
    assert video.color_transfer == "smpte2084"
    assert video.bit_depth == 10
    assert video.ten_bit is True
    plain = build_probe([video_stream(color_transfer=None, pix_fmt="yuv420p")]).real_video[0]
    assert plain.bit_depth == 8
    assert plain.ten_bit is False
    unknown = build_probe([{"index": 0, "codec_name": "h264", "codec_type": "video"}]).real_video[0]
    assert unknown.bit_depth is None
    assert unknown.ten_bit is None
    dv = build_probe([video_stream(codec_tag_string="dvh1")]).real_video[0]
    assert dv.dolby_vision is True
