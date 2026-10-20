from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.media.probe import ProbeData, probe_from_raw
from common.facts import builtin_facts
from common.plan import FFmpegPlan, StreamPolicy, StreamSelection, render
from common.plan.renderer import changes_media
from common.enums import StreamOutcome
from server.scanner.normalize import normalize

SIZE = 2048


def stream(**overrides: object) -> dict:
    raw: dict = {
        "index": 0,
        "codec_name": "hevc",
        "codec_type": "video",
        "profile": "Main 10",
        "pix_fmt": "yuv420p10le",
        "avg_frame_rate": "30000/1001",
        "bit_rate": "5000000",
        "disposition": {"default": 1},
        "tags": {},
    }
    raw.update(overrides)
    return raw


def audio(**overrides: object) -> dict:
    raw: dict = {
        "index": 1,
        "codec_name": "ac3",
        "codec_type": "audio",
        "sample_rate": "48000",
        "channels": 6,
        "channel_layout": "5.1",
        "disposition": {"forced": 1},
        "tags": {"language": "eng"},
    }
    raw.update(overrides)
    return raw


def build(streams: list[dict]) -> ProbeData:
    return probe_from_raw(Path("/lib/show.mkv"), SIZE, {"streams": streams, "format": {"duration": "60"}})


@pytest.mark.parametrize(
    ("stored", "tag", "title", "keep"),
    [
        (False, "jap", "Japanese", "jpn"),
        (True, "jap", "Japanese", "jpn"),
        (False, "ja", "English", "jpn"),
        (False, "en", None, "eng"),
        (False, "fre", None, "fra"),
        (False, "fra", None, "fre"),
    ],
)
def test_language_selection(stored: bool, tag: str, title: str | None, keep: str) -> None:
    raw_streams: list[dict] = [
        {"index": 0, "codec_name": "hevc", "codec_type": "video"},
        {"index": 1, "codec_name": "aac", "codec_type": "audio", "tags": {"language": "eng"}},
        {"index": 2, "codec_name": "aac", "codec_type": "audio", "tags": {"language": tag, "title": title}},
        {"index": 3, "codec_name": "ass", "codec_type": "subtitle", "tags": {"language": "eng"}},
        {"index": 4, "codec_name": "ass", "codec_type": "subtitle", "tags": {"language": tag, "title": title}},
    ]
    if stored:
        probe = ProbeData.model_validate(
            {
                "file_path": "/lib/show.mkv",
                "duration_s": 60,
                "size_bytes": SIZE,
                "container": "mkv",
                "streams": [
                    {
                        "index": entry["index"],
                        "codec": entry["codec_name"],
                        "codec_type": entry["codec_type"],
                        **entry.get("tags", {}),
                    }
                    for entry in raw_streams
                ],
            }
        )
    else:
        probe = build(raw_streams)
    plan = FFmpegPlan(
        streams=StreamPolicy(
            audio=StreamSelection(keep_languages=["eng", keep]),
            subtitle=StreamSelection(keep_languages=["eng", keep]),
        )
    )

    result = render(plan, probe, probe.file_path, "/cache/output.mkv")

    assert {entry.stream_index: entry.outcome for entry in result.stream_log} == {
        index: StreamOutcome.COPY for index in range(5)
    }
    assert [result.argv[n + 1] for n, arg in enumerate(result.argv) if arg == "-map"] == ["0"]
    assert not changes_media(plan, probe)


def test_language_title_exact() -> None:
    probe = build(
        [
            audio(tags={"language": "eng"}, disposition={}),
            audio(index=2, tags={"language": "jap", "title": "Japanese / English"}, disposition={}),
        ]
    )
    plan = FFmpegPlan(streams=StreamPolicy(audio=StreamSelection(keep_languages=["eng", "jpn"])))

    result = render(plan, probe, probe.file_path, "/cache/output.mkv")

    assert {entry.stream_index: entry.outcome for entry in result.stream_log} == {
        1: StreamOutcome.COPY,
        2: StreamOutcome.DROP,
    }


def test_fps_variants() -> None:
    assert build([stream()]).video[0].fps == pytest.approx(29.97, abs=1e-3)
    assert build([stream(avg_frame_rate="25/1")]).video[0].fps == 25.0
    assert build([stream(avg_frame_rate="24000/1001")]).video[0].fps == pytest.approx(23.976, abs=1e-3)
    assert build([stream(avg_frame_rate="0/0")]).video[0].fps is None
    assert build([stream(avg_frame_rate="junk")]).video[0].fps is None
    no_rate = stream()
    del no_rate["avg_frame_rate"]
    assert build([no_rate]).video[0].fps is None


@pytest.mark.parametrize(
    ("pix_fmt", "depth"),
    [
        ("nv12", 8),
        ("nv16", 8),
        ("nv21", 8),
        ("nv24", 8),
        ("nv42", 8),
        ("yuv420p", 8),
        ("yuv420p10le", 10),
        ("yuv420p12le", 12),
        ("p010le", 10),
        ("p016le", 16),
        ("rgb48be", 16),
        ("bgr24", 8),
    ],
)
def test_pixel_component_depth(pix_fmt: str, depth: int) -> None:
    probe = build([stream(pix_fmt=pix_fmt)])
    assert probe.video[0].bit_depth == depth
    assert probe.video[0].ten_bit is (depth >= 10)
    assert builtin_facts(probe)["media.video.bit_depth"] == depth


@pytest.mark.parametrize("bits", ["10", "0", "-1"])
def test_explicit_depth_precedence(bits: str) -> None:
    video = build([stream(pix_fmt="nv12", bits_per_raw_sample=bits)]).video[0]
    assert video.bit_depth == (10 if bits == "10" else 8)
    assert video.ten_bit is (bits == "10")


