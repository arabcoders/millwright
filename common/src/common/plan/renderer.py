from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from common.enums import RateControlMode, StreamOutcome
from common.media.probe import ProbeData, ProbeStream
from common.plan.metadata import metadata_updates
from common.plan.models import AudioTarget, FFmpegPlan, StreamSelection

_CHANNEL_TITLES = {8: "7.1", 6: "5.1", 2: "2.0"}
_OUTPUT_MUXERS = {"mkv": "matroska", "ts": "mpegts", "m4v": "ipod", "wmv": "asf", "mpg": "mpeg"}
_RENDER_OPTIONS = {
    "-y",
    "-hide_banner",
    "-fflags",
    "-i",
    "-map",
    "-c",
    "-f",
    "-map_chapters",
    "-pix_fmt",
    "-preset",
    "-filter",
    "-crf",
    "-cq",
    "-global_quality",
    "-qp",
    "-qp_i",
    "-qp_p",
    "-rc",
    "-b",
    "-bsf",
    "-minrate",
    "-maxrate",
    "-bufsize",
    "-metadata",
    "-disposition",
}


def dst_for(src: str, plan: FFmpegPlan, *, input_container: str | None = None) -> str:
    if plan.container == "original":
        return src if input_container is None else str(Path(src).with_suffix(f".{input_container}"))
    return f"{Path(src).with_suffix('')}.{plan.container}"


@dataclass(kw_only=True)
class StreamLogEntry:
    stream_index: int
    codec: str
    language: str | None
    outcome: StreamOutcome
    title: str | None = None
    detail: str = ""


@dataclass(kw_only=True)
class RenderResult:
    argv: list[str]
    stream_log: list[StreamLogEntry]


def _entry(stream: ProbeStream, outcome: StreamOutcome, detail: str = "") -> StreamLogEntry:
    return StreamLogEntry(
        stream_index=stream.index,
        codec=stream.codec,
        language=stream.language,
        outcome=outcome,
        title=stream.title,
        detail=detail,
    )


def _channel_title(channels: int | None) -> str | None:
    if channels is not None and channels in _CHANNEL_TITLES:
        return _CHANNEL_TITLES[channels]
    return None


def _language_drop_reason(stream: ProbeStream, sel: StreamSelection) -> str | None:
    if sel.keep_languages is None:
        return None
    if stream.language in sel.keep_languages:
        return None
    if (stream.language or "und") == "und" and (sel.retag_und_as is not None or "und" in sel.keep_languages):
        return None
    if sel.keep_forced and stream.disposition_forced:
        return None
    return "language not in keep list"


def _drop_reason(stream: ProbeStream, sel: StreamSelection) -> str | None:
    if sel.drop_descriptive and stream.is_descriptive:
        return "descriptive track"
    return _language_drop_reason(stream, sel)


def _drop_reasons(streams: list[ProbeStream], sel: StreamSelection | None) -> dict[int, str]:
    if sel is None:
        return {}
    drops: dict[int, str] = {}
    kept_per_language: dict[str | None, int] = {}
    for n, stream in enumerate(streams):
        reason = _drop_reason(stream, sel)
        if reason is not None:
            drops[n] = reason
            continue
        language = stream.language or "und"
        kept_per_language[language] = kept_per_language.get(language, 0) + 1
        if sel.max_per_language is not None and kept_per_language[language] > sel.max_per_language:
            drops[n] = "max per language reached"
    return drops


def _filter_path(path: str) -> str:
    option_escaped = "".join(f"\\{char}" if char in "\\:'" else char for char in path)
    return "".join(f"\\{char}" if char in "\\,[];'" else char for char in option_escaped)


