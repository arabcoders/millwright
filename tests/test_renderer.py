from __future__ import annotations

import pytest

from common.media.probe import ProbeData, ProbeStream
from common.metadata import MetadataValues
from common.plan import (
    AudioTarget,
    FFmpegPlan,
    RateControl,
    RenderResult,
    StreamPolicy,
    StreamSelection,
    SubtitleBurn,
    VideoTarget,
    render,
)
from common.plan.renderer import changes_media, render_subtitle
from common.enums import RateControlMode, StreamOutcome

SRC = "/media/input.mkv"
DST = "/cache/output.mkv"


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_metadata_render(container: str) -> None:
    probe = _probe().model_copy(
        update={"container": container, "raw": {"format": {"tags": {"TITLE": "Old", "copyright": "Keep"}}}}
    )
    plan = FFmpegPlan(container=container, metadata=MetadataValues(title="New", description="Plot\nالعربية"))
    rendered = render(plan, probe, SRC, DST)
    assert changes_media(plan, probe)
    values = [rendered.argv[n + 1] for n, value in enumerate(rendered.argv) if value == "-metadata:g"]
    description = "DESCRIPTION" if container == "mkv" else "description"
    assert values == ["TITLE=New", f"{description}=Plot\nالعربية"]
    assert "-map_metadata" not in rendered.argv
    assert rendered.argv[rendered.argv.index("-c") + 1] == "copy"


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_metadata_noop(container: str) -> None:
    probe = _probe().model_copy(
        update={
            "container": container,
            "raw": {
                "format": {
                    "tags": {
                        "TiTlE": "Title",
                        "DATE_RELEASED": "2020-12-04",
                        "SYNOPSIS": "Plot",
                        "TVSHOW": "Show",
                        "SEASON_NUMBER": "03",
                    }
                }
            },
        }
    )
    probe.streams = [stream for stream in probe.streams if stream.codec_type != "subtitle"]
    plan = FFmpegPlan(
        container=container,
        metadata=MetadataValues(title="Title", date="2020-12-04", description="Plot", show="Show", season_number="3"),
    )
    assert not changes_media(plan, probe)
    assert "-metadata:g" not in render(plan, probe, SRC, DST).argv


def test_mp4_metadata_flags() -> None:
    plan = FFmpegPlan(container="mp4", metadata=MetadataValues(title="Title"), extras=["-movflags", "+faststart"])
    first = render(plan, _probe(), SRC, DST).argv
    assert first[first.index("-movflags") + 1] == "+faststart+use_metadata_tags"
    # Later stages must retain mdta tags even after the request has been consumed.
    later = render(FFmpegPlan(container="mp4", extras=["-movflags", "frag_keyframe"]), _probe(), SRC, DST).argv
    assert later[later.index("-movflags") + 1] == "frag_keyframe+use_metadata_tags"


def test_unsupported_metadata_container() -> None:
    probe = _probe().model_copy(update={"container": "avi"})
    with pytest.raises(ValueError, match="supports MKV and MP4, not AVI"):
        render(FFmpegPlan(container="original", metadata=MetadataValues(title="Title")), probe, SRC, DST)


@pytest.mark.parametrize(
    ("encoder", "quality_option", "pixel"),
    [
        ("hevc_nvenc", "-cq", "p010le"),
        ("hevc_qsv", "-global_quality", "p010le"),
        ("hevc_vaapi", "-qp", "p010le"),
        ("hevc_amf", "-qp_i", "p010le"),
        ("libx265", "-crf", "yuv420p10le"),
    ],
)
def test_encoder_selection(encoder: str, quality_option: str, pixel: str) -> None:
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="hevc_nvenc",
            encoder_priority=["hevc_nvenc", "hevc_qsv", "hevc_vaapi", "hevc_amf", "libx265"],
            rate=RateControl(quality=None, bitrate_source="average", from_source_bitrate=0.5),
            pixel_format="p010le",
            preset="slow",
        )
    )
    selected = plan.select_encoder([encoder])
    assert selected.video is not None and selected.video.encoder == encoder
    argv = render(selected, _probe(), SRC, DST).argv
    assert argv[argv.index("-c:v:0") + 1] == encoder
    assert f"{quality_option}:v:0" in argv
    assert "-minrate:v:0" not in argv and "-maxrate:v:0" not in argv
    if encoder == "hevc_nvenc":
        assert argv[argv.index("-rc:v:0") + 1] == "vbr"
        assert argv[argv.index("-b:v:0") + 1] == "0"
    else:
        assert "-b:v:0" not in argv
    if encoder == "hevc_amf":
        assert argv[argv.index("-rc:v:0") + 1] == "cqp"
    assert ("-spatial_aq:v" in argv) == (encoder == "hevc_nvenc")
    if encoder == "hevc_vaapi":
        assert argv[argv.index("-filter:v:0") + 1] == "format=p010le,hwupload"
        assert argv[argv.index("-init_hw_device") + 1] == "vaapi=va"
        assert "-pix_fmt:v:0" not in argv
    else:
        assert argv[argv.index("-pix_fmt:v:0") + 1] == pixel
    if encoder == "libx265":
        assert argv[argv.index("-crf:v:0") + 1] == "21"
        assert argv[argv.index("-preset:v:0") + 1] == "slow"


