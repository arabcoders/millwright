from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import httpx
from pydantic import ValidationError

from common.media.probe import ProbeData, ProbeStream, probe_from_raw
from common.plan import FFmpegPlan, render
from common.enums import RateControlMode, StreamOutcome
from server.api import app as app_module
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.core.log_history import install_log_file, query_logs
from server.plugins.host import EvaluatedDecision, PluginHost
from server.plugins.manifest import load_manifest
from tests.helpers import database_context, make_test_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_PLUGINS = REPO_ROOT / "plugins"
FAKE_PLUGINS = Path(__file__).parent / "fakes" / "plugins"

ECHO_Y_SOURCE = """from __future__ import annotations

from typing import Any

from common.ctx import PluginContext


def decide(_ctx: PluginContext, _inputs: dict[str, Any]) -> dict[str, Any]:
    return {"type": "match", "facets": {"mode": "y"}}
"""


def stream(index: int, codec: str, codec_type: str, language: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"index": index, "codec": codec, "codec_type": codec_type}
    if language is not None:
        data["language"] = language
    return data


def video(index: int = 0, codec: str = "h264") -> dict[str, Any]:
    return stream(index, codec, "video")


def audio(index: int = 1, codec: str = "ac3", language: str | None = "eng") -> dict[str, Any]:
    return stream(index, codec, "audio", language)


def ctx_dict(
    *,
    container: str = "mp4",
    size_bytes: int = 4 * 1024**3,
    streams: list[dict[str, Any]],
    sidecar_subtitles: list[str] | None = None,
) -> dict[str, Any]:
    probe = ProbeData(
        file_path="/media/fixture.mp4",
        container=container,
        duration_s=3000.0,
        size_bytes=size_bytes,
        streams=[ProbeStream(**data) for data in streams],
    )
    return {
        "probe": probe.model_dump(mode="json"),
        "original_filename": "fixture.mp4",
        "sidecar_subtitles": sidecar_subtitles or [],
    }


def stage_all(root: Path, *names: str) -> None:
    for name in names:
        shutil.copytree(FAKE_PLUGINS / name, root / name)


@pytest.mark.asyncio
async def test_subtitle_ambiguity_error() -> None:
    async with host_for([STARTER_PLUGINS]) as host:
        decision = await evaluated(
            host,
            "transcode.burn_subtitles",
            streams=[video()],
            inputs={"on_multiple": "error"},
            sidecar_subtitles=["fixture.eng.srt", "fixture.srt"],
        )

        assert decision.type == "error"
        assert decision.error is not None
        assert next(info for info in host.plugins() if info.manifest.id == "transcode.burn_subtitles").state == "ready"


async def start_runner(plugin_dir: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "server.plugins.runner",
        str(plugin_dir),
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def roundtrip(proc: asyncio.subprocess.Process, payload: dict[str, Any]) -> dict[str, Any]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload).encode() + b"\n")
    await proc.stdin.drain()
    return json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))


async def stop_runner(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        finally:
            if proc.returncode is None:
                proc.kill()


@pytest.mark.asyncio
@pytest.mark.parametrize("describe", [False, True], ids=["decide", "describe"])
async def test_cancelled_runner_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, describe: bool) -> None:
    stage_all(tmp_path, "filter.echo")
    (tmp_path / "filter.echo" / "plugin.py").write_text(
        'def decide(ctx, inputs):\n    return {"type": "match", "facets": {"mode": inputs["mode"]}}\n',
        encoding="utf-8",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    previous: asyncio.subprocess.Process | None = None
    original = PluginHost._roundtrip
    first = True

    async def roundtrip(proc: asyncio.subprocess.Process, request: dict[str, Any]) -> bytes:
        nonlocal previous, first
        if first:
            first = False
            previous = proc
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(request) + "\n").encode())
            await proc.stdin.drain()
            started.set()
            await release.wait()
        return await original(proc, request)

    monkeypatch.setattr(PluginHost, "_roundtrip", staticmethod(roundtrip))
    async with host_for([tmp_path]) as host:
        task = asyncio.create_task(
            host.describe_inputs("filter.echo")
            if describe
            else host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "old"})
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            assert previous is not None and previous.returncode is not None
            assert host._plugins["filter.echo"].runner is None
            if describe:
                fields = await asyncio.wait_for(host.describe_inputs("filter.echo"), 5)
                assert isinstance(fields, list)
            decision = await asyncio.wait_for(
                host.evaluate("filter.echo", ctx_dict(streams=[video()]), {"mode": "new"}), 5
            )
            assert decision.type == "match"
            assert decision.facets == {"mode": "new"}
        finally:
            release.set()
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)