def _video_args(plan: FFmpegPlan, probe: ProbeData, subtitle_path: str | None = None) -> list[str]:
    video = plan.video
    if video is None:
        return []
    args = ["-c:v", video.encoder]
    vaapi = video.encoder.endswith("_vaapi")
    if video.pixel_format is not None and not vaapi:
        args += ["-pix_fmt", video.pixel_format]
    if video.preset is not None:
        args += ["-preset", video.preset]
    filters: list[str] = []
    if plan.burn_subtitle is not None:
        if subtitle_path is None:
            raise ValueError("subtitle_path is required when burning subtitles")
        filters.append(f"subtitles=filename={_filter_path(subtitle_path)}")
    if vaapi:
        filters += [f"format={video.pixel_format or 'nv12'}", "hwupload"]
    rate_args: list[str] = []
    rate = video.rate
    if rate is not None and video.encoder.endswith("_nvenc"):
        rate_args += ["-rc", "vbr"]
    if rate is not None and video.encoder.endswith("_amf"):
        rate_args += ["-rc", "cqp" if rate.mode == RateControlMode.QUALITY else "vbr_peak"]
    if rate is not None and rate.mode == RateControlMode.QUALITY:
        if rate.quality is None:
            raise ValueError("Quality rate control requires a quality value.")
        if video.encoder.endswith("_amf"):
            rate_args += ["-qp_i", str(rate.quality), "-qp_p", str(rate.quality)]
        else:
            rate_args += [f"-{rate.quality_option}", str(rate.quality)]
        if video.encoder.endswith("_nvenc"):
            # NVENC otherwise retains its default bitrate in constant-quality VBR.
            rate_args += ["-b:v", "0"]
    source_bps = probe.video_bitrate_bps
    if rate is not None and rate.bitrate_source == "container":
        source_bps = probe.container_bitrate_bps
    elif rate is not None and rate.bitrate_source == "average":
        source_bps = probe.average_bitrate_bps
    if rate is not None and rate.mode == RateControlMode.BITRATE:
        if source_bps is None:
            raise ValueError("Bitrate rate control requires a source bitrate.")
        target_kbps = probe.target_bitrate_kbps(rate.bitrate_source, rate.from_source_bitrate)
        assert target_kbps is not None
        rate_args += [
            "-b:v",
            f"{target_kbps}k",
            "-minrate",
            f"{round(target_kbps * (1 - rate.tolerance))}k",
            "-maxrate",
            f"{round(target_kbps * (1 + rate.tolerance))}k",
            "-bufsize",
            f"{round(source_bps / 1000)}k",
        ]
    retained = [
        stream
        for stream in probe.video
        if not (plan.streams is not None and plan.streams.drop_cover_art and stream.disposition_attached_pic)
    ]
    scoped: list[str] = []
    for n, stream in enumerate(retained):
        if stream.disposition_attached_pic:
            continue
        stream_filters: list[str] = []
        exceeds = (video.max_width is not None and stream.width is not None and stream.width > video.max_width) or (
            video.max_height is not None and stream.height is not None and stream.height > video.max_height
        )
        if exceeds:
            scale = f"{video.max_width or -2}:{video.max_height or -2}"
            stream_filters.append(f"scale={scale}:force_original_aspect_ratio=decrease:force_divisible_by=2")
        stream_filters += filters
        stream_args = args + (["-filter", ",".join(stream_filters)] if stream_filters else []) + rate_args
        for option, argument in zip(stream_args[::2], stream_args[1::2], strict=True):
            scoped += [f"{option.partition(':')[0]}:v:{n}", argument]
    return scoped


def _claimed_audio_targets(
    targets: list[AudioTarget],
    audio: list[ProbeStream],
    drops: dict[int, str],
) -> dict[int, AudioTarget]:
    claimed: dict[int, AudioTarget] = {}
    for target in targets:
        for n, stream in enumerate(audio):
            if n in drops or n in claimed:
                continue
            if target.when_codec_in and stream.codec not in target.when_codec_in:
                continue
            claimed[n] = target
    return claimed