@pytest.mark.parametrize(("encoder", "quality", "preset"), [("libx264", 23, "slow"), ("libsvtav1", 30, "6")])
def test_software_override(encoder: str, quality: int, preset: str) -> None:
    plan = FFmpegPlan(video=VideoTarget(encoder=encoder, rate=RateControl(quality=quality), preset=preset))
    argv = render(plan.select_encoder([encoder, "hevc_nvenc"]), _probe(), SRC, DST).argv
    assert argv[argv.index("-c:v:0") + 1] == encoder
    assert argv[argv.index("-crf:v:0") + 1] == str(quality)
    assert argv[argv.index("-preset:v:0") + 1] == preset


@pytest.mark.parametrize("encoder", ["hevc_qsv", "hevc_nvenc", "hevc_vaapi", "hevc_amf", "libx265"])
def test_bitrate_encoder_selection(encoder: str) -> None:
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="libx265",
            encoder_priority=[encoder],
            rate=RateControl(mode=RateControlMode.BITRATE, from_source_bitrate=0.5),
        )
    )
    selected = plan.select_encoder([encoder])
    assert selected.video is not None and selected.video.rate is not None
    assert selected.video.rate.mode == RateControlMode.BITRATE
    assert selected.video.rate.quality is None
    argv = render(selected, _probe(), SRC, DST).argv
    assert argv[argv.index("-b:v:0") + 1] == "2000k"
    assert argv[argv.index("-minrate:v:0") + 1] == "1500k"
    assert argv[argv.index("-maxrate:v:0") + 1] == "2500k"
    assert argv[argv.index("-bufsize:v:0") + 1] == "4000k"
    assert not any(
        option.partition(":")[0] in {"-crf", "-cq", "-global_quality", "-qp", "-qp_i", "-qp_p"} for option in argv
    )
    if encoder in {"hevc_nvenc", "hevc_amf"}:
        assert argv[argv.index("-rc:v:0") + 1] == ("vbr" if encoder == "hevc_nvenc" else "vbr_peak")


@pytest.mark.parametrize("mode", list(RateControlMode))
def test_missing_rate_input(mode: RateControlMode) -> None:
    probe = _probe().model_copy(update={"duration_s": 0.0, "size_bytes": 0})
    probe.streams[0].bit_rate = None
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265", rate=RateControl(mode=mode)))
    with pytest.raises(ValueError):
        render(plan, probe, SRC, DST)


def test_blank_language_kept() -> None:
    probe = _probe()
    probe.audio[1].language = ""
    plan = FFmpegPlan(streams=StreamPolicy(audio=StreamSelection(keep_languages=["eng", "und"])))
    result = render(plan, probe, SRC, DST)
    assert next(entry for entry in result.stream_log if entry.stream_index == 2).outcome == StreamOutcome.COPY


def test_encoder_order() -> None:
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265", encoder_priority=["libx265", "hevc_nvenc"]))
    selected = plan.select_encoder(["hevc_nvenc", "libx265"])
    assert selected.video is not None and selected.video.encoder == "libx265"
    with pytest.raises(ValueError):
        plan.select_encoder(["libx264"])


