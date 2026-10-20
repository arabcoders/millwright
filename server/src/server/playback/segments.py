from __future__ import annotations

import math
from dataclasses import dataclass, field
from urllib.parse import urlencode

from common.plan.renderer import PlaybackPlan

# One segment is bounded to this many bytes by the output collector. The master playlist
# advertises the matching maximum bitrate this bound can produce.
SEGMENT_MAX_BYTES = 12 * 1024 * 1024
ENCODE_AHEAD = 2
_SEGMENT_TIMEOUT_SLACK_S = 60.0
_AUDIO_MIMETYPES = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "mka": "audio/x-matroska",
    "webm": "audio/webm",
}


class SegmentError(Exception):
    pass


def _safe_attribute(value: object) -> str:
    """Playlist quoted attributes never carry CR/LF or quote characters."""
    return "".join(char for char in str(value or "") if char not in '\r\n"').strip()


@dataclass(kw_only=True, slots=True)
class AudioTrack:
    stream_index: int
    lang: str
    name: str
    codec: str
    channels: int | None
    channel_layout: str
    default: bool


@dataclass(kw_only=True, slots=True)
class MediaSummary:
    """Read-only probe facts captured once per playback session."""

    container: str
    duration_s: float
    has_video: bool
    has_audio: bool
    mimetype: str
    video_stream_index: int | None = None
    audio_stream_index: int | None = None
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    video_codec: str | None = None
    video_profile: str | None = None
    video_pix_fmt: str | None = None
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_probe(cls, raw: dict) -> MediaSummary:
        streams = raw.get("streams") or []
        video = next(
            (
                stream
                for stream in streams
                if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")
            ),
            None,
        )
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        audio = next(
            (stream for stream in audio_streams if (stream.get("disposition") or {}).get("default")),
            audio_streams[0] if audio_streams else None,
        )
        fmt = raw.get("format") or {}
        try:
            duration = float(fmt.get("duration") or 0.0)
        except TypeError, ValueError:
            duration = 0.0
        if not math.isfinite(duration):
            duration = 0.0
        container = (fmt.get("format_name") or "").partition(",")[0]
        return cls(
            container=container,
            duration_s=duration,
            has_video=video is not None,
            has_audio=audio is not None,
            mimetype=_mimetype(video, container, audio),
            video_stream_index=int(video["index"]) if video is not None and "index" in video else None,
            audio_stream_index=int(audio["index"]) if audio is not None and "index" in audio else None,
            video_codec=video.get("codec_name") if video is not None else None,
            video_profile=video.get("profile") if video is not None else None,
            video_pix_fmt=video.get("pix_fmt") if video is not None else None,
            width=video.get("width") if video is not None else None,
            height=video.get("height") if video is not None else None,
            audio_tracks=[
                AudioTrack(
                    stream_index=int(stream["index"]),
                    lang=str((stream.get("tags") or {}).get("language") or "und"),
                    name=str((stream.get("tags") or {}).get("title") or f"Audio {index + 1}"),
                    codec=str(stream.get("codec_name") or "unknown"),
                    channels=int(stream["channels"]) if stream.get("channels") is not None else None,
                    channel_layout=str(stream.get("channel_layout") or ""),
                    default=stream is audio,
                )
                for index, stream in enumerate(audio_streams)
                if "index" in stream
            ],
        )


def _mimetype(video: dict | None, container: str, audio: dict | None) -> str:
    if video is None:
        return _AUDIO_MIMETYPES.get(
            container, _AUDIO_MIMETYPES.get(str((audio or {}).get("codec_name") or ""), "application/octet-stream")
        )
    codec = str((video or {}).get("codec_name") or "")
    if codec == "h264" and container in {"mp4", "mov", "m4v", "ipod", "mpeg4"}:
        return "video/mp4"
    if container in {"mp4", "ipod"}:
        return "video/mp4"
    if container == "mov":
        return "video/quicktime"
    if container == "matroska":
        return "video/x-matroska"
    if container == "webm":
        return "video/webm"
    return "application/octet-stream"