@contextlib.asynccontextmanager
async def host_for(roots: list[Path], **kwargs: Any) -> AsyncIterator[PluginHost]:
    host = PluginHost(roots, **kwargs)
    try:
        yield host
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_installed_plugins_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prefix = tmp_path / "prefix"
    bundled = prefix / "share/millwright/plugins"
    shutil.copytree(STARTER_PLUGINS, bundled)
    async with database_context("installed-plugins", tables=("flows", "plugin_states")) as factory:
        with monkeypatch.context() as context:
            context.setattr(app_module, "_REPO_ROOT", tmp_path / "installed")
            context.setattr(app_module.sys, "prefix", str(prefix))
            app = create_app(factory, config=ServerConfig())
        host = app.state.plugin_host
        try:
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/plugins")
                assert response.status_code == 200
                plugins = response.json()
                assert {item["manifest"]["id"] for item in plugins} == {
                    load_manifest(path.parent).id for path in bundled.glob("*/plugin.toml")
                }
                assert all(item["source"] == "bundled" for item in plugins)
                presets = (await client.get("/api/flows/presets")).json()
                assert presets
                for preset in presets:
                    response = await client.put(f"/api/flows/{preset['id']}", json={"definition": preset["definition"]})
                    assert response.status_code == 200, response.text
        finally:
            await asyncio.wait_for(host.aclose(), 10)


async def evaluated(
    host: PluginHost,
    plugin_id: str,
    *,
    container: str = "mp4",
    size_bytes: int = 4 * 1024**3,
    streams: list[dict[str, Any]],
    inputs: dict[str, Any] | None = None,
    sidecar_subtitles: list[str] | None = None,
) -> EvaluatedDecision:
    return await host.evaluate(
        plugin_id,
        ctx_dict(
            container=container,
            size_bytes=size_bytes,
            streams=streams,
            sidecar_subtitles=sidecar_subtitles,
        ),
        inputs or {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_id", ["transcode.remux", "transcode.video"])
@pytest.mark.parametrize(
    ("container", "removed", "inputs"),
    [
        (
            "mp4",
            audio(index=2, language="fre"),
            {"audio_keep_languages": ["eng"]},
        ),
        (
            "mkv",
            stream(3, "subrip", "subtitle", "ger"),
            {"subtitle_keep_languages": ["eng"]},
        ),
    ],
    ids=["audio", "german-subtitle"],
)
async def test_cleanup_preserves_container(
    plugin_id: str, container: str, removed: dict[str, Any], inputs: dict[str, Any]
) -> None:
    subtitle_codec = "subrip" if container == "mkv" else "mov_text"
    streams = [
        video(),
        audio(),
        {**stream(4, subtitle_codec, "subtitle", "eng"), "disposition_forced": True},
        removed,
    ]
    ctx = ctx_dict(container=container, streams=streams)
    output_container = "original" if plugin_id == "transcode.remux" else container
    inputs = {"target": "avc", **inputs} if plugin_id == "transcode.video" else inputs
    async with host_for([STARTER_PLUGINS]) as host:
        decision = await host.evaluate(plugin_id, ctx, {"container": output_container, **inputs})

    assert decision.type == "plan"
    assert decision.plan is not None
    plan = FFmpegPlan.model_validate(decision.plan)
    assert plan.container == output_container
    assert plan.video is None
    assert plan.generate_timestamps == (plugin_id == "transcode.remux")
    rendered = render(plan, ProbeData.model_validate(ctx["probe"]), "source.mkv", "output.mkv")
    selector = "a" if removed["codec_type"] == "audio" else "s"
    assert [rendered.argv[n + 1] for n, arg in enumerate(rendered.argv) if arg == "-map"] == ["0", f"-0:{selector}:1"]
    assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.DROP} == {
        removed["index"]
    }
    assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.COPY} == {0, 1, 4}