def audio_targets(plan: FFmpegPlan, probe: ProbeData) -> dict[int, AudioTarget]:
    selection = plan.streams.audio if plan.streams else None
    drops = _drop_reasons(probe.audio, selection)
    if drops and len(drops) == len(probe.audio):
        if selection is not None and selection.keep_all_if_empty:
            drops.clear()
        else:
            del drops[0]
    return _claimed_audio_targets(plan.audio_encode, probe.audio, drops)


def _audio_metadata_args(audio: list[ProbeStream], sel: StreamSelection, drops: dict[int, str]) -> list[str]:
    args: list[str] = []
    for n, stream in enumerate(audio):
        if n in drops:
            continue
        output_n = _audio_output_index(n, drops)
        if sel.retag_und_as is not None and (stream.language or "und") == "und":
            args += [f"-metadata:s:a:{output_n}", f"language={sel.retag_und_as}"]
        title = _channel_title(stream.channels) if sel.title_from_channel_layout else None
        if title is not None:
            args += [f"-metadata:s:a:{output_n}", f"title={title}"]
    if sel.set_default_language is not None:
        selected = next(
            (
                n
                for n, stream in enumerate(audio)
                if n not in drops
                and (
                    sel.retag_und_as
                    if sel.retag_und_as is not None and (stream.language or "und") == "und"
                    else stream.language
                )
                == sel.set_default_language
            ),
            None,
        )
        if selected is not None:
            for n in range(len(audio)):
                if n not in drops:
                    args += [
                        f"-disposition:a:{_audio_output_index(n, drops)}",
                        "+default" if n == selected else "-default",
                    ]
    return args


def _audio_output_index(n: int, drops: dict[int, str]) -> int:
    return sum(1 for kept in range(n) if kept not in drops)


def _video_log(probe: ProbeData, plan: FFmpegPlan) -> list[StreamLogEntry]:
    if plan.video is None:
        return [_entry(stream, StreamOutcome.COPY, "remux") for stream in probe.video]
    detail = f"encode {plan.video.encoder}"
    if plan.burn_subtitle is not None:
        detail += f", burn subtitles {plan.burn_subtitle.filename}"
    return [
        _entry(stream, StreamOutcome.COPY, "remux")
        if stream.disposition_attached_pic
        else _entry(stream, StreamOutcome.TRANSCODE, detail)
        for stream in probe.video
    ]


def _audio_log(
    probe: ProbeData,
    sel: StreamSelection | None,
    drops: dict[int, str],
    kept_notes: dict[int, str],
    claimed: dict[int, AudioTarget],
) -> list[StreamLogEntry]:
    entries: list[StreamLogEntry] = []
    for n, stream in enumerate(probe.audio):
        if n in drops:
            entries.append(_entry(stream, StreamOutcome.DROP, drops[n]))
            continue
        retag_effects: list[str] = []
        if sel is not None:
            if sel.retag_und_as is not None and (stream.language or "und") == "und":
                retag_effects.append(f"language={sel.retag_und_as}")
            title = _channel_title(stream.channels) if sel.title_from_channel_layout else None
            if title is not None:
                retag_effects.append(f"title={title}")
        target = claimed.get(n)
        if target is not None:
            outcome = StreamOutcome.TRANSCODE
        elif retag_effects:
            outcome = StreamOutcome.RETAG
        else:
            outcome = StreamOutcome.COPY
        detail_parts: list[str] = []
        if n in kept_notes:
            detail_parts.append(kept_notes[n])
        detail_parts += retag_effects
        if target is not None:
            detail_parts.append(f"encode {target.codec} {target.bitrate}")
        entries.append(_entry(stream, outcome, ", ".join(detail_parts)))
    return entries


