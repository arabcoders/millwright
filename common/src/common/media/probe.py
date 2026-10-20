from __future__ import annotations

import re
from math import floor
from pathlib import PurePath
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, Field, model_validator

from common.media.language import normalize_language

if TYPE_CHECKING:
    from pathlib import Path

_DESCRIPTIVE_TITLE = re.compile(r"commentary|description|sdh", re.IGNORECASE)
# Planar suffixes and P0/P2/P4 formats encode component depth, unlike NV layouts
# and packed RGB formats whose numbers describe layout or total pixel width.
_PIX_FMT_DEPTH = re.compile(r"(?:p|gray|p[024])(9|10|12|14|16)(?:le|be)?$")
_DV_TAG_STRINGS = frozenset({"dvh1", "dvhe", "dav1", "dvc1"})


class ProbeStream(BaseModel):
    index: int
    codec: str
    codec_type: str
    language: str | None = None
    title: str | None = None
    filename: str | None = None
    mimetype: str | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    profile: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    width: int | None = None
    height: int | None = None
    bit_rate: int | None = None
    disposition_default: bool = False
    disposition_forced: bool = False
    disposition_commentary: bool = False
    disposition_attached_pic: bool = False
    ten_bit: bool | None = None
    color_transfer: str | None = None
    bit_depth: int | None = None
    dolby_vision: bool = False
    is_descriptive: bool = False

    @model_validator(mode="after")
    def normalize_language(self) -> Self:
        if self.language:
            self.language = normalize_language(self.language, title=self.title)
        return self

    @model_validator(mode="after")
    def compute_descriptive(self) -> Self:
        if not self.is_descriptive:
            title_hit = self.title is not None and _DESCRIPTIVE_TITLE.search(self.title) is not None
            self.is_descriptive = self.disposition_commentary or title_hit
        return self


class ProbeData(BaseModel):
    file_path: str
    container: str = "original"
    duration_s: float = Field(allow_inf_nan=False)
    size_bytes: int
    streams: list[ProbeStream]
    # Preserve ffprobe fields not covered by the model.
    raw: dict | None = None

    @property
    def audio(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "audio"]

    @property
    def video(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "video"]

    @property
    def real_video(self) -> list[ProbeStream]:
        return [s for s in self.video if not s.disposition_attached_pic]

    @property
    def subtitle(self) -> list[ProbeStream]:
        return [s for s in self.streams if s.codec_type == "subtitle"]

    @property
    def video_bitrate_bps(self) -> int | None:
        first = self.real_video[0] if self.real_video else None
        if first is not None and first.bit_rate:
            return first.bit_rate
        if self.duration_s <= 0:
            return None
        return round(self.size_bytes * 8 / self.duration_s)

    @property
    def container_bitrate_bps(self) -> int | None:
        rate = _positive_int(((self.raw or {}).get("format") or {}).get("bit_rate"))
        if rate is not None:
            return rate
        if self.duration_s <= 0:
            return None
        return round(self.size_bytes * 8 / self.duration_s)

    @property
    def average_bitrate_bps(self) -> int | None:
        video = self.real_video[0] if self.real_video else None
        rates = [rate for rate in (video.bit_rate if video else None, self.container_bitrate_bps) if rate and rate > 0]
        return round(sum(rates) / len(rates)) if rates else None

    def target_bitrate_kbps(self, source: str, ratio: float) -> int | None:
        bps = {
            "video": self.video_bitrate_bps,
            "container": self.container_bitrate_bps,
            "average": self.average_bitrate_bps,
        }[source]
        if bps is None:
            return None
        if source == "video":
            return round(bps * ratio / 1000)
        return floor(floor(bps / 1000 + 0.5) * ratio + 0.5)


def mkv_repair_ratio(probe: ProbeData) -> int:
    if probe.container != "mkv":
        return 0
    if any(stream.codec.lower() in {"", "unknown", "none"} for stream in probe.subtitle):
        # Reserve space for the normalized MKV and extracted/converted subtitle files.
        return 4
    if any(
        PurePath(stream.filename or "").suffix.lower() in {".ttf", ".otf"}
        and (stream.mimetype or "").lower().startswith("image/")
        for stream in probe.streams
    ):
        # A separate source copy plus one extracted font for checking attachment contents.
        return 2
    return 0