def test_retag_stream_metadata() -> None:
    probe = ProbeData.model_validate(
        ctx_dict(
            container="mkv",
            streams=[
                video(),
                audio(language="spa"),
                {**audio(index=4, language=None), "channels": 2},
                audio(index=9),
            ],
        )["probe"]
    )
    plan = FFmpegPlan.model_validate(
        {
            "container": "original",
            "streams": {"audio": {"keep_languages": ["eng"], "retag_und_as": "eng", "title_from_channel_layout": True}},
        }
    )
    rendered = render(plan, probe, "source.mkv", "output.mkv")

    assert "-0:a:0" in rendered.argv
    assert [rendered.argv[n + 1] for n, arg in enumerate(rendered.argv) if arg == "-metadata:s:a:0"] == [
        "language=eng",
        "title=2.0",
    ]
    assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.COPY} == {0, 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_id", ["transcode.remux", "transcode.video"])
async def test_cleanup_audio_cancels(plugin_id: str) -> None:
    inputs = {"container": "original"} if plugin_id == "transcode.remux" else {"container": "mp4", "target": "avc"}
    async with host_for([STARTER_PLUGINS]) as host:
        decision = await evaluated(
            host,
            plugin_id,
            streams=[video(), audio(language="fre"), audio(index=2, language="spa")],
            inputs={**inputs, "audio_keep_languages": ["eng"], "audio_keep_all_if_empty": True},
        )

    assert decision.type == "skip"
    assert decision.plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(RateControlMode))
@pytest.mark.parametrize("deferred", [False, True])
async def test_video_rate_modes(mode: RateControlMode, deferred: bool) -> None:
    encoder = "hevc_qsv"
    ctx = ctx_dict(streams=[video()])
    ctx["probe"]["raw"] = {"format": {"bit_rate": "10000000"}}
    inputs: dict[str, Any] = {"rate_control": mode.value, "quality": 19, "ratio": 0.5, "bitrate_source": "container"}
    inputs.update({"encoder_priority": [encoder]} if deferred else {"encoder": encoder})
    async with host_for([STARTER_PLUGINS]) as host:
        decision = await host.evaluate("transcode.video", ctx, inputs)
    assert decision.plan is not None
    plan = FFmpegPlan.model_validate(decision.plan).select_encoder([encoder])
    assert plan.video is not None and plan.video.rate is not None
    assert plan.video.rate.mode == mode
    assert plan.video.rate.quality == (19 if mode == RateControlMode.QUALITY else None)
    argv = render(plan, ProbeData.model_validate(ctx["probe"]), "source.mkv", "output.mkv").argv
    if mode == RateControlMode.BITRATE:
        assert argv[argv.index("-b:v:0") + 1] == "5000k"
        assert "-global_quality:v:0" not in argv and "-cq:v:0" not in argv and "-crf:v:0" not in argv
    else:
        assert argv[argv.index(f"-{plan.video.rate.quality_option}:v:0") + 1] == "19"
        assert "-maxrate:v:0" not in argv


@pytest.mark.asyncio
async def test_audio_language_facets() -> None:
    mixed = [video(), audio(language="eng"), audio(index=2, language="jpn")]
    async with host_for([STARTER_PLUGINS]) as host:
        match = await evaluated(host, "inspector.audio_languages", streams=mixed)
        assert match.type == "match"
        assert match.facets == {"unexpected_languages": ["jpn"]}
        plain = await evaluated(host, "inspector.audio_languages", streams=[video(), audio()])
        assert plain.facets == {"unexpected_languages": []}


@pytest.mark.asyncio
async def test_zero_quality_hardware() -> None:
    async with host_for([STARTER_PLUGINS]) as host:
        hardware = await evaluated(
            host,
            "transcode.video",
            streams=[video(), audio()],
            inputs={"encoder": "hevc_nvenc", "quality": 0, "ten_bit": True},
        )
        assert hardware.plan is not None
        hardware_plan = FFmpegPlan.model_validate(hardware.plan)
        assert hardware_plan.video is not None and hardware_plan.video.rate is not None
        assert hardware_plan.video.rate.quality == 0
        assert hardware_plan.video.rate.quality_option == "cq"
        assert hardware_plan.video.pixel_format == "p010le"


@pytest.mark.asyncio
async def test_cleared_encoder_preferences() -> None:
    async with host_for([STARTER_PLUGINS]) as host:
        preferred = await evaluated(
            host,
            "transcode.video",
            container="mkv",
            streams=[video(codec="hevc")],
            inputs={"encoder": None, "encoder_priority": ["hevc_nvenc"]},
        )
        assert preferred.plan is not None
        plan = FFmpegPlan.model_validate(preferred.plan)
        assert plan.video is not None and plan.video.encoder_priority == ["hevc_nvenc"]
        selected = plan.select_encoder(["hevc_nvenc"])
        assert selected.video is not None and selected.video.encoder == "hevc_nvenc"
        assert selected.video.rate is not None and selected.video.rate.quality_option == "cq"
        cleared = await evaluated(
            host, "transcode.video", container="mkv", streams=[video(codec="hevc")], inputs={"encoder": None}
        )
        assert cleared.type == "skip"