@pytest.mark.parametrize("suffix", ["", ":v", ":v:0"])
def test_nvenc_extras_win(suffix: str) -> None:
    extras = [f"-spatial_aq{suffix}", "0", f"-rc-lookahead{suffix}", "8", f"-profile{suffix}", "rext"]
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="hevc_nvenc", encoder_priority=["hevc_nvenc"], pixel_format="p010le", rate=RateControl(quality=24)
        ),
        extras=extras,
    )
    selected = plan.select_encoder(["hevc_nvenc"])
    assert selected.extras == extras
    argv = render(selected, _probe(), SRC, DST).argv
    assert argv[argv.index("-cq:v:0") + 1] == "24"
    for flag, value in zip(extras[::2], extras[1::2], strict=True):
        assert argv.count(flag) == 1 and argv[argv.index(flag) + 1] == value


def _probe() -> ProbeData:
    video = ProbeStream(index=0, codec="h264", codec_type="video", bit_rate=4_000_000, ten_bit=True)
    audio = [
        ProbeStream(index=1, codec="dts", codec_type="audio", language="eng", channels=6, bit_rate=1_536_000),
        ProbeStream(index=2, codec="ac3", codec_type="audio", language="und", channels=2, bit_rate=192_000),
        ProbeStream(index=3, codec="ac3", codec_type="audio", language="spa", channels=6, bit_rate=640_000),
        ProbeStream(
            index=4,
            codec="dts",
            codec_type="audio",
            language="jpn",
            channels=6,
            bit_rate=1_536_000,
            title="Director's Commentary",
        ),
    ]
    subtitle = ProbeStream(index=5, codec="subrip", codec_type="subtitle", language="eng", disposition_forced=True)
    return ProbeData(file_path=SRC, duration_s=7200.0, size_bytes=3_600_000_000, streams=[video, *audio, subtitle])


def _outcomes(result: RenderResult) -> list[tuple[int, StreamOutcome]]:
    return [(entry.stream_index, entry.outcome) for entry in result.stream_log]


@pytest.mark.parametrize("video", [None, VideoTarget(encoder="libx265")], ids=["remux", "encode"])
def test_stream_titles(video: VideoTarget | None) -> None:
    source = _probe()
    source.video[0].title = "Main picture"
    source.audio[0].title = "Main mix"
    plan = FFmpegPlan(
        video=video,
        streams=StreamPolicy(audio=StreamSelection(drop_descriptive=True, title_from_channel_layout=True)),
    )

    result = render(plan, source, SRC, DST)

    assert [(entry.stream_index, entry.title) for entry in result.stream_log] == [
        (0, "Main picture"),
        (1, "Main mix"),
        (2, None),
        (3, None),
        (4, "Director's Commentary"),
        (5, None),
    ]
    assert result.stream_log[1].outcome is StreamOutcome.RETAG
    assert result.stream_log[4].outcome is StreamOutcome.DROP


@pytest.mark.parametrize(
    "extras",
    [
        ["-x265-params", "csv=/operator/log.csv:stats=/operator/stats:aq-mode=3"],
        ["-vf", "drawtext=text='a;b|c':fontfile=/operator/font.ttf"],
    ],
)
def test_renders_operator_extras(extras: list[str]) -> None:
    result = render(FFmpegPlan(extras=extras), _probe(), SRC, DST)
    assert result.argv[-len(extras) - 1 :] == [*extras, DST]
    assert result.argv.count("-i") == 1


@pytest.mark.parametrize("option", ["-y", "-i", "-map", "-c", "-c:v:0", "-f"])
def test_rejects_generated_conflicts(option: str) -> None:
    with pytest.raises(ValueError):
        render(FFmpegPlan(extras=[option, "custom"]), _probe(), SRC, DST)


@pytest.mark.parametrize("option", ["-codec:v", "-vcodec", "-acodec", "-scodec"])
def test_rejects_codec_aliases(option: str) -> None:
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265"), extras=[option, "copy"])
    with pytest.raises(ValueError):
        render(plan, _probe(), SRC, DST)


@pytest.mark.parametrize(
    ("container", "source", "muxer"),
    [
        ("mkv", "mp4", "matroska"),
        ("mp4", "mkv", "mp4"),
        ("original", "mkv", "matroska"),
        ("original", "ts", "mpegts"),
        ("original", "m4v", "ipod"),
        ("original", "wmv", "asf"),
        ("original", "mpg", "mpeg"),
        ("original", "mp4", "mp4"),
        ("original", "avi", "avi"),
    ],
)
def test_explicit_output_muxer(container: str, source: str, muxer: str) -> None:
    probe = _probe()
    probe.container = source
    plan = FFmpegPlan(container=container)
    result = render(plan, probe, "/proc/self/fd/11", "/proc/self/fd/12")
    assert result.argv[-3:] == ["-f", muxer, "/proc/self/fd/12"]
    assert result.argv.count("-f") == 1