def _subtitle_log(probe: ProbeData, drops: dict[int, str], container: str) -> list[StreamLogEntry]:
    entries: list[StreamLogEntry] = []
    for n, stream in enumerate(probe.subtitle):
        if n in drops:
            entries.append(_entry(stream, StreamOutcome.DROP, drops[n]))
        elif container == "mkv" and stream.codec in {"mov_text", "webvtt"}:
            entries.append(_entry(stream, StreamOutcome.TRANSCODE, "encode srt"))
        elif probe.container == "mkv" and stream.codec.lower() in {"", "unknown", "none"}:
            # Execution must normalize unsupported subtitles before FFmpeg.
            # Recording conversion also protects originals under transcode retention.
            entries.append(_entry(stream, StreamOutcome.TRANSCODE, "repair unsupported subtitle as srt"))
        elif container == "mp4" and stream.codec == "subrip":
            entries.append(_entry(stream, StreamOutcome.TRANSCODE, "encode mov_text"))
        elif stream.disposition_forced:
            entries.append(_entry(stream, StreamOutcome.COPY, "forced"))
        else:
            entries.append(_entry(stream, StreamOutcome.COPY))
    return entries


def _stream_log(plan: FFmpegPlan, probe: ProbeData) -> list[StreamLogEntry]:
    audio_sel = plan.streams.audio if plan.streams else None
    sub_sel = plan.streams.subtitle if plan.streams else None
    audio_drops = _drop_reasons(probe.audio, audio_sel)
    kept_notes: dict[int, str] = {}
    if audio_drops and len(audio_drops) == len(probe.audio):
        if audio_sel is not None and audio_sel.keep_all_if_empty:
            audio_drops.clear()
        else:
            del audio_drops[0]
            kept_notes[0] = "kept as last remaining audio stream"
    sub_drops = _drop_reasons(probe.subtitle, sub_sel)
    container = probe.container if plan.container == "original" else plan.container
    log = [
        *_video_log(probe, plan),
        *_audio_log(probe, audio_sel, audio_drops, kept_notes, audio_targets(plan, probe)),
        *_subtitle_log(probe, sub_drops, container),
    ]
    if plan.streams is not None:
        for stream in probe.streams:
            if (plan.streams.drop_data and stream.codec_type == "data") or (
                plan.streams.drop_cover_art and stream.codec_type == "video" and stream.disposition_attached_pic
            ):
                log = [entry for entry in log if entry.stream_index != stream.index]
                reason = "data streams not kept" if stream.codec_type == "data" else "cover art not kept"
                log.append(_entry(stream, StreamOutcome.DROP, reason))
    return log


def change_reasons(plan: FFmpegPlan, probe: ProbeData) -> list[str]:
    # Stream decisions do not require a node-selected encoder or executable argv.
    reasons: list[str] = []
    container = probe.container if plan.container == "original" else plan.container
    if container != probe.container:
        reasons.append(f"Change container from {probe.container.upper()} to {container.upper()}.")
    if updates := metadata_updates(plan.metadata, probe, container):
        reasons.append(f"Update global metadata: {', '.join(updates)}.")
    streams = {stream.index: stream for stream in probe.streams}
    for entry in _stream_log(plan, probe):
        if entry.outcome == StreamOutcome.COPY:
            continue
        stream = streams[entry.stream_index]
        reasons.append(
            f"{entry.outcome.value.capitalize()} {stream.codec_type} stream {stream.index} "
            f"({entry.language or 'und'}, {entry.codec}): {entry.detail}."
        )
    return reasons