@pytest.mark.asyncio
@pytest.mark.parametrize("pix_fmt", ["nv16", "p010le"])
async def test_probed_video_precision(pix_fmt: str) -> None:
    probe = probe_from_raw(
        Path("/media/fixture.mp4"),
        2048,
        {"format": {"duration": "60"}, "streams": [{**video(), "pix_fmt": pix_fmt}]},
    )
    ctx = ctx_dict(streams=[])
    ctx["probe"] = probe.model_dump(mode="json")
    async with host_for([STARTER_PLUGINS]) as host:
        decision = await host.evaluate("transcode.video", ctx, {})
    assert decision.plan is not None
    plan = FFmpegPlan.model_validate(decision.plan)
    assert plan.video is not None
    assert plan.video.encoder == "libx265"
    assert plan.video.pixel_format == (None if pix_fmt in {"nv12", "nv16"} else "yuv420p10le")


@pytest.mark.asyncio
async def test_same_target_transforms() -> None:
    streams = [video(codec="hevc"), audio(), audio(index=2, codec="aac", language="spa")]
    async with host_for([STARTER_PLUGINS]) as host:
        remux = await evaluated(host, "transcode.video", streams=[video(codec="hevc"), audio()])
        reencode = await evaluated(
            host,
            "transcode.video",
            container="mkv",
            streams=streams,
            inputs={"quality": 0, "audio_keep_languages": ["eng"]},
        )

    assert remux.plan is not None
    assert FFmpegPlan.model_validate(remux.plan).video is None
    assert reencode.plan is not None
    plan = FFmpegPlan.model_validate(reencode.plan)
    video_plan = plan.video
    assert video_plan is not None and video_plan.rate is not None and video_plan.rate.quality == 0
    rendered = render(plan, ProbeData.model_validate(ctx_dict(container="mkv", streams=streams)["probe"]), "src", "dst")
    assert rendered.argv[rendered.argv.index("-c:v:0") + 1] == "libx265"
    assert "-0:a:1" in rendered.argv and "-0:a:0" not in rendered.argv


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_id", ["transcode.remux", "transcode.video"])
async def test_plugin_stream_changes(plugin_id: str) -> None:
    inputs = {"target": "avc"} if plugin_id == "transcode.video" else {}
    async with host_for([STARTER_PLUGINS]) as host:
        done = await evaluated(host, plugin_id, container="mkv", streams=[video()], inputs=inputs)
        assert done.type == "skip"
        pending = await evaluated(host, plugin_id, container="mp4", streams=[video()], inputs=inputs)
        assert pending.type == "plan"
        assert pending.plan is not None
        plan = FFmpegPlan.model_validate(pending.plan)
        assert plan.container == "mkv"
        assert plan.video is None
        assert plan.streams is None
        streams = [
            video(),
            audio(codec="dts"),
            audio(index=2, codec="aac"),
            {**audio(index=3, codec="aac"), "title": "Commentary"},
            audio(index=4, codec="aac", language="spa"),
            {**audio(index=5, codec="aac", language="jpn"), "disposition_forced": True},
            stream(6, "mov_text", "subtitle", "eng"),
            stream(7, "mov_text", "subtitle", "ger"),
            {**stream(8, "subrip", "subtitle", "jpn"), "disposition_forced": True},
            {**video(index=9, codec="mjpeg"), "disposition_attached_pic": True},
            stream(10, "bin_data", "data"),
        ]
        audio_plan = await evaluated(
            host,
            plugin_id,
            container="mp4",
            streams=streams,
            inputs={
                **inputs,
                "audio_codec": "aac",
                "audio_bitrate": "192k",
                "audio_when_codecs": ["dts"],
                "audio_keep_languages": ["eng"],
                "audio_max_per_language": 1,
                "audio_drop_descriptive": True,
                "subtitle_keep_languages": ["eng"],
                "drop_cover_art": True,
                "drop_data": True,
            },
        )
        assert audio_plan.plan is not None
        converted = FFmpegPlan.model_validate(audio_plan.plan)
        assert converted.audio_encode[0].when_codec_in == ["dts"]
        rendered = render(
            converted,
            ProbeData.model_validate(ctx_dict(container="mp4", streams=streams)["probe"]),
            "source.mkv",
            "output.mkv",
        )
        assert [rendered.argv[n + 1] for n, arg in enumerate(rendered.argv) if arg == "-map"] == [
            "0",
            "-0:a:1",
            "-0:a:2",
            "-0:a:3",
            "-0:s:1",
            "-0:9",
            "-0:10",
        ]
        assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.DROP} == {
            2,
            3,
            4,
            7,
            9,
            10,
        }
        assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.COPY} == {0, 5, 8}
        assert {entry.stream_index for entry in rendered.stream_log if entry.outcome == StreamOutcome.TRANSCODE} == {
            1,
            6,
        }
        assert rendered.argv[rendered.argv.index("-c:a:0") + 1] == "aac"
        assert "-c:a:1" not in rendered.argv
        assert rendered.argv[rendered.argv.index("-c:s:0") + 1] == "srt"
        assert "-c:s:1" not in rendered.argv


