from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.media.probe import probe_from_raw

if TYPE_CHECKING:
    import os
    from pathlib import Path

    from common.media.probe import ProbeData

# First matching bucket wins. Limits are cumulative, so order matters.
_RESOLUTION_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("480p", 792, 528),
    ("576p", 792, 634),
    ("720p", 1408, 792),
    ("1080p", 2112, 1188),
    ("4KUHD", 4224, 2376),
    ("DCI4K", 4506, 2376),
    ("8KUHD", 8448, 5752),
)


def resolution_bucket(width: int | None, height: int | None) -> str | None:
    if width is None or height is None or width < 100 or height < 100:
        return None
    for bucket, max_width, max_height in _RESOLUTION_LIMITS:
        if width <= max_width and height <= max_height:
            return bucket
    return "other"


@dataclass(kw_only=True, slots=True)
class NormalizedFile:
    probe: ProbeData
    container: str
    video_codec: str | None
    width: int | None
    height: int | None
    resolution_bucket: str | None
    duration_s: float | None
    bit_rate: int | None
    medium: str


def normalize(path: Path, stat: os.stat_result, raw: dict) -> NormalizedFile:
    raw_streams: list[dict] = raw.get("streams") or []
    fmt: dict = raw.get("format") or {}

    probe = probe_from_raw(path, stat.st_size, raw)
    probe_streams = probe.streams

    video = next(
        (
            (rs, ps)
            for rs, ps in zip(raw_streams, probe_streams, strict=True)
            if ps.codec_type == "video" and not ps.disposition_attached_pic
        ),
        None,
    )
    has_audio = any(ps.codec_type == "audio" for ps in probe_streams)

    duration = _float(fmt.get("duration"))
    bit_rate = _int(fmt.get("bit_rate"))
    if bit_rate is None and duration:
        bit_rate = round(stat.st_size * 8 / duration)

    container = path.suffix.lstrip(".").lower()

    if video is not None:
        rs, ps = video
        width, height = _int(rs.get("width")), _int(rs.get("height"))
        video_codec: str | None = ps.codec
        bucket = resolution_bucket(width, height)
        medium = "video"
    else:
        width = height = bucket = video_codec = None
        medium = "audio" if has_audio else "other"

    return NormalizedFile(
        probe=probe,
        container=container,
        video_codec=video_codec,
        width=width,
        height=height,
        resolution_bucket=bucket,
        duration_s=duration,
        bit_rate=bit_rate,
        medium=medium,
    )


def _int(value: str | float | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float(value: str | float | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