@pytest.mark.parametrize(
    ("plan", "option"),
    [
        (FFmpegPlan(video=VideoTarget(encoder="libx265", preset="slow")), "-preset:v:0"),
        (FFmpegPlan(video=VideoTarget(encoder="libx265", pixel_format="yuv420p")), "-pix_fmt:v"),
        (FFmpegPlan(video=VideoTarget(encoder="libx265", rate=RateControl(quality=23))), "-crf:v"),
        (
            FFmpegPlan(video=VideoTarget(encoder="libx265", rate=RateControl(mode=RateControlMode.BITRATE))),
            "-maxrate:v:0",
        ),
        (FFmpegPlan(video=VideoTarget(encoder="libx265", max_width=1280)), "-vf"),
        (FFmpegPlan(video=VideoTarget(encoder="libx265", max_width=1280)), "-filter:v:0"),
        (FFmpegPlan(video=VideoTarget(encoder="hevc_amf", rate=RateControl(quality=23))), "-qp_i:v:0"),
        (FFmpegPlan(video=VideoTarget(encoder="hevc_amf", rate=RateControl(quality=23))), "-qp_p:v:0"),
        (FFmpegPlan(generate_timestamps=True), "-fflags"),
        (FFmpegPlan(chapters=False), "-map_chapters"),
        (FFmpegPlan(streams=StreamPolicy(audio=StreamSelection(retag_und_as="eng"))), "-metadata:s:a:1"),
    ],
)
def test_conditional_extras_conflicts(plan: FFmpegPlan, option: str) -> None:
    extras = [option, "custom"]
    probe = _probe()
    probe.real_video[0].width = 1920
    assert render(FFmpegPlan(extras=extras), probe, SRC, DST).argv[-3:] == [*extras, DST]
    plan = plan.model_copy(update={"extras": extras})
    with pytest.raises(ValueError):
        render(plan, probe, SRC, DST)


def test_audio_cleanup_cancels() -> None:
    source = _probe()
    plan = FFmpegPlan(
        container="original",
        streams=StreamPolicy(audio=StreamSelection(keep_languages=["fre"], keep_all_if_empty=True)),
    )

    result = render(plan, source, SRC, DST)

    assert not any(entry.outcome == StreamOutcome.DROP for entry in result.stream_log)
    assert not any(arg.startswith("-0:a:") for arg in result.argv)


