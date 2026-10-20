from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from common.enums import RateControlMode
from common.media.language import normalize_language
from common.metadata import MetadataValues

_LANGUAGE = re.compile(r"^[a-z]{3}$")
_CONTAINERS = ("mkv", "mp4", "original")
_SUBTITLE_EXTENSIONS = ("srt", "ass", "ssa", "vtt")


def _checked_language(code: str) -> str:
    if _LANGUAGE.fullmatch(code) is None:
        msg = f"Language codes must contain three lowercase letters: {code!r}"
        raise ValueError(msg)
    return normalize_language(code)


class StreamSelection(BaseModel):
    # None keeps every language. [] applies forced-track retention only.
    # The renderer never drops the last audio stream.
    keep_languages: list[str] | None = None
    drop_descriptive: bool = False
    max_per_language: int | None = Field(default=None, ge=1)
    set_default_language: str | None = None
    retag_und_as: str | None = None
    title_from_channel_layout: bool = False
    keep_forced: bool = True
    keep_all_if_empty: bool = False

    @field_validator("keep_languages")
    @classmethod
    def check_keep_languages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_checked_language(code) for code in value]

    @field_validator("set_default_language", "retag_und_as")
    @classmethod
    def check_language_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _checked_language(value)


class StreamPolicy(BaseModel):
    audio: StreamSelection | None = None
    subtitle: StreamSelection | None = None
    drop_data: bool = False
    drop_cover_art: bool = False


class StreamPolicyInputs(BaseModel):
    audio_keep_languages: list[str] = Field(
        default=[], description="Comma-separated ISO-639-2 audio languages (eng, jpn). Empty keeps all"
    )
    audio_drop_descriptive: bool = Field(default=False, description="Drop commentary/description audio tracks")
    audio_keep_forced: bool = Field(default=True, description="Keep forced audio tracks regardless of language")
    audio_keep_all_if_empty: bool = Field(
        default=False, description="Keep all audio if the language policy would remove every track"
    )
    audio_max_per_language: int | None = Field(
        default=None, ge=1, description="Keep at most this many audio tracks per language. Blank keeps all"
    )
    subtitle_keep_languages: list[str] = Field(
        default=[],
        description="Comma-separated ISO-639-2 subtitle languages. Empty leaves subtitles untouched",
    )
    subtitle_strip_all: bool = Field(default=False, description="Drop all subtitles except retained forced tracks")
    subtitle_keep_forced: bool = Field(default=True, description="Keep forced subtitles regardless of language")
    drop_data: bool = Field(default=False, description="Drop all data streams")
    drop_cover_art: bool = Field(default=False, description="Drop embedded picture video streams")

    def stream_policy(self) -> StreamPolicy | None:
        audio = None
        if self.audio_keep_languages or self.audio_drop_descriptive or self.audio_max_per_language is not None:
            audio = StreamSelection(
                keep_languages=self.audio_keep_languages or None,
                drop_descriptive=self.audio_drop_descriptive,
                max_per_language=self.audio_max_per_language,
                keep_forced=self.audio_keep_forced,
                keep_all_if_empty=self.audio_keep_all_if_empty,
            )
        subtitle = None
        if self.subtitle_strip_all:
            subtitle = StreamSelection(keep_languages=[], keep_forced=self.subtitle_keep_forced)
        elif self.subtitle_keep_languages:
            subtitle = StreamSelection(
                keep_languages=self.subtitle_keep_languages, keep_forced=self.subtitle_keep_forced
            )
        if audio is None and subtitle is None and not (self.drop_data or self.drop_cover_art):
            return None
        return StreamPolicy(
            audio=audio, subtitle=subtitle, drop_data=self.drop_data, drop_cover_art=self.drop_cover_art
        )


class AudioPlanInputs(StreamPolicyInputs):
    audio_codec: str | None = Field(default=None, description="Codec for re-encoding matching audio tracks")
    audio_bitrate: str | None = Field(default=None, description="Target bitrate for re-encoded audio tracks")
    audio_when_codecs: list[str] = Field(
        default=[], description="Re-encode audio with these source codecs. Empty matches all"
    )

    @field_validator("audio_codec", "audio_bitrate", mode="before")
    @classmethod
    def empty_audio(cls, value: str | None) -> str | None:
        return None if value == "" else value

    @model_validator(mode="after")
    def check_audio_pair(self) -> AudioPlanInputs:
        if (self.audio_codec is None) != (self.audio_bitrate is None):
            msg = "audio_codec and audio_bitrate must be supplied together"
            raise ValueError(msg)
        return self

    def audio_targets(self) -> list[AudioTarget]:
        if self.audio_codec is None or self.audio_bitrate is None:
            return []
        return [AudioTarget(codec=self.audio_codec, bitrate=self.audio_bitrate, when_codec_in=self.audio_when_codecs)]