@pytest.mark.asyncio
async def test_video_extras_remux() -> None:
    async with host_for([STARTER_PLUGINS]) as host:
        no_op = await evaluated(
            host, "transcode.video", container="mp4", streams=[video()], inputs={"target": "avc", "container": "mp4"}
        )
        assert no_op.type == "skip"
        decision = await evaluated(
            host,
            "transcode.video",
            container="mp4",
            streams=[video()],
            inputs={"target": "avc", "container": "mp4", "extras": ["-movflags", "+faststart"]},
        )
        assert decision.plan is not None
        plan = FFmpegPlan.model_validate(decision.plan)
        assert plan.video is None
        assert plan.extras == ["-movflags", "+faststart"]


@pytest.mark.asyncio
async def test_mjpeg_recording_eligible() -> None:
    async with host_for([STARTER_PLUGINS]) as host:
        recording = await evaluated(
            host, "transcode.video", streams=[{**video(codec="mjpeg"), "fps": 30, "disposition_attached_pic": False}]
        )
        cover = await evaluated(
            host, "transcode.video", streams=[{**video(codec="mjpeg"), "disposition_attached_pic": True}]
        )
    assert recording.type == "plan"
    assert cover.type == "skip"


@pytest.mark.asyncio
async def test_typed_inputs_instantiated() -> None:
    root = make_test_temp_dir("plugins-typed")
    stage_all(root, "filter.typed")
    async with host_for([root]) as host:
        defaults = await evaluated(host, "filter.typed", streams=[video()])
        runner = host._plugins["filter.typed"].runner
        assert defaults.facets is not None
        assert defaults.facets["mode"] == "fast"
        assert defaults.facets["threshold"] == 5
        custom = await evaluated(host, "filter.typed", streams=[video()], inputs={"threshold": 9, "mode": "slow"})
        assert custom.facets is not None
        assert custom.facets["mode"] == "slow"
        assert custom.facets["threshold"] == 9
        for inputs in ({"threshold": 11}, {"threshold": True}, {"bogus": 1}):
            bad = await evaluated(host, "filter.typed", streams=[video()], inputs=inputs)
            assert bad.type == "error"
            assert bad.error is not None
        recovered = await evaluated(host, "filter.typed", streams=[video()])
        assert recovered.type == "match"
        assert host._plugins["filter.typed"].runner is runner


@pytest.mark.asyncio
async def test_garbage_decision_rejected() -> None:
    root = make_test_temp_dir("plugins-garbage")
    stage_all(root, "filter.garbage", "filter.always_match")
    async with host_for([root]) as host:
        bad = await evaluated(host, "filter.garbage", streams=[video()])
        assert bad.type == "error"
        assert bad.error is not None
        runner = host._plugins["filter.garbage"].runner
        assert (await evaluated(host, "filter.garbage", streams=[video()])).type == "error"
        assert host._plugins["filter.garbage"].runner is runner
        ok = await evaluated(host, "filter.always_match", streams=[video()])
        assert ok.type == "match"