def segment_count(duration_s: float, segment_s: float) -> int:
    if duration_s <= 0:
        raise SegmentError("media has no duration")
    return max(1, math.ceil(duration_s / segment_s))


def segment_start(index: int, segment_s: float) -> float:
    return 0.0 if index == 0 else segment_s * index


def segment_duration(index: int, duration_s: float, segment_s: float) -> float:
    remaining = duration_s - index * segment_s
    if remaining <= 0:
        msg = f"segment {index} starts past the end of the media"
        raise SegmentError(msg)
    return min(segment_s, remaining)


def plan_playback(
    *,
    index: int,
    duration_s: float,
    segment_s: float,
    encoder: str,
    summary: MediaSummary,
    audio_stream_index: int | None = None,
    subtitle_stream_index: int | None = None,
) -> PlaybackPlan:
    return PlaybackPlan(
        start_s=segment_start(index, segment_s),
        duration_s=duration_s - segment_start(index, segment_s),
        segment_s=segment_s,
        read_ahead_s=segment_s * (ENCODE_AHEAD + 1),
        video_encoder=encoder,
        video_stream_index=summary.video_stream_index,
        audio_stream_index=summary.audio_stream_index if audio_stream_index is None else audio_stream_index,
        subtitle_stream_index=subtitle_stream_index,
    )


def encode_timeout_s(segment_s: float) -> float:
    return segment_s * 4.0 + _SEGMENT_TIMEOUT_SLACK_S


def bandwidth_bps(segment_s: float, max_bytes: int = SEGMENT_MAX_BYTES) -> int:
    """Maximum bitrate the enforced per-segment size bound can produce."""
    return math.ceil(max_bytes * 8 / max(segment_s, 0.001))


def media_playlist(
    duration_s: float,
    segment_s: float,
    *,
    audio_stream_index: int | None = None,
    subtitle_track_id: str | None = None,
) -> str:
    selection = {"audio_stream_index": audio_stream_index, "subtitle_track_id": subtitle_track_id}
    query = urlencode({key: value for key, value in selection.items() if value is not None})
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{math.ceil(segment_s)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index in range(segment_count(duration_s, segment_s)):
        lines.append(f"#EXTINF:{segment_duration(index, duration_s, segment_s):.6f},")
        lines.append(f"segments/{index}.ts" + (f"?{query}" if query else ""))
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def subtitle_playlist(duration_s: float, uri: str) -> str:
    return "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(duration_s))}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXTINF:{duration_s:.6f},",
            uri,
            "#EXT-X-ENDLIST",
        ]
    )


def master_playlist(
    native_subtitles: list[dict],
    bandwidth: int,
    *,
    audio_stream_index: int | None = None,
    subtitle_track_id: str | None = None,
) -> str:
    lines = ["#EXTM3U"]
    groups = ""
    for track in native_subtitles:
        groups = ',SUBTITLES="subs"'
        name = _safe_attribute(track.get("name") or track.get("lang") or "subtitles") or "subtitles"
        lang = _safe_attribute(track.get("lang")) or "und"
        uri = f"subtitles/{track['id']}.m3u8"
        lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",DEFAULT=NO,'
            f'AUTOSELECT=NO,FORCED=NO,LANGUAGE="{lang}",URI="{uri}"'
        )
    lines.append(f"#EXT-X-STREAM-INF:PROGRAM-ID=1{groups},BANDWIDTH={max(1, bandwidth)}")
    selection = {"audio_stream_index": audio_stream_index, "subtitle_track_id": subtitle_track_id}
    query = urlencode({key: value for key, value in selection.items() if value is not None})
    lines.append("stream.m3u8" + (f"?{query}" if query else ""))
    return "\n".join(lines)