def test_container_stream_conformance() -> None:
    source = _probe()
    source.streams += [
        ProbeStream(index=6, codec="bin_data", codec_type="data"),
        ProbeStream(index=7, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
        ProbeStream(index=8, codec="mov_text", codec_type="subtitle", language="eng"),
    ]
    plan = FFmpegPlan(streams=StreamPolicy(drop_data=True, drop_cover_art=True))

    result = render(plan, source, SRC, DST)

    assert "-0:6" in result.argv and "-0:7" in result.argv
    assert result.argv[result.argv.index("-c:s:1") + 1] == "srt"
    outcomes = {entry.stream_index: entry.outcome for entry in result.stream_log}
    assert outcomes[6] == outcomes[7] == StreamOutcome.DROP
    assert outcomes[8] == StreamOutcome.TRANSCODE


@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.parametrize("encoder", ["libx265", "hevc_vaapi", "hevc_amf"])
@pytest.mark.parametrize("mode", list(RateControlMode))
def test_artwork_encode_scoping(drop: bool, encoder: str, mode: RateControlMode) -> None:
    source = _probe()
    source.video[0].width = 3840
    source.streams = [
        ProbeStream(index=7, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
        *source.streams,
        ProbeStream(index=8, codec="h264", codec_type="video", width=1920),
        ProbeStream(index=9, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
    ]
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder=encoder,
            pixel_format="p010le",
            preset="slow",
            max_width=1280,
            rate=RateControl(mode=mode, quality=21, quality_option="qp" if encoder.endswith("_vaapi") else "crf"),
        ),
        streams=StreamPolicy(drop_cover_art=drop),
    )
    result = render(plan, source, SRC, DST)
    start = result.argv.index(f"-c:v:{0 if drop else 1}")
    options = dict(zip(result.argv[start:-1:2], result.argv[start + 1 : -1 : 2], strict=True))
    assert options.pop("-f") == "matroska"
    indices = [0, 1] if drop else [1, 2]
    rate_options = (
        ["-b", "-minrate", "-maxrate", "-bufsize"]
        if mode == RateControlMode.BITRATE
        else ["-qp_i", "-qp_p"]
        if encoder.endswith("_amf")
        else ["-qp"]
        if encoder.endswith("_vaapi")
        else ["-crf"]
    )
    assert set(options) == {
        f"{option}:v:{n}"
        for n in indices
        for option in (
            ["-c", "-preset", "-filter"]
            + ([] if encoder.endswith("_vaapi") else ["-pix_fmt"])
            + (["-rc"] if encoder.endswith("_amf") else [])
            + rate_options
        )
    }
    for n in indices:
        assert options[f"-c:v:{n}"] == encoder
        if mode == RateControlMode.BITRATE:
            assert options[f"-b:v:{n}"] == "4000k"
        assert options[f"-filter:v:{n}"].startswith("scale=1280:-2:")
    outcomes = {entry.stream_index: entry.outcome for entry in result.stream_log}
    assert outcomes[7] == outcomes[9] == (StreamOutcome.DROP if drop else StreamOutcome.COPY)
    assert outcomes[0] == outcomes[8] == StreamOutcome.TRANSCODE


@pytest.mark.parametrize("encoder", ["libx265", "hevc_vaapi"])
@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.parametrize("large_first", [False, True])
def test_multivideo_resize_scoping(encoder: str, drop: bool, large_first: bool) -> None:
    source = _probe()
    widths = (3840, 1920) if large_first else (1920, 3840)
    source.video[0].width = widths[0]
    source.streams = [
        ProbeStream(index=7, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
        *source.streams,
        ProbeStream(index=8, codec="h264", codec_type="video", width=widths[1]),
    ]
    plan = FFmpegPlan(
        video=VideoTarget(encoder=encoder, max_width=1920),
        streams=StreamPolicy(drop_cover_art=drop),
        burn_subtitle=SubtitleBurn(filename="captions.srt"),
    )
    result = render(plan, source, SRC, DST, subtitle_path="captions.srt")
    offset = 0 if drop else 1
    for n, width in enumerate(widths):
        filters = result.argv[result.argv.index(f"-filter:v:{offset + n}") + 1]
        expected = ["scale=1920:-2:force_original_aspect_ratio=decrease:force_divisible_by=2"] if width > 1920 else []
        expected += ["subtitles=filename=captions.srt"]
        if encoder.endswith("_vaapi"):
            expected += ["format=nv12", "hwupload"]
        assert filters == ",".join(expected)
    outcomes = {entry.stream_index: entry.outcome for entry in result.stream_log}
    assert outcomes[7] == (StreamOutcome.DROP if drop else StreamOutcome.COPY)


def test_timestamp_input_option() -> None:
    result = render(FFmpegPlan(generate_timestamps=True), _probe(), SRC, DST)

    assert result.argv[result.argv.index("-fflags") + 1] == "+genpts"
    assert result.argv.index("-fflags") < result.argv.index("-i")


def test_container_bitrate_rounding() -> None:
    source = _probe()
    source.raw = {"format": {"bit_rate": "3001500"}}
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="libx265",
            rate=RateControl(mode=RateControlMode.BITRATE, from_source_bitrate=0.5, bitrate_source="container"),
        )
    )

    argv = render(plan, source, SRC, DST).argv

    assert argv[argv.index("-b:v:0") + 1] == "1501k"


def test_remux_cleanup_argv() -> None:
    plan = FFmpegPlan(
        streams=StreamPolicy(
            audio=StreamSelection(
                keep_languages=["eng", "jpn", "und"],
                drop_descriptive=True,
                retag_und_as="eng",
                title_from_channel_layout=True,
            ),
            subtitle=StreamSelection(keep_languages=["eng"]),
        ),
    )
    result = render(plan, _probe(), SRC, DST)

    assert result.argv == [
        "-y",
        "-hide_banner",
        "-i",
        SRC,
        "-map",
        "0",
        "-c",
        "copy",
        "-map",
        "-0:a:2",
        "-map",
        "-0:a:3",
        "-metadata:s:a:0",
        "title=5.1",
        "-metadata:s:a:1",
        "language=eng",
        "-metadata:s:a:1",
        "title=2.0",
        "-f",
        "matroska",
        DST,
    ]
    outcomes = _outcomes(result)
    assert (1, StreamOutcome.RETAG) in outcomes
    assert (2, StreamOutcome.RETAG) in outcomes
    assert (3, StreamOutcome.DROP) in outcomes
    assert (4, StreamOutcome.DROP) in outcomes
    assert (5, StreamOutcome.COPY) in outcomes


