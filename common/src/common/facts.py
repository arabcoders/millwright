from __future__ import annotations

from typing import TYPE_CHECKING

from common.enums import BitrateSource, HdrFormat

if TYPE_CHECKING:
    from collections.abc import Iterable

    from common.ctx import FacetValue
    from common.media.probe import ProbeData, ProbeStream

# Conditions match codec families. Unlisted codecs remain their own family.
_CODEC_FAMILIES = {"h264": "avc", "h265": "hevc", "av01": "av1", "mpeg2video": "mpeg2"}
_TRANSFER_HDR10 = "smpte2084"
_TRANSFER_HLG = "arib-std-b67"

FACT_TYPES = {
    "media.file.container": "str",
    "media.file.duration_s": "float",
    "media.file.size_bytes": "int",
    "media.file.estimated_bitrate_kbps": "int",
    "media.video.count": "int",
    "media.video.codec": "str",
    "media.video.codec_family": "str",
    "media.video.width": "int",
    "media.video.height": "int",
    "media.video.pixels": "int",
    "media.video.fps": "float",
    "media.video.profile": "str",
    "media.video.pix_fmt": "str",
    "media.video.bit_depth": "int",
    "media.video.hdr_format": "str",
    "media.video.bitrate_kbps": "int",
    "media.video.bitrate_source": "str",
    "media.audio.count": "int",
    "media.audio.codecs": "list[str]",
    "media.audio.languages": "list[str]",
    "media.audio.max_channels": "int",
    "media.audio.descriptive_stream_indices": "list[int]",
    "media.subtitle.count": "int",
    "media.subtitle.codecs": "list[str]",
    "media.subtitle.languages": "list[str]",
    "media.subtitle.forced_count": "int",
    "media.subtitle.forced_present": "bool",
    "media.attachment.count": "int",
    "media.data.count": "int",
}


def builtin_facts(probe: ProbeData) -> dict[str, FacetValue]:
    facts: dict[str, FacetValue] = {
        "media.file.container": probe.container,
        "media.file.size_bytes": probe.size_bytes,
        "media.video.count": len(probe.real_video),
        "media.audio.count": len(probe.audio),
        "media.audio.codecs": _unique(stream.codec for stream in probe.audio),
        "media.audio.languages": _languages(probe.audio),
        "media.audio.descriptive_stream_indices": [stream.index for stream in probe.audio if stream.is_descriptive],
        "media.subtitle.count": len(probe.subtitle),
        "media.subtitle.codecs": _unique(stream.codec for stream in probe.subtitle),
        "media.subtitle.languages": _languages(probe.subtitle),
        "media.subtitle.forced_count": sum(stream.disposition_forced for stream in probe.subtitle),
        "media.subtitle.forced_present": any(stream.disposition_forced for stream in probe.subtitle),
        "media.attachment.count": sum(stream.codec_type == "attachment" for stream in probe.streams),
        "media.data.count": sum(stream.codec_type == "data" for stream in probe.streams),
    }
    if probe.duration_s > 0:
        facts["media.file.duration_s"] = probe.duration_s
    estimated = round(probe.size_bytes * 8 / probe.duration_s / 1000) if probe.duration_s > 0 else None
    if estimated is not None:
        facts["media.file.estimated_bitrate_kbps"] = estimated
    if video := probe.real_video:
        facts.update(_video_facts(video[0], estimated))
    channels = [stream.channels for stream in probe.audio if stream.channels is not None]
    if channels:
        facts["media.audio.max_channels"] = max(channels)
    return facts


def _video_facts(video: ProbeStream, estimated: int | None) -> dict[str, FacetValue]:
    facts: dict[str, FacetValue] = {}
    if video.codec:
        facts["media.video.codec"] = video.codec
        facts["media.video.codec_family"] = _CODEC_FAMILIES.get(video.codec, video.codec)
    for key, value in (
        ("width", video.width),
        ("height", video.height),
        ("fps", video.fps),
        ("profile", video.profile),
        ("pix_fmt", video.pix_fmt),
    ):
        if value is not None:
            facts[f"media.video.{key}"] = value
    if video.width is not None and video.height is not None:
        facts["media.video.pixels"] = video.width * video.height
    if video.bit_depth is not None:
        facts["media.video.bit_depth"] = video.bit_depth
    facts["media.video.hdr_format"] = _hdr_format(video)
    if video.bit_rate:
        facts["media.video.bitrate_kbps"] = round(video.bit_rate / 1000)
        facts["media.video.bitrate_source"] = BitrateSource.STREAM
    elif estimated is not None:
        facts["media.video.bitrate_kbps"] = estimated
        facts["media.video.bitrate_source"] = BitrateSource.ESTIMATED
    return facts


def _hdr_format(video: ProbeStream) -> HdrFormat:
    if video.dolby_vision:
        return HdrFormat.DOLBY_VISION
    if video.color_transfer == _TRANSFER_HDR10:
        return HdrFormat.HDR10
    if video.color_transfer == _TRANSFER_HLG:
        return HdrFormat.HLG
    return HdrFormat.SDR


def _languages(streams: list[ProbeStream]) -> list[str]:
    langs: list[str] = []
    for stream in streams:
        if stream.language and stream.language not in langs:
            langs.append(stream.language)
    return langs


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