def render(plan: FFmpegPlan, probe: ProbeData, src: str, dst: str, *, subtitle_path: str | None = None) -> RenderResult:
    # -map must follow -i or FFmpeg rejects it as an input option.
    argv = ["-y", "-hide_banner"]
    if plan.video is not None and plan.video.encoder_priority:
        raise ValueError("video encoder must be selected before rendering")
    if plan.video is not None and plan.video.encoder.endswith("_vaapi"):
        argv += ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va"]
    if plan.generate_timestamps:
        argv += ["-fflags", "+genpts"]
    argv += ["-i", src, "-map", "0", "-c", "copy"]
    if not plan.chapters:
        argv += ["-map_chapters", "-1"]

    log = _stream_log(plan, probe)
    drops = {entry.stream_index: entry.detail for entry in log if entry.outcome == StreamOutcome.DROP}
    audio_drops = {n: drops[stream.index] for n, stream in enumerate(probe.audio) if stream.index in drops}
    sub_drops = {n: drops[stream.index] for n, stream in enumerate(probe.subtitle) if stream.index in drops}
    for n in sorted(audio_drops):
        argv += ["-map", f"-0:a:{n}"]
    for n in sorted(sub_drops):
        argv += ["-map", f"-0:s:{n}"]
    for stream in probe.streams:
        if stream.index in drops and stream.codec_type not in {"audio", "subtitle"}:
            argv += ["-map", f"-0:{stream.index}"]

    container = probe.container if plan.container == "original" else plan.container
    for key, value in metadata_updates(plan.metadata, probe, container).items():
        argv += ["-metadata:g", f"{key}={value}"]
    for n, stream in enumerate(probe.subtitle):
        if n in sub_drops:
            continue
        output_n = sum(1 for kept in range(n) if kept not in sub_drops)
        if container == "mkv" and stream.codec in {"mov_text", "webvtt"}:
            argv += [f"-c:s:{output_n}", "srt"]
        elif container == "mp4" and stream.codec == "subrip":
            argv += [f"-c:s:{output_n}", "mov_text"]

    argv += _video_args(plan, probe, subtitle_path)

    claimed = audio_targets(plan, probe)
    for n, target in claimed.items():
        output_n = _audio_output_index(n, audio_drops)
        argv += [f"-c:a:{output_n}", target.codec, f"-b:a:{output_n}", target.bitrate]

    if probe.container == "ts" and container in {"mkv", "mp4"}:
        for n, stream in enumerate(probe.audio):
            if stream.codec == "aac" and n not in audio_drops and n not in claimed:
                argv += [f"-bsf:a:{_audio_output_index(n, audio_drops)}", "aac_adtstoasc"]

    audio_sel = plan.streams.audio if plan.streams else None
    if audio_sel is not None:
        argv += _audio_metadata_args(probe.audio, audio_sel, audio_drops)

    extras = list(plan.extras)
    if container == "mp4":
        # Native MP4 atoms drop unrelated mdta tags, including tags from an earlier stage.
        # Keep all global tags through every pass and compose with caller muxer flags.
        positions = [n for n, arg in enumerate(extras) if arg == "-movflags"]
        if positions:
            position = positions[-1] + 1
            if position == len(extras):
                raise ValueError("extras -movflags requires a value")
            extras[position] += "+use_metadata_tags"
        else:
            argv += ["-movflags", "+use_metadata_tags"]
    argv += ["-f", _OUTPUT_MUXERS.get(container, container)]
    generated = {arg.partition(":")[0] for arg in argv} & _RENDER_OPTIONS
    for arg in plan.extras:
        option = arg.partition(":")[0]
        if option in {"-codec", "-vcodec", "-acodec", "-scodec"}:
            option = "-c"
        elif option == "-vf":
            option = "-filter"
        if option in generated:
            msg = f"extras argument {arg!r} conflicts with a renderer-generated option"
            raise ValueError(msg)
    argv += extras
    argv.append(dst)

    return RenderResult(argv=argv, stream_log=log)


def changes_media(plan: FFmpegPlan, probe: ProbeData) -> bool:
    container = probe.container if plan.container == "original" else plan.container
    if metadata_updates(plan.metadata, probe, container):
        return True
    if plan.container not in {"original", probe.container} or plan.video is not None:
        return True
    if plan.extras or not plan.chapters or plan.generate_timestamps:
        return True
    rendered = render(plan, probe, probe.file_path, probe.file_path)
    return any(entry.outcome != StreamOutcome.COPY for entry in rendered.stream_log) or any(
        arg.startswith(("-metadata:", "-disposition:", "-c:s:")) for arg in rendered.argv
    )