def test_chapters_option() -> None:
    assert "-map_chapters" not in render(FFmpegPlan(), _probe(), SRC, DST).argv

    argv = render(FFmpegPlan(chapters=False), _probe(), SRC, DST).argv
    assert argv[argv.index("-map_chapters") : argv.index("-map_chapters") + 2] == ["-map_chapters", "-1"]


@pytest.mark.parametrize("drop", [False, True])
def test_audio_default_flags(drop: bool) -> None:
    source = ProbeData(
        file_path=SRC,
        duration_s=60,
        size_bytes=100,
        streams=[
            ProbeStream(index=1, codec="aac", codec_type="audio", language="spa"),
            ProbeStream(index=2, codec="aac", codec_type="audio", language="jpn", disposition_default=True),
            ProbeStream(
                index=3,
                codec="aac",
                codec_type="audio",
                language="eng",
                disposition_commentary=True,
                disposition_forced=True,
            ),
            ProbeStream(index=4, codec="aac", codec_type="audio", language="eng", disposition_default=True),
        ],
    )
    selection = StreamSelection(set_default_language="eng", keep_languages=["jpn", "eng"] if drop else None)
    argv = render(FFmpegPlan(streams=StreamPolicy(audio=selection)), source, SRC, DST).argv
    dispositions = {arg: argv[n + 1] for n, arg in enumerate(argv) if arg.startswith("-disposition:a:")}
    assert dispositions == (
        {"-disposition:a:0": "-default", "-disposition:a:1": "+default", "-disposition:a:2": "-default"}
        if drop
        else {
            "-disposition:a:0": "-default",
            "-disposition:a:1": "-default",
            "-disposition:a:2": "+default",
            "-disposition:a:3": "-default",
        }
    )


@pytest.mark.parametrize("language", ["fre", "spa"])
def test_audio_default_absent(language: str) -> None:
    source = _probe()
    source.audio[0].disposition_default = True
    selection = StreamSelection(set_default_language=language, keep_languages=["eng", "und"])
    plan = FFmpegPlan(streams=StreamPolicy(audio=selection))
    unchanged = FFmpegPlan(streams=StreamPolicy(audio=StreamSelection(keep_languages=["eng", "und"])))
    assert render(plan, source, SRC, DST).argv == render(unchanged, source, SRC, DST).argv


@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.parametrize("default", ["jpn", "fre"])
def test_retagged_audio_default(drop: bool, default: str) -> None:
    source = _probe()
    source.audio[0].disposition_default = True
    selection = StreamSelection(
        keep_languages=["eng", "und", "jpn"] if drop else None,
        retag_und_as="jpn",
        set_default_language=default,
    )
    plan = FFmpegPlan(streams=StreamPolicy(audio=selection))
    argv = render(plan, source, SRC, DST).argv
    dispositions = {arg: argv[n + 1] for n, arg in enumerate(argv) if arg.startswith("-disposition:a:")}
    assert dispositions == (
        {f"-disposition:a:{n}": "+default" if n == 1 else "-default" for n in range(3 if drop else 4)}
        if default == "jpn"
        else {}
    )
    assert "language=jpn" in argv
    assert changes_media(plan, source)


def test_retag_default_remap() -> None:
    source = _probe()
    source.audio[0].disposition_default = True
    plan = FFmpegPlan(
        streams=StreamPolicy(
            audio=StreamSelection(keep_languages=["und", "jpn"], retag_und_as="jpn", set_default_language="jpn")
        )
    )
    argv = render(plan, source, SRC, DST).argv
    assert "-0:a:0" in argv
    assert argv[argv.index("-disposition:a:0") + 1] == "+default"
    assert argv[argv.index("-disposition:a:1") + 1] == "-default"
    assert changes_media(plan, source)