@pytest.mark.asyncio
async def test_plugin_raise_recovers(tmp_path: Path) -> None:
    root = make_test_temp_dir("plugins-crash")
    stage_all(root, "filter.crash", "filter.always_match")
    handler = install_log_file(tmp_path)
    try:
        async with host_for([root]) as host:
            boom = await evaluated(host, "filter.crash", streams=[video()])
            assert boom.type == "error"
            assert boom.error is not None
            ok = await evaluated(host, "filter.always_match", streams=[video()])
            assert ok.type == "match"
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    entries = query_logs(tmp_path)["entries"]
    assert len(entries) == 1
    assert entries[0]["logger"] == "server.plugins.host" and entries[0]["level"] == "warning"
    assert entries[0]["fields"]["plugin_id"] == "filter.crash"
    assert entries[0]["fields"]["operation"] == "plugin_evaluation"
    assert entries[0]["fields"]["outcome"] == "error"
    assert boom.error in entries[0]["fields"]["error"]


@pytest.mark.asyncio
async def test_description_failure_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    stage_all(tmp_path, "filter.echo")
    (tmp_path / "filter.echo/plugin.py").write_text("raise RuntimeError('missing fixture dependency')\n")
    async with host_for([tmp_path]) as host:
        assert await host.describe_inputs("filter.echo") is None
    records = [record for record in caplog.records if record.name == "server.plugins.host"]
    assert records and all(record.levelno == logging.WARNING for record in records)
    assert any("missing fixture dependency" in getattr(record, "error", "") for record in records)


@pytest.mark.asyncio
async def test_timeout_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("plugins-timeout")
    stage_all(root, "filter.always_match")
    async with host_for([root], decide_timeout_s=0) as host:
        spawn = host._spawn
        runners: list[asyncio.subprocess.Process] = []

        async def tracked_spawn(state: Any) -> asyncio.subprocess.Process:
            proc = await spawn(state)
            runners.append(proc)
            return proc

        monkeypatch.setattr(host, "_spawn", tracked_spawn)
        result = await evaluated(host, "filter.always_match", streams=[video()])
        assert result.type == "error"
        assert result.error is not None
        assert len(runners) == 1 and runners[0].returncode is not None
        assert host._plugins["filter.always_match"].runner is None
        host._decide_timeout_s = 10
        recovered = await evaluated(host, "filter.always_match", streams=[video()])
        assert recovered.type == "match"
        assert len(runners) == 2


@pytest.mark.asyncio
async def test_runner_death_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("plugins-death")
    stage_all(root, "filter.die", "filter.always_match")
    async with host_for([root]) as host:
        spawn = host._spawn
        runners: list[asyncio.subprocess.Process] = []

        async def tracked_spawn(state: Any) -> asyncio.subprocess.Process:
            proc = await spawn(state)
            runners.append(proc)
            return proc

        monkeypatch.setattr(host, "_spawn", tracked_spawn)
        dead = await evaluated(host, "filter.die", streams=[video()])
        assert dead.type == "error"
        assert dead.error is not None
        assert len(runners) == 1 and runners[0].returncode is not None
        shutil.copyfile(root / "filter.always_match" / "plugin.py", root / "filter.die" / "plugin.py")
        ok = await evaluated(host, "filter.die", streams=[video()])
        assert ok.type == "match"
        assert len(runners) == 2


@pytest.mark.asyncio
async def test_hot_reload_swaps(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("plugins-reload")
    stage_all(root, "filter.echo")
    plugin_py = root / "filter.echo" / "plugin.py"
    async with host_for([root]) as host:
        ready, changed, reloaded = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def watch(*_roots: Path, **_kwargs: Any) -> AsyncIterator[set[tuple[int, str]]]:
            ready.set()
            await asyncio.wait_for(changed.wait(), 5)
            yield {(2, str(plugin_py))}
            reloaded.set()

        monkeypatch.setattr("server.plugins.host.watchfiles.awatch", watch)
        host.start_watching()
        await asyncio.wait_for(ready.wait(), 5)
        before = await evaluated(host, "filter.echo", streams=[video()])
        assert before.facets is not None
        assert before.facets["mode"] == "x"
        plugin_py.write_text(ECHO_Y_SOURCE, encoding="utf-8")
        changed.set()
        await asyncio.wait_for(reloaded.wait(), 5)
        after = await evaluated(host, "filter.echo", streams=[video()])
        assert after.facets is not None
        assert after.facets["mode"] == "y"


@pytest.mark.asyncio
async def test_watcher_removes_rename() -> None:
    root = make_test_temp_dir("plugins-rename")
    stage_all(root, "filter.echo")
    old = root / "filter.echo"
    new = root / "filter.renamed"
    async with host_for([root]) as host:
        await evaluated(host, "filter.echo", streams=[video()])
        old.rename(new)
        manifest = new / "plugin.toml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace("filter.echo", "filter.renamed"), encoding="utf-8"
        )

        await host._apply_changes({old, new})

        assert host.describe("filter.echo") is None
        assert host.describe("filter.renamed") is not None
        assert not host.invalid_plugins