class RateControl(BaseModel):
    mode: RateControlMode = RateControlMode.QUALITY
    from_source_bitrate: float = 1.0
    bitrate_source: Literal["video", "container", "average"] = "video"
    quality: int | None = None
    quality_option: Literal["crf", "cq", "global_quality", "qp"] = "crf"
    tolerance: float = 0.25

    @field_validator("from_source_bitrate")
    @classmethod
    def check_multiplier(cls, value: float) -> float:
        if not 0 < value <= 1:
            msg = f"from_source_bitrate must be in (0, 1], got {value}"
            raise ValueError(msg)
        return value

    @field_validator("tolerance")
    @classmethod
    def check_tolerance(cls, value: float) -> float:
        if not 0 < value <= 1:
            msg = f"tolerance must be in (0, 1], got {value}"
            raise ValueError(msg)
        return value


class VideoTarget(BaseModel):
    encoder: str
    encoder_priority: list[str] = Field(default_factory=list)
    rate: RateControl | None = None
    pixel_format: str | None = None
    preset: str | None = None
    max_width: int | None = Field(default=None, ge=1)
    max_height: int | None = Field(default=None, ge=1)


class AudioTarget(BaseModel):
    codec: str
    bitrate: str
    when_codec_in: list[str] = []


class VerificationSpec(BaseModel):
    min_duration_ratio: float = 0.98
    size_ceiling_ratio: float = 1.05

    @field_validator("min_duration_ratio", "size_ceiling_ratio")
    @classmethod
    def check_positive(cls, value: float) -> float:
        if value <= 0:
            msg = f"verification ratios must be positive, got {value}"
            raise ValueError(msg)
        return value


class SubtitleBurn(BaseModel):
    filename: str

    @field_validator("filename")
    @classmethod
    def check_filename(cls, value: str) -> str:
        path = value.lower()
        if value in (".", "..") or any(char in value for char in ("/", "\\", "\x00")):
            raise ValueError("subtitle filename must be a safe basename")
        if "." not in value or path.rsplit(".", 1)[1] not in _SUBTITLE_EXTENSIONS:
            raise ValueError("subtitle filename must have an srt, ass, ssa, or vtt extension")
        return value


class FFmpegPlan(BaseModel):
    container: str = "mkv"
    metadata: MetadataValues | None = None
    streams: StreamPolicy | None = None
    video: VideoTarget | None = None
    burn_subtitle: SubtitleBurn | None = None
    audio_encode: list[AudioTarget] = []
    chapters: bool = True
    generate_timestamps: bool = False
    expected: VerificationSpec = Field(default_factory=VerificationSpec)
    extras: list[str] = []

    def select_encoder(self, encoders: list[str]) -> FFmpegPlan:
        if self.video is None or not self.video.encoder_priority:
            return self
        encoder = next((name for name in self.video.encoder_priority if name in encoders), None)
        if encoder is None:
            msg = f"no compatible encoder: {', '.join(self.video.encoder_priority)}"
            raise ValueError(msg)
        plan = self.model_copy(deep=True)
        assert plan.video is not None
        video = plan.video
        video.encoder, video.encoder_priority = encoder, []
        vendor = encoder.rsplit("_", 1)[-1]
        hardware = vendor in {"nvenc", "qsv", "vaapi", "amf"}
        if video.rate is not None:
            video.rate.quality_option = (
                "cq"
                if vendor == "nvenc"
                else "global_quality"
                if vendor == "qsv"
                else "qp"
                if vendor in {"vaapi", "amf"}
                else "crf"
            )
            if video.rate.mode == RateControlMode.QUALITY and video.rate.quality is None:
                video.rate.quality = 19 if hardware else {"libx264": 23, "libx265": 21, "libsvtav1": 30}.get(encoder)
        if video.pixel_format in {"yuv420p10le", "p010le"}:
            video.pixel_format = "p010le" if hardware else "yuv420p10le"
        elif vendor == "vaapi":
            video.pixel_format = "nv12"
        if vendor in {"vaapi", "amf"}:
            video.preset = None
        defaults = [("-spatial_aq:v", "1"), ("-rc-lookahead:v", "32")] if vendor == "nvenc" else []
        if encoder.startswith("hevc_") and video.pixel_format == "p010le":
            defaults.append(("-profile:v", "main10"))
        flags = {arg.split(":", 1)[0] for arg in plan.extras}
        for flag, value in defaults:
            if flag.split(":", 1)[0] not in flags:
                plan.extras += [flag, value]
        return plan

    @model_validator(mode="after")
    def check_burn_subtitle(self) -> FFmpegPlan:
        if self.burn_subtitle is not None and self.video is None:
            raise ValueError("burn_subtitle requires video")
        return self

    @field_validator("container")
    @classmethod
    def check_container(cls, value: str) -> str:
        if value not in _CONTAINERS:
            msg = f"container must be one of {', '.join(_CONTAINERS)}, got {value!r}"
            raise ValueError(msg)
        return value