def test_hevc_encode_argv() -> None:
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="libx265",
            rate=RateControl(quality=19),
            pixel_format="p010le",
        ),
    )
    result = render(plan, _probe(), SRC, DST)

    assert result.argv == [
        "-y",
        "-hide_banner",
        "-i",
        SRC,
        "-map",
        "0",
        "-c",
        "copy",
        "-c:v:0",
        "libx265",
        "-pix_fmt:v:0",
        "p010le",
        "-crf:v:0",
        "19",
        "-f",
        "matroska",
        DST,
    ]
    video_entry = result.stream_log[0]
    assert (video_entry.stream_index, video_entry.outcome) == (0, StreamOutcome.TRANSCODE)
    assert all(entry.outcome is StreamOutcome.COPY for entry in result.stream_log[1:])


def test_no_language_filter() -> None:
    plan = FFmpegPlan(
        streams=StreamPolicy(
            audio=StreamSelection(drop_descriptive=True),
            subtitle=StreamSelection(keep_languages=[]),
        ),
    )
    result = render(plan, _probe(), SRC, DST)

    assert "-0:a:3" in result.argv
    assert not any(arg.startswith("-0:s:") for arg in result.argv)
    outcomes = _outcomes(result)
    assert (3, StreamOutcome.COPY) in outcomes
    assert (4, StreamOutcome.DROP) in outcomes
    assert (5, StreamOutcome.COPY) in outcomes


def test_last_audio_survives() -> None:
    plan = FFmpegPlan(streams=StreamPolicy(audio=StreamSelection(keep_languages=["fre"])))
    result = render(plan, _probe(), SRC, DST)

    assert result.argv == [
        "-y",
        "-hide_banner",
        "-i",
        SRC,
        "-map",
        "0",
        "-c",
        "copy",
        "-map",
        "-0:a:1",
        "-map",
        "-0:a:2",
        "-map",
        "-0:a:3",
        "-f",
        "matroska",
        DST,
    ]
    first_audio = result.stream_log[1]
    assert (first_audio.stream_index, first_audio.outcome) == (1, StreamOutcome.COPY)
    assert [entry.outcome for entry in result.stream_log[2:5]] == [
        StreamOutcome.DROP,
        StreamOutcome.DROP,
        StreamOutcome.DROP,
    ]


def test_quality_without_bitrate() -> None:
    probe = _probe().model_copy(update={"duration_s": 0.0, "size_bytes": 0})
    probe.streams[0].bit_rate = None
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265", rate=RateControl(quality=20)))

    argv = render(plan, probe, SRC, DST).argv

    assert argv[argv.index("-crf:v:0") + 1] == "20"
    assert "-b:v:0" not in argv