@pytest.mark.asyncio
async def test_invalid_manifest_isolated() -> None:
    root = make_test_temp_dir("plugins-invalid")
    stage_all(root, "filter.always_match", "broken_id")
    async with host_for([root]) as host:
        assert "broken_id" in host.invalid_plugins
        assert host.describe("filter.always_match") is not None
        ok = await evaluated(host, "filter.always_match", streams=[video()])
        assert ok.type == "match"
        assert [info.manifest.id for info in host.plugins()] == ["filter.always_match"]


@pytest.mark.asyncio
async def test_runner_protocol() -> None:
    root = make_test_temp_dir("plugins-protocol")
    stage_all(root, "filter.typed")
    proc = await start_runner(root / "filter.typed")
    try:
        async with asyncio.timeout(20):
            assert await roundtrip(proc, {"op": "ping"}) == {"pong": True}
            described = await roundtrip(proc, {"op": "describe"})
            assert described["manifest"]["id"] == "filter.typed"
            fields = {field["name"]: field for field in described["inputs"]}
            threshold = fields["threshold"]
            assert (threshold["type"], threshold["min"], threshold["max"]) == ("int", 0, 10)
            mode = fields["mode"]
            assert (mode["type"], mode["default"], mode["values"]) == ("enum", "fast", ["fast", "slow"])
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(b"not json\n")
            await proc.stdin.drain()
            assert "error" in json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
            decision = await roundtrip(proc, {"op": "decide", "ctx": ctx_dict(streams=[video()]), "inputs": {}})
            assert decision["decision"]["type"] == "match"
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 10)
            assert proc.returncode == 0
    finally:
        await stop_runner(proc)


def test_manifest_requires_description() -> None:
    root = make_test_temp_dir("plugins-toml-description")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


def test_manifest_metadata_rejected() -> None:
    root = make_test_temp_dir("plugins-toml-metadata")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)

    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)

    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "ftp://example.com/plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


def test_rejects_bad_requires() -> None:
    root = make_test_temp_dir("plugins-toml-requires")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\nrequires = ["Not A Tag"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


def test_rejects_bad_dependency() -> None:
    root = make_test_temp_dir("plugins-toml-require-plugins")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\nrequire_plugins = ["bogus"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


def test_rejects_unknown_keys() -> None:
    root = make_test_temp_dir("plugins-toml-extra")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\nbogus = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


@pytest.mark.asyncio
async def test_reset_uploaded_keeps_bundled() -> None:
    uploaded = make_test_temp_dir("plugins-uploaded")
    stage_all(uploaded, "filter.echo")
    async with host_for([STARTER_PLUGINS], uploaded_dir=uploaded) as host:
        host.start_watching()
        assert host.describe("filter.echo") is not None
        watcher = host._watch_task

        await host.reset_uploaded()

        assert host._watch_task is watcher
        assert not (uploaded / "filter.echo").exists()
        assert host.describe("transcode.video") is not None
        assert all(info.source == "bundled" for info in host.plugins())


@pytest.mark.asyncio
async def test_reset_missing_uploaded() -> None:
    uploaded = make_test_temp_dir("plugins-missing-uploaded")
    async with host_for([STARTER_PLUGINS], uploaded_dir=uploaded) as host:
        host.start_watching()
        watcher = host._watch_task
        uploaded.rmdir()

        await host.reset_uploaded()

        assert not uploaded.exists()
        assert host._watch_task is watcher
        assert host.describe("transcode.video") is not None


def test_rejects_kind_mismatch() -> None:
    root = make_test_temp_dir("plugins-toml-kind")
    (root / "plugin.toml").write_text(
        'id = "filter.x"\nkind = "transcode"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)


def test_rejects_reserved_namespace() -> None:
    root = make_test_temp_dir("plugins-toml-reserved")
    (root / "plugin.toml").write_text(
        'id = "filter.media"\nkind = "filter"\nname = "X"\ndescription = "Test manifest"\nauthor = "Ada"\nurl = "https://example.com/plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_manifest(root)