@pytest.mark.parametrize("tag", ["BPS", "BPS-eng"])
def test_bps_fallback(tag: str) -> None:
    assert build([stream(bit_rate=None, tags={tag: "3854774"})]).video[0].bit_rate == 3_854_774


def test_bitrate_precedes_bps() -> None:
    assert build([stream(tags={"BPS": "3854774"})]).video[0].bit_rate == 5_000_000


def test_invalid_bps() -> None:
    assert build([stream(bit_rate=None, tags={"BPS": "0"})]).video[0].bit_rate is None


@pytest.mark.parametrize(
    ("filename", "kind", "codec"),
    [("font.ttf", "attachment", "ttf"), ("font.otf", "attachment", "otf"), ("cover.jpg", "video", "mjpeg")],
)
def test_attachment_types(filename: str, kind: str, codec: str) -> None:
    probe = build(
        [
            stream(
                codec_name="mjpeg",
                tags={"FILENAME": filename, "MIMETYPE": "image/jpeg"},
                disposition={"attached_pic": 1},
            )
        ]
    )
    attachment = probe.streams[0]
    assert (attachment.codec_type, attachment.codec) == (kind, codec)
    assert attachment.filename == filename and attachment.mimetype == "image/jpeg"
    assert attachment.disposition_attached_pic == (kind == "video")


@pytest.mark.parametrize("duration", ["nan", "inf", "-inf"])
def test_rejects_nonfinite_duration(tmp_path: Path, duration: str) -> None:
    path = tmp_path / "source.mkv"
    with pytest.raises(ValidationError):
        probe_from_raw(path, SIZE, {"streams": [stream()], "format": {"duration": duration}})
    with pytest.raises(ValidationError):
        ProbeData(file_path=str(path), duration_s=float(duration), size_bytes=SIZE, streams=[])


@pytest.mark.parametrize("attached", [False, True])
def test_mjpeg_disposition(tmp_path: Path, attached: bool) -> None:
    path = tmp_path / "camera.avi"
    path.write_bytes(b"recording")
    raw = {
        "format": {"duration": "60"},
        "streams": [
            stream(
                codec_name="mjpeg",
                avg_frame_rate="30/1",
                width=1920,
                height=1080,
                disposition={"attached_pic": int(attached)},
            )
        ],
    }
    media = normalize(path, path.stat(), raw)
    assert media.medium == ("other" if attached else "video")
    assert media.video_codec == (None if attached else "mjpeg")
    assert builtin_facts(media.probe)["media.video.count"] == (0 if attached else 1)
    assert media.probe.video[0].fps == 30
    rendered = render(FFmpegPlan(streams=StreamPolicy(drop_cover_art=True)), media.probe, str(path), "out.mkv")
    assert ("-0:0" in rendered.argv) == attached


@pytest.mark.parametrize("tag", ["mimetype", "MIMETYPE", "MimeType"])
def test_remuxed_artwork_classified(tag: str) -> None:
    probe = build(
        [
            stream(),
            stream(
                index=1,
                codec_name="mjpeg",
                width=351,
                height=500,
                disposition={"attached_pic": 0},
                tags={"filename": "cover.jpg", tag: "image/jpeg"},
            ),
        ]
    )
    assert probe.video[1].disposition_attached_pic
    assert probe.real_video == [probe.video[0]]
    assert builtin_facts(probe)["media.video.count"] == 1
    assert probe.raw is not None and probe.raw["streams"][1]["disposition"]["attached_pic"] == 0
    rendered = render(FFmpegPlan(streams=StreamPolicy(drop_cover_art=True)), probe, "source.mkv", "output.mkv")
    assert "-0:1" in rendered.argv


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("MW_TEST_REAL_FFMPEG") != "1", reason="Set MW_TEST_REAL_FFMPEG=1 to opt in.")
@pytest.mark.asyncio
async def test_real_artwork_remux(tmp_path: Path) -> None:
    from common.capabilities import binary_identity
    from common.plan.models import VerificationSpec
    from node.executor import JobSpec, execute
    from server.finalization import validate_artifact

    ffmpeg = binary_identity("ffmpeg")[0]
    ffprobe = binary_identity("ffprobe")[0]
    cover, source, output = (tmp_path / name for name in ("cover.jpg", "source.mkv", "output.mkv"))
    # These lavfi inputs generate disposable fixtures, not library media.
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=32x32:d=0.04",
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-pix_fmt",
            "yuvj420p",
            str(cover),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:d=1",
            "-c:v",
            "ffv1",
            "-attach",
            str(cover),
            "-metadata:s:t:0",
            "mimetype=image/jpeg",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    raw = json.loads(
        subprocess.check_output(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
            text=True,
            timeout=10,
        )
    )
    probe = probe_from_raw(source, source.stat().st_size, raw)
    assert probe.video[1].disposition_attached_pic
    plan = FFmpegPlan(expected=VerificationSpec(size_ceiling_ratio=5))
    job = JobSpec(
        job_id="artwork",
        attempt_id=1,
        src=str(source),
        dst=str(output),
        log=str(tmp_path / "ffmpeg.log"),
        plan=plan,
        probe=probe,
        min_free_gb=0,
        hard_size_ceiling=5,
    )
    result = await asyncio.wait_for(execute(job, ffmpeg, ffprobe), 10)
    assert result.status == "succeeded", result.error
    assert result.output_probe is not None and result.output_probe.raw is not None
    assert len(result.output_probe.real_video) == 1
    assert result.output_probe.video[1].disposition_attached_pic
    assert result.output_probe.raw["streams"][1]["disposition"]["attached_pic"] == 0
    verified = validate_artifact(result.output_probe.raw, output, probe, plan)
    assert len(verified.real_video) == 1 and verified.video[1].disposition_attached_pic