def test_scales_large_video() -> None:
    probe = _probe()
    probe.streams[0].width = 3840
    probe.streams[0].height = 2160
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265", max_width=1920, max_height=1080))

    argv = render(plan, probe, SRC, DST).argv

    assert (
        argv[argv.index("-filter:v:0") + 1]
        == "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def test_targets_output_audio() -> None:
    plan = FFmpegPlan(
        streams=StreamPolicy(audio=StreamSelection(keep_languages=["und", "jpn"], retag_und_as="eng")),
        audio_encode=[AudioTarget(codec="aac", bitrate="192k", when_codec_in=["ac3"])],
    )

    argv = render(plan, _probe(), SRC, DST).argv

    assert ["-c:a:0", "aac", "-b:a:0", "192k"] == argv[argv.index("-c:a:0") : argv.index("-c:a:0") + 4]
    assert "-c:a:1" not in argv
    assert ["-metadata:s:a:0", "language=eng"] == argv[
        argv.index("-metadata:s:a:0") : argv.index("-metadata:s:a:0") + 2
    ]


@pytest.mark.parametrize(
    ("source_container", "container", "encode_aac", "filtered"),
    [
        ("ts", "mkv", False, True),
        ("ts", "mp4", False, True),
        ("ts", "original", False, False),
        ("mkv", "mkv", False, False),
        ("ts", "mkv", True, False),
    ],
)
def test_copied_aac_filter(source_container: str, container: str, encode_aac: bool, filtered: bool) -> None:
    probe = ProbeData(
        file_path=SRC,
        container=source_container,
        duration_s=60,
        size_bytes=100,
        streams=[
            ProbeStream(index=1, codec="aac", codec_type="audio", language="spa"),
            ProbeStream(index=2, codec="aac", codec_type="audio", language="eng"),
            ProbeStream(index=3, codec="ac3", codec_type="audio", language="eng"),
            ProbeStream(index=4, codec="aac", codec_type="audio", language="eng"),
        ],
    )
    plan = FFmpegPlan(
        container=container,
        streams=StreamPolicy(audio=StreamSelection(keep_languages=["eng"])),
        audio_encode=[AudioTarget(codec="aac", bitrate="192k", when_codec_in=["aac" if encode_aac else "ac3"])],
    )

    result = render(plan, probe, SRC, DST)

    filters = {arg: result.argv[n + 1] for n, arg in enumerate(result.argv) if arg.startswith("-bsf")}
    assert filters == ({"-bsf:a:0": "aac_adtstoasc", "-bsf:a:2": "aac_adtstoasc"} if filtered else {})
    assert result.stream_log[0].outcome == StreamOutcome.DROP
    assert [entry.outcome for entry in result.stream_log[1:]] == (
        [StreamOutcome.TRANSCODE, StreamOutcome.COPY, StreamOutcome.TRANSCODE]
        if encode_aac
        else [StreamOutcome.COPY, StreamOutcome.TRANSCODE, StreamOutcome.COPY]
    )
    if filtered:
        with pytest.raises(ValueError):
            render(plan.model_copy(update={"extras": ["-bsf:a", "null"]}), probe, SRC, DST)


def test_webvtt_conversion() -> None:
    probe = ProbeData(
        file_path="captions.vtt",
        container="vtt",
        duration_s=10,
        size_bytes=100,
        streams=[ProbeStream(index=0, codec="webvtt", codec_type="subtitle", language="eng")],
    )
    rendered = render(FFmpegPlan(container="mkv"), probe, SRC, DST)
    assert rendered.argv[rendered.argv.index("-c:s:0") + 1] == "srt"
    assert rendered.stream_log[0].outcome == StreamOutcome.TRANSCODE
    argv = render_subtitle(SRC, DST, stream_index=0, format="srt")
    assert argv[argv.index("-c:s") + 1] == "srt"
    assert argv[argv.index("-f") + 1] == "srt"


def test_burn_subtitle_argv() -> None:
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265"), burn_subtitle=SubtitleBurn(filename="captions.srt"))

    result = render(plan, _probe(), SRC, DST, subtitle_path="/tmp/captions.srt")

    assert result.argv[result.argv.index("-filter:v:0") : result.argv.index("-filter:v:0") + 2] == [
        "-filter:v:0",
        "subtitles=filename=/tmp/captions.srt",
    ]


def test_burn_composes_scale() -> None:
    plan = FFmpegPlan(
        video=VideoTarget(encoder="libx265", max_width=1280),
        burn_subtitle=SubtitleBurn(filename="captions.ass"),
    )
    probe = _probe()
    probe.video[0].width = 1920

    argv = render(plan, probe, SRC, DST, subtitle_path="/tmp/captions.ass").argv

    assert argv[argv.index("-filter:v:0") + 1] == (
        "scale=1280:-2:force_original_aspect_ratio=decrease:force_divisible_by=2,subtitles=filename=/tmp/captions.ass"
    )


def test_burn_escapes_filter_path() -> None:
    plan = FFmpegPlan(video=VideoTarget(encoder="libx265"), burn_subtitle=SubtitleBurn(filename="captions.vtt"))

    argv = render(plan, _probe(), SRC, DST, subtitle_path="C:\\a,b[c]'d\\e.vtt").argv

    assert argv[argv.index("-filter:v:0") + 1] == "subtitles=filename=C\\\\:\\\\\\\\a\\,b\\[c\\]\\\\\\'d\\\\\\\\e.vtt"


def test_burn_requires_video_and_path() -> None:
    with pytest.raises(ValueError):
        FFmpegPlan(burn_subtitle=SubtitleBurn(filename="captions.srt"))

    plan = FFmpegPlan(video=VideoTarget(encoder="libx265"), burn_subtitle=SubtitleBurn(filename="captions.srt"))
    with pytest.raises(ValueError):
        render(plan, _probe(), SRC, DST)


def test_subtitle_filename_validation() -> None:
    for filename in (".", "..", "a/b.srt", "a\\b.srt", "a.srt\x00", "captions.txt"):
        with pytest.raises(ValueError):
            SubtitleBurn(filename=filename)
