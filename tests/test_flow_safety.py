from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from common.ctx import PluginContext
from common.facts import builtin_facts
from common.media.probe import ProbeData, ProbeStream
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.flows.definition import ConditionNode, FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.engine import FlowContinuation, FlowResult, evaluate, evaluate_post
from server.flows.validation import validate_definition, validate_flow
from server.models import Flow, Job, Library, MediaFile, ProbeRow, utcnow
from common.fs.identity import source_identity
from tests.helpers import database_context, plugin_host
from tests.test_flow_validation import manifest
from tests.test_api import ApiEnv, requeue_outcome

PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def test_dependency_needs_predecessor() -> None:
    plugin = manifest("transcode.video", "transcode", require_plugins=["transcode.video"])
    flow = FlowDefinition(entry="encode", nodes=[PluginNode(id="encode", plugin=plugin.id)])
    errors = validate_flow({plugin.id: plugin}, flow)
    assert errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin", "inputs"),
    [
        ("transcode.remux", {"audio_codec": "aac"}),
        ("transcode.video", {"ratio": 0}),
        ("transcode.burn_subtitles", {"language": "English"}),
        ("transcode.burn_subtitles", {"name_priority": []}),
        ("action.notify", {"targets": [], "on": []}),
        (
            "action.notify_external",
            {"application": "sonarr", "url": "file:///media", "api_key": "private-api-key"},
        ),
    ],
)
async def test_validate_complete_inputs(plugin: str, inputs: dict) -> None:
    nodes = [PluginNode(id="subject", plugin=plugin, inputs=inputs)]
    edges = []
    if plugin.startswith("action."):
        nodes.append(PluginNode(id="encode", plugin="transcode.video"))
        edges.append(FlowEdge(src="subject", port="next", dst="encode"))
    flow = FlowDefinition(entry="subject", nodes=nodes, edges=edges)
    ctx = PluginContext(
        original_filename="movie.mkv",
        probe=ProbeData(
            file_path="movie.mkv",
            container="mkv",
            duration_s=60,
            size_bytes=1000,
            streams=[ProbeStream(index=0, codec="h264", codec_type="video")],
        ),
    )
    async with plugin_host([PLUGINS]) as host:
        decision = await host.evaluate(plugin, ctx.model_dump(mode="json"), inputs)
        assert decision.type == "error" and decision.error is not None
        errors = await validate_definition(host, flow)
    assert errors, f"flow save validation accepted inputs rejected by {plugin}: {decision.error}"


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["stopped", "timeout"])
async def test_validate_requires_schema(unavailable: str) -> None:
    flow = FlowDefinition(
        entry="encode",
        nodes=[PluginNode(id="encode", plugin="transcode.video", inputs={"unknown": True})],
    )
    async with plugin_host([PLUGINS]) as host:
        if unavailable == "stopped":
            await host.stop("transcode.video")
        else:
            host._decide_timeout_s = 0
        errors = await validate_definition(host, flow)
    assert errors, "flow save validation skipped an unavailable input schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["fps", "channels", "duration"])
async def test_post_forgets_missing(missing: str) -> None:
    key, expected = {
        "fps": ("media.video.fps", 24.0),
        "channels": ("media.audio.max_channels", 6),
        "duration": ("media.file.duration_s", 60.0),
    }[missing]
    flow = FlowDefinition(
        entry="encode",
        nodes=[
            PluginNode(id="encode", plugin="transcode.video"),
            ConditionNode(id="guard", when={key: expected}),
            TerminalNode(id="done", outcome="done"),
            TerminalNode(id="veto", outcome="veto"),
        ],
        edges=[
            FlowEdge(src="encode", port="plan", dst="guard"),
            FlowEdge(src="guard", port="true", dst="done"),
            FlowEdge(src="guard", port="false", dst="veto"),
        ],
    )
    source = ProbeData(
        file_path="source.mkv",
        container="mkv",
        duration_s=60,
        size_bytes=1000,
        streams=[
            ProbeStream(index=0, codec="h264", codec_type="video", fps=24),
            ProbeStream(index=1, codec="ac3", codec_type="audio", channels=6),
        ],
    )
    output = source.model_copy(deep=True)
    output.file_path = "output.mkv"
    output.streams[0].codec = "hevc"
    if missing == "fps":
        output.streams[0].fps = None
    elif missing == "channels":
        output.streams[1].channels = None
    else:
        output.duration_s = 0
    assert key not in builtin_facts(output)
    async with plugin_host([PLUGINS]) as host:
        assert await validate_definition(host, flow) == []
        selected = await evaluate(flow, PluginContext(probe=source, original_filename="movie.mkv"), host)
        assert selected.plan is not None and selected.continuation is not None
        continuation = FlowContinuation.model_validate_json(selected.continuation.model_dump_json())
        result = await evaluate_post(
            continuation,
            PluginContext(
                probe=output,
                source_probe=source,
                original_filename="movie.mkv",
                facets=selected.facets,
            ),
            host,
        )
    assert result.status == "vetoed"
    assert key not in result.facets
    assert [(step.node, step.decision) for step in result.steps] == [("guard", "false"), ("veto", "veto")]


@pytest.mark.asyncio
async def test_skip_rejects_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"source")
    async with database_context("flow-skipped-source") as db, plugin_host([PLUGINS]) as host:
        async with db() as session:
            flow = Flow(
                name="movies",
                definition=FlowDefinition(
                    entry="encode", nodes=[PluginNode(id="encode", plugin="transcode.video")]
                ).model_dump_json(),
            )
            session.add(flow)
            await session.flush()
            library = Library(name="movies", roots=[str(tmp_path)], flow_id=flow.id)
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(source),
                size=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns,
                source_identity=source_identity(source.stat()),
                container="mkv",
                medium="video",
                health_status="healthy",
                last_scanned_at=utcnow(),
            )
            session.add(media)
            await session.flush()
            probe = ProbeRow(
                media_file_id=media.id,
                raw_json='{"format":{"duration":"60"},"streams":[{"index":0,"codec_name":"hevc","codec_type":"video"}]}',
            )
            session.add(probe)
            await session.flush()
            media.probe_id = probe.id
            await session.commit()
            media_id = media.id

        async def evaluate_changed(*_args: object) -> FlowResult:
            source.write_bytes(b"changed source")
            async with db() as session:
                current = await session.get(MediaFile, media_id)
                assert current is not None
                current.size = source.stat().st_size
                current.mtime_ns = source.stat().st_mtime_ns
                current.transcode_decided = False
                current.health_status = "pending"
                current.first_seen = utcnow()
                await session.commit()
            return FlowResult(status="skipped", reason="old source already hevc")

        monkeypatch.setattr("server.flows.evaluation.evaluate", evaluate_changed)
        app = create_app(db, plugin_host=host)
        await app.state.jobs.start()
        try:
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.post(f"/api/files/{media_id}/transcode")
                assert response.status_code == 202
                job_id = response.json()["job_id"]
                env = ApiEnv(factory=db, client=client, app=app, lib_dir=tmp_path)
                assert (await requeue_outcome(env, job_id, event_type="request_result"))["outcome"] == "failed"
        finally:
            await app.state.jobs.close()
        async with db() as session:
            current = await session.get(MediaFile, media_id)
            assert current is not None and current.size == len(b"changed source")
            assert not current.transcode_decided
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "failed"