def render_subtitle(src: str, dst: str, *, stream_index: int, format: str) -> list[str]:
    codec = {"srt": "srt", "vtt": "webvtt"}[format]
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        src,
        "-map",
        f"0:{stream_index}",
        "-c:s",
        codec,
        "-f",
        codec,
        dst,
    ]


class PlaybackPlan(BaseModel):
    """Run H.264/AAC encoding continuously and let the muxer split segments."""

    start_s: float = Field(ge=0)
    duration_s: float = Field(gt=0)
    segment_s: float = Field(gt=0)
    read_ahead_s: float = Field(gt=0)
    video_encoder: str = "libx264"
    # Absolute input stream indexes. Attached pictures are not playback video.
    video_stream_index: int | None = None
    audio_stream_index: int | None = None
    subtitle_stream_index: int | None = Field(default=None, ge=0, strict=True)
    audio_codec: str = "aac"

    @field_validator("video_encoder", "audio_codec")
    @classmethod
    def check_codec_name(cls, value: str) -> str:
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            msg = f"codec names must be plain ffmpeg names: {value!r}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def check_subtitle_video(self) -> PlaybackPlan:
        if self.subtitle_stream_index is not None and self.video_stream_index is None:
            raise ValueError("Bitmap subtitles require a video stream.")
        return self


def render_playback(plan: PlaybackPlan, src: str) -> list[str]:
    """
    TS bytes go to stdout. Bounded HLS snapshots on stderr locate completed segments.
    Neither output opens a filesystem path. The server handles temporary file writes and backpressure.
    """
    argv = [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-ss",
        f"{plan.start_s:.6f}",
        "-t",
        f"{plan.duration_s:.6f}",
        "-readrate",
        "1",
        "-readrate_initial_burst",
        f"{plan.read_ahead_s:.6f}",
    ]
    if plan.video_stream_index is not None and plan.video_encoder.endswith("_vaapi"):
        argv += ["-init_hw_device", "vaapi=va", "-filter_hw_device", "va"]
    argv += ["-i", src, "-map_metadata", "-1"]
    if plan.video_stream_index is not None:
        filters = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        if plan.video_encoder.endswith("_vaapi"):
            filters += ",format=nv12,hwupload"
        if plan.subtitle_stream_index is not None:
            graph = (
                f"[0:{plan.video_stream_index}][0:{plan.subtitle_stream_index}]overlay=eof_action=pass,{filters}[video]"
            )
            argv += ["-filter_complex", graph, "-map", "[video]"]
        else:
            argv += ["-map", f"0:{plan.video_stream_index}", "-vf", filters]
        argv += ["-c:v", plan.video_encoder, "-b:v", "4M", "-maxrate", "6M", "-bufsize", "12M"]
        argv += ["-flags", "+cgop", "-bf", "0", "-force_key_frames", f"expr:gte(t,n_forced*{plan.segment_s:.6f})"]
        if plan.video_encoder == "libx264":
            argv += ["-x264-params", "open-gop=0:scenecut=0"]
        elif plan.video_encoder == "h264_nvenc":
            argv += ["-forced-idr", "1"]
        if not plan.video_encoder.endswith("_vaapi"):
            argv += ["-pix_fmt", "yuv420p"]
    if plan.audio_stream_index is not None:
        argv += ["-map", f"0:{plan.audio_stream_index}", "-c:a", plan.audio_codec, "-ac", "2", "-b:a", "192k"]
    argv += [
        "-sn",
        "-dn",
        "-output_ts_offset",
        f"{plan.start_s:.6f}",
        "-f",
        "hls",
        "-hls_time",
        f"{plan.segment_s:.6f}",
        "-hls_list_size",
        "2",
        "-hls_flags",
        "single_file+independent_segments",
        "-hls_segment_filename",
        "pipe:1",
        "pipe:2",
    ]
    return argv