def probe_from_raw(path: Path, size_bytes: int, raw: dict) -> ProbeData:
    fmt: dict = raw.get("format") or {}
    return ProbeData(
        file_path=str(path),
        container=path.suffix.lstrip(".").lower(),
        duration_s=_duration(fmt.get("duration")),
        size_bytes=size_bytes,
        streams=[probe_stream(stream) for stream in raw.get("streams") or []],
        raw=raw,
    )


def _duration(value: str | float | None) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _fps(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    num, _, den = value.partition("/")
    try:
        numerator, denominator = int(num), int(den)
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return numerator / denominator


def probe_stream(raw: dict) -> ProbeStream:
    tags: dict = raw.get("tags") or {}
    disposition: dict = raw.get("disposition") or {}
    # Matroska remuxing loses the attached-picture flag but retains image MIME tags.
    mimetype = _lower(next((value for key, value in tags.items() if key.lower() == "mimetype"), None))
    filename = next((value for key, value in tags.items() if key.lower() == "filename"), None)
    # Mislabelled fonts remain attachments for stream policy and verification.
    # The executor checks their bytes before repairing the MIME metadata.
    font = (
        PurePath(filename).suffix.lower().lstrip(".")
        if isinstance(filename, str) and mimetype is not None and mimetype.startswith("image/")
        else None
    )
    font = font if font in {"ttf", "otf"} else None
    artwork = raw.get("codec_type") == "video" and mimetype is not None and mimetype.startswith("image/")
    return ProbeStream(
        index=int(raw["index"]),
        codec=font or raw.get("codec_name") or "",
        codec_type="attachment" if font else raw.get("codec_type") or "",
        language=_lower(tags.get("language")),
        title=tags.get("title"),
        filename=filename,
        mimetype=mimetype,
        fps=_fps(raw.get("avg_frame_rate")),
        pix_fmt=_lower(raw.get("pix_fmt")),
        profile=raw.get("profile"),
        channels=_int(raw.get("channels")),
        channel_layout=raw.get("channel_layout"),
        sample_rate=_int(raw.get("sample_rate")),
        width=_int(raw.get("width")),
        height=_int(raw.get("height")),
        bit_rate=_positive_int(raw.get("bit_rate"))
        or _positive_int(tags.get("BPS"))
        or _positive_int(tags.get("BPS-eng")),
        disposition_default=bool(disposition.get("default")),
        disposition_forced=bool(disposition.get("forced")),
        disposition_commentary=bool(disposition.get("commentary")),
        disposition_attached_pic=not font and (bool(disposition.get("attached_pic")) or artwork),
        ten_bit=None if (depth := _bit_depth(raw)) is None else depth >= 10,
        color_transfer=_lower(raw.get("color_transfer")),
        bit_depth=depth,
        dolby_vision=_dolby_vision(raw),
    )


def _bit_depth(raw: dict) -> int | None:
    bits = _positive_int(raw.get("bits_per_raw_sample"))
    if bits is not None:
        return bits
    pix_fmt = raw.get("pix_fmt")
    if not isinstance(pix_fmt, str) or not pix_fmt:
        return None
    pix_fmt = pix_fmt.lower()
    if pix_fmt in {"nv12", "nv16", "nv21", "nv24", "nv42"}:
        return 8
    if match := _PIX_FMT_DEPTH.search(pix_fmt):
        return int(match.group(1))
    if match := re.fullmatch(r"(?:rgb|bgr)(24|48)(?:le|be)?", pix_fmt):
        return int(match.group(1)) // 3
    return None if re.search(r"\d+(?:le|be)?$", pix_fmt) else 8


def _dolby_vision(raw: dict) -> bool:
    if _lower(raw.get("codec_tag_string")) in _DV_TAG_STRINGS:
        return True
    return any(
        isinstance(entry, dict) and entry.get("dv_profile") is not None for entry in raw.get("side_data_list") or []
    )


def _lower(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value.lower()
    return None


def _int(value: str | float | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _positive_int(value: str | float | None) -> int | None:
    parsed = _int(value)
    return parsed if parsed is not None and parsed > 0 else None
