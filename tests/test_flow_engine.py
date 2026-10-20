from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio

from common.actions import ExtractSubtitles, Notify, PreserveTimestamps
from common.ctx import FacetValue, PluginContext
from common.media.probe import ProbeData, ProbeStream
from common.metadata import MetadataValues, SidecarMetadata
from common.plan import StreamSelection, render
from common.enums import StreamOutcome
from server.flows.definition import ConditionNode, FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.engine import FlowContinuation, evaluate, evaluate_post
from server.flows.policy import filter_actions
from server.plugins.host import PluginHost
from tests.helpers import plugin_host

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_PLUGINS = REPO_ROOT / "plugins"
FAKE_PLUGINS = Path(__file__).parent / "fakes" / "plugins"

SIZE_4GIB = 4 * 2**30


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("plugin", "inputs", "policy", "existing", "tags", "matched"),
    [
        ("transcode.remux", {"container": "mkv"}, "skip", "fill_missing", {}, False),
        ("transcode.remux", {"container": "mkv"}, "remux", "fill_missing", {}, True),
        ("transcode.video", {"target": "avc"}, "remux", "fill_missing", {}, True),
        ("transcode.remux", {"container": "mp4"}, "skip", "fill_missing", {}, True),
        ("transcode.video", {"target": "hevc"}, "skip", "fill_missing", {}, True),
        ("transcode.remux", {"container": "mkv"}, "remux", "fill_missing", {"TITLE": "Existing"}, False),
        ("transcode.remux", {"container": "mkv"}, "remux", "overwrite", {"Title": "Sidecar"}, False),
        ("transcode.remux", {"container": "mkv"}, "remux", "overwrite", {"TITLE": "Existing"}, True),
    ],
)
async def test_metadata_policy(host, plugin, inputs, policy, existing, tags, matched) -> None:
    ctx = make_ctx(1000, [video()])
    ctx.probe.raw = {"format": {"tags": {**tags, "COPYRIGHT": "Unrelated"}}}
    ctx.sidecar_metadata = SidecarMetadata(sources={"nfo": MetadataValues(title="Sidecar")})
    flow = graph_flow(
        "metadata",
        [
            pnode("metadata", "action.embed_metadata", existing_tags=existing, when_no_media_changes=policy),
            pnode("process", plugin, **inputs),
        ],
        [("metadata", "next", "process")],
    )
    result = await evaluate(flow, ctx, host)
    assert result.status == ("matched" if matched else "skipped")
    assert not result.actions
    if matched:
        assert result.plan is not None and result.plan.metadata is not None
        rendered = render(result.plan, ctx.probe, "/in", "/out")
        assert any(arg.endswith("=Sidecar") for arg in rendered.argv)
        assert not any(arg.startswith("COPYRIGHT=") for arg in rendered.argv)
        assert not ctx.metadata_requests


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("later_remux", [False, True])
async def test_metadata_semantic_skip(host, later_remux: bool) -> None:
    ctx = make_ctx(1000, [video()])
    ctx.sidecar_metadata = SidecarMetadata(sources={"nfo": MetadataValues(title="Sidecar")})
    nodes = [
        pnode("metadata", "action.embed_metadata", when_no_media_changes="remux"),
        pnode("burn", "transcode.burn_subtitles"),
    ]
    edges = [("metadata", "next", "burn")]
    if later_remux:
        nodes.append(pnode("remux", "transcode.remux", container="mkv"))
        edges.append(("burn", "skip", "remux"))
    result = await evaluate(graph_flow("metadata", nodes, edges), ctx, host)
    assert result.steps[1].decision == "skip"
    if later_remux:
        assert result.plan is not None and result.plan.video is None and result.plan.metadata is not None
    else:
        assert result.status == "skipped" and result.plan is None


@pytest.mark.asyncio(loop_scope="module")
async def test_metadata_continuation(host) -> None:
    ctx = make_ctx(1000, [video()])
    ctx.sidecar_metadata = SidecarMetadata(sources={"nfo": MetadataValues(title="Original sidecar")})
    flow = graph_flow(
        "metadata",
        [
            pnode("metadata", "action.embed_metadata"),
            pnode("cleanup", "transcode.remux", container="mp4"),
            pnode("encode", "transcode.video", target="hevc", container="mp4"),
            pnode("again", "action.embed_metadata"),
            pnode("final", "transcode.remux", container="mkv"),
        ],
        [
            ("metadata", "next", "cleanup"),
            ("cleanup", "plan", "encode"),
            ("encode", "plan", "again"),
            ("again", "next", "final"),
        ],
    )
    first = await evaluate(flow, ctx, host)
    assert first.plan is not None and first.plan.metadata is not None and first.continuation is not None
    continuation = FlowContinuation.model_validate_json(first.continuation.model_dump_json())
    output = make_ctx(950, [video()], container="mp4")
    output.probe.file_path = "/workspace/stage_0.mp4"
    output.sidecar_metadata = SidecarMetadata(sources={"nfo": MetadataValues(title="Changed sidecar")})
    # Metadata is applied once even if an intermediate probe lacks the original title.
    second = await evaluate_post(continuation, output, host)
    assert second.plan is not None and second.plan.metadata is None and second.continuation is not None
    assert second.plan.video is not None
    rendered = render(second.plan, output.probe, "/stage_0", "/stage_1")
    assert rendered.argv[rendered.argv.index("-movflags") + 1] == "+use_metadata_tags"
    third = await evaluate_post(second.continuation, output, host)
    assert third.plan is not None and third.plan.metadata is not None
    assert third.plan.metadata.title == "Original sidecar"


@pytest.mark.asyncio(loop_scope="module")
async def test_metadata_container_error(host) -> None:
    ctx = make_ctx(1000, [video()], container="avi")
    ctx.sidecar_metadata = SidecarMetadata(sources={"nfo": MetadataValues(title="Title")})
    flow = graph_flow(
        "metadata",
        [
            pnode("metadata", "action.embed_metadata", when_no_media_changes="remux"),
            pnode("process", "transcode.remux", container="original"),
        ],
        [("metadata", "next", "process")],
    )
    result = await evaluate(flow, ctx, host)
    assert result.status == "error"
    assert result.steps[0].decision == "actions"
    assert result.steps[-1].plugin == "transcode.remux"
    assert "supports MKV and MP4, not AVI" in (result.steps[-1].error or "")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def host() -> AsyncIterator[PluginHost]:
    # Share only stateless starter decisions. Lifecycle and fake-plugin tests keep fresh hosts.
    async with plugin_host([STARTER_PLUGINS]) as host:
        yield host


def seeded_facets() -> dict[str, FacetValue]:
    return {
        "media.file.size_bytes": SIZE_4GIB,
        "media.file.estimated_bitrate_kbps": 11_453,
        "media.video.codec": "h264",
        "media.video.codec_family": "avc",
        "media.video.hdr_format": "sdr",
        "media.video.bitrate_kbps": 11_453,
        "media.video.bitrate_source": "estimated",
        "media.audio.languages": ["eng"],
        "media.audio.descriptive_stream_indices": [],
        "media.subtitle.languages": [],
        "media.subtitle.forced_present": False,
    }


def make_ctx(size_bytes: int, streams: list[ProbeStream], container: str = "mkv") -> PluginContext:
    probe = ProbeData(
        file_path="/media/fixture.mkv",
        container=container,
        duration_s=3000.0,
        size_bytes=size_bytes,
        streams=streams,
    )
    return PluginContext(probe=probe, original_filename="movie.mkv")


def video(index: int = 0, codec: str = "h264") -> ProbeStream:
    return ProbeStream(index=index, codec=codec, codec_type="video")


def audio(index: int = 1, codec: str = "ac3", language: str | None = "eng") -> ProbeStream:
    return ProbeStream(index=index, codec=codec, codec_type="audio", language=language)


def pnode(node_id: str, plugin: str, **inputs: Any) -> PluginNode:
    return PluginNode(id=node_id, plugin=plugin, inputs=inputs)


def cnode(node_id: str, when: dict[str, Any]) -> ConditionNode:
    return ConditionNode(id=node_id, when=when)


def tnode(node_id: str, outcome: Literal["skip", "done", "veto"] = "done") -> TerminalNode:
    return TerminalNode(id=node_id, outcome=outcome)


def graph_flow(
    entry: str,
    nodes: list,
    edges: list[tuple[str, str, str]],
    *,
    actions: list | None = None,
    when: dict[str, Any] | None = None,
) -> FlowDefinition:
    return FlowDefinition(
        entry=entry,
        nodes=nodes,
        edges=[FlowEdge(src=src, port=port, dst=dst) for src, port, dst in edges],
        actions=actions or [],
        when=when,
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_facets_merge_namespaced(host: PluginHost) -> None:
    flow = graph_flow(
        "size",
        [cnode("size", {"media.file.size_bytes": {"gte": 2 * 2**30}}), pnode("foreign", "inspector.audio_languages")],
        [("size", "true", "foreign")],
    )
    streams = [video(), audio(), audio(index=2, language="jpn")]
    result = await evaluate(flow, make_ctx(SIZE_4GIB, streams), host)

    assert result.status == "skipped"
    assert [(step.node, step.decision) for step in result.steps] == [("size", "true"), ("foreign", "match")]
    assert result.steps[0].facets == {}
    assert result.steps[1].facets == {"audio_languages.unexpected_languages": ["jpn"]}
    assert result.facets["media.audio.languages"] == ["eng", "jpn"]
    assert result.facets["audio_languages.unexpected_languages"] == ["jpn"]


@pytest.mark.asyncio(loop_scope="module")
async def test_veto_stops_flow(host: PluginHost) -> None:
    flow = graph_flow(
        "size",
        [
            cnode("size", {"media.file.size_bytes": {"gte": 2 * 2**30}}),
            pnode("enc", "transcode.video"),
            TerminalNode(id="veto", outcome="veto"),
            tnode("done"),
        ],
        [("size", "true", "enc"), ("size", "false", "veto"), ("enc", "plan", "done")],
    )
    result = await evaluate(flow, make_ctx(2**30, [video(), audio()]), host)

    assert result.status == "vetoed"
    assert [(step.node, step.decision) for step in result.steps] == [("size", "false"), ("veto", "veto")]
    assert result.plan is None
    assert result.continuation is None


@pytest.mark.asyncio(loop_scope="module")
async def test_skip_chain(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video"), pnode("remux", "transcode.remux"), tnode("done")],
        [("enc", "skip", "remux"), ("enc", "plan", "done"), ("remux", "plan", "done")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(codec="hevc")]), host)

    assert result.status == "skipped"
    assert result.plan is None
    assert [(step.node, step.decision) for step in result.steps] == [("enc", "skip"), ("remux", "skip")]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("inputs", "container", "codec", "changed"),
    [
        ({"audio_keep_languages": ["eng"]}, "mkv", "hevc", False),
        ({"audio_codec": "aac", "audio_bitrate": "192k", "audio_when_codecs": ["dts"]}, "mkv", "hevc", False),
        ({"audio_codec": "ac3", "audio_bitrate": "192k", "audio_when_codecs": ["aac"]}, "mkv", "hevc", True),
        ({}, "mp4", "hevc", True),
        ({"extras": ["-metadata", "title=changed"]}, "mkv", "hevc", True),
        ({}, "mkv", "h264", True),
        ({"fix_timestamps": True}, "avi", "hevc", True),
    ],
)
async def test_video_change_edges(
    inputs: dict[str, Any], container: str, codec: str, changed: bool, host: PluginHost
) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video", **inputs), tnode("noop", "skip"), tnode("done")],
        [("enc", "skip", "noop"), ("enc", "plan", "done")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(codec=codec), audio(codec="aac")], container), host)
    if changed:
        assert result.plan is not None
        assert [(step.node, step.decision) for step in result.steps] == [("enc", "plan")]
        assert result.continuation is not None and result.continuation.node == "done"
    else:
        assert result.plan is None
        assert [(step.node, step.decision) for step in result.steps] == [("enc", "skip"), ("noop", "skip")]


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize(
    ("codec", "width", "height", "inputs", "artwork", "changed"),
    [
        ("h264", 1920, 1080, {}, False, True),
        ("hevc", 3840, 2160, {"max_width": 1920}, False, True),
        ("hevc", 1920, 2160, {"max_height": 1080}, False, True),
        ("hevc", 1920, 1080, {"max_width": 1920}, False, False),
        ("h264", 3840, 2160, {"max_width": 1920}, True, False),
    ],
)
async def test_multivideo_change_edges(
    codec: str, width: int, height: int, inputs: dict[str, Any], artwork: bool, changed: bool, host: PluginHost
) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video", **inputs), tnode("noop", "skip"), tnode("done")],
        [("enc", "skip", "noop"), ("enc", "plan", "done")],
    )
    streams = [
        ProbeStream(index=0, codec="hevc", codec_type="video", width=1920, height=1080),
        ProbeStream(
            index=1, codec=codec, codec_type="video", width=width, height=height, disposition_attached_pic=artwork
        ),
    ]
    result = await evaluate(flow, make_ctx(SIZE_4GIB, streams), host)
    if changed:
        assert result.plan is not None and result.plan.video is not None
        assert result.continuation is not None and result.continuation.node == "done"
        argv = render(result.plan, make_ctx(SIZE_4GIB, streams).probe, "source.mkv", "output.mkv").argv
        if inputs:
            assert "-filter:v:0" not in argv
            assert argv[argv.index("-filter:v:1") + 1].startswith("scale=")
    else:
        assert result.plan is None
        assert [(step.node, step.decision) for step in result.steps] == [("enc", "skip"), ("noop", "skip")]


@pytest.mark.asyncio(loop_scope="module")
async def test_earlier_facets(host: PluginHost) -> None:
    flow = graph_flow(
        "inspect",
        [
            pnode("inspect", "inspector.audio_languages"),
            cnode("gate", {"audio_languages.unexpected_languages": {"contains": "jpn"}}),
            pnode("enc", "transcode.video"),
            pnode("remux", "transcode.remux"),
            tnode("done"),
        ],
        [
            ("inspect", "match", "gate"),
            ("gate", "true", "enc"),
            ("gate", "false", "remux"),
            ("enc", "plan", "done"),
            ("remux", "plan", "done"),
        ],
    )
    result = await evaluate(
        flow,
        make_ctx(SIZE_4GIB, [video(), audio(), audio(index=2, language="jpn")], container="mp4"),
        host,
    )

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [
        ("inspect", "match"),
        ("gate", "true"),
        ("enc", "plan"),
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_stored_facets_route(host: PluginHost) -> None:
    flow = graph_flow(
        "gate",
        [
            cnode("gate", {"bitrate_estimate.hevc_kbps": 26662}),
            pnode("enc", "transcode.video"),
            pnode("remux", "transcode.remux"),
            tnode("done"),
        ],
        [("gate", "true", "enc"), ("gate", "false", "remux"), ("enc", "plan", "done"), ("remux", "plan", "done")],
    )
    ctx = make_ctx(SIZE_4GIB, [video(), audio()], container="mp4")
    ctx.facets["bitrate_estimate.hevc_kbps"] = 26662
    result = await evaluate(flow, ctx, host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("gate", "true"), ("enc", "plan")]
    assert result.facets["bitrate_estimate.hevc_kbps"] == 26662
    assert result.facets["media.file.size_bytes"] == SIZE_4GIB


@pytest.mark.asyncio(loop_scope="module")
async def test_bitrate_fact_routes(host: PluginHost) -> None:
    flow = graph_flow(
        "gate",
        [
            cnode("gate", {"media.file.estimated_bitrate_kbps": {"gte": 8000}}),
            pnode("enc", "transcode.video"),
            pnode("remux", "transcode.remux"),
            tnode("done"),
        ],
        [("gate", "true", "enc"), ("gate", "false", "remux"), ("enc", "plan", "done"), ("remux", "plan", "done")],
    )
    big = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()], container="mp4"), host)
    small = await evaluate(flow, make_ctx(2**30, [video(), audio()], container="mp4"), host)

    # Over 3000 s, 4 GiB estimates to 11_453 kbps and 1 GiB to 2_863 kbps.
    assert [(step.node, step.decision) for step in big.steps] == [("gate", "true"), ("enc", "plan")]
    assert big.plan is not None and big.plan.video is not None
    assert [(step.node, step.decision) for step in small.steps] == [("gate", "false"), ("remux", "plan")]
    assert small.plan is not None and small.plan.video is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin", "decision"),
    [
        ("filter.garbage", {"type": "nonsense"}),
        ("filter.garbage", {"type": "match", "facets": {"media.video.codec": "hevc"}}),
        ("transcode.nvenc", {"type": "plan", "plan": {"container": "avi"}}),
    ],
)
async def test_invalid_decision_stops(tmp_path: Path, plugin: str, decision: dict[str, Any]) -> None:
    shutil.copytree(FAKE_PLUGINS / plugin, tmp_path / plugin)
    (tmp_path / plugin / "plugin.py").write_text(f"def decide(ctx, inputs):\n    return {decision!r}\n")
    flow = graph_flow(
        "bad",
        [pnode("bad", plugin), cnode("size", {"media.file.size_bytes": {"gte": 2 * 2**30}})],
        [("bad", "plan" if plugin.startswith("transcode.") else "match", "size")],
    )
    async with plugin_host([tmp_path]) as host:
        result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.steps[0].decision == "error"
    assert result.steps[0].error is not None
    assert len(result.steps) == 1
    assert result.status == "error"
    assert result.plan is None
    assert result.continuation is None
    assert result.facets["media.video.codec"] == "h264"


@pytest.mark.asyncio(loop_scope="module")
async def test_unknown_plugin_errors(host: PluginHost) -> None:
    flow = graph_flow(
        "bad",
        [pnode("bad", "filter.nope"), cnode("size", {"media.file.size_bytes": {"gte": 2 * 2**30}})],
        [("bad", "next", "size")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.steps[0].decision == "error"
    assert result.steps[0].error is not None
    assert len(result.steps) == 1
    assert result.status == "error"


@pytest.mark.asyncio(loop_scope="module")
async def test_actions_preplan(host: PluginHost) -> None:
    flow = graph_flow(
        "subs",
        [pnode("subs", "action.extract_subs"), pnode("enc", "transcode.video"), tnode("done")],
        [("subs", "next", "enc"), ("enc", "plan", "done")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("subs", "actions"), ("enc", "plan")]
    assert result.actions == [
        ExtractSubtitles(formats=["srt"], languages=["eng"], plugin_id="action.extract_subs", config_version=1)
    ]
    assert result.continuation is not None and result.continuation.node == "done"


@pytest.mark.asyncio
async def test_encoder_union_collected() -> None:
    flow = graph_flow(
        "tagged",
        [pnode("tagged", "filter.tagged"), pnode("enc", "transcode.nvenc"), tnode("done")],
        [("tagged", "match", "enc"), ("enc", "plan", "done")],
    )
    async with plugin_host([STARTER_PLUGINS, FAKE_PLUGINS]) as host:
        result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert result.requires == ["gpu"]
    assert result.requires_encoders == ["hevc_nvenc"]


@pytest.mark.asyncio
async def test_loser_requirements() -> None:
    flow = graph_flow(
        "remux",
        [pnode("remux", "transcode.remux"), pnode("enc", "transcode.nvenc"), tnode("done")],
        [("remux", "skip", "enc"), ("remux", "plan", "done"), ("enc", "plan", "done")],
    )
    async with plugin_host([STARTER_PLUGINS, FAKE_PLUGINS]) as host:
        result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()], container="mp4"), host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("remux", "plan")]
    assert result.requires == []
    assert result.requires_encoders == []


@pytest.mark.asyncio
async def test_winner_requirements() -> None:
    flow = graph_flow(
        "remux",
        [pnode("remux", "transcode.remux"), pnode("enc", "transcode.nvenc"), tnode("done")],
        [("remux", "skip", "enc"), ("remux", "plan", "done"), ("enc", "plan", "done")],
    )
    async with plugin_host([STARTER_PLUGINS, FAKE_PLUGINS]) as host:
        result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("remux", "skip"), ("enc", "plan")]
    assert result.requires_encoders == ["hevc_nvenc"]


@pytest.mark.asyncio
async def test_condition_skips() -> None:
    flow = graph_flow(
        "gate",
        [
            cnode("gate", {"media.video.codec": "h264"}),
            pnode("tagged", "filter.tagged"),
            pnode("enc", "transcode.video"),
            tnode("done"),
        ],
        [
            ("gate", "true", "tagged"),
            ("gate", "false", "enc"),
            ("tagged", "match", "enc"),
            ("enc", "plan", "done"),
        ],
    )
    async with plugin_host([STARTER_PLUGINS, FAKE_PLUGINS]) as host:
        result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(codec="hevc"), audio()]), host)

    # The hevc source takes the false branch straight to the transcode, so the
    # tagged filter's gpu requirement never rides along.
    assert result.status == "skipped"
    assert [(step.node, step.decision) for step in result.steps] == [("gate", "false"), ("enc", "skip")]
    assert result.requires == []


@pytest.mark.asyncio(loop_scope="module")
async def test_flow_actions_deferred(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done")],
        [("enc", "plan", "done")],
        actions=[ExtractSubtitles(formats=["srt"], languages=["eng"])],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert result.actions == []
    assert result.post_actions == [ExtractSubtitles(formats=["srt"], languages=["eng"])]


@pytest.mark.asyncio(loop_scope="module")
async def test_plan_pauses_flow(host: PluginHost) -> None:
    flow = graph_flow(
        "size",
        [
            cnode("size", {"media.file.size_bytes": {"gte": 2 * 2**30}}),
            pnode("enc", "transcode.video"),
            pnode("subs", "action.extract_subs"),
            tnode("done"),
        ],
        [("size", "true", "enc"), ("enc", "plan", "subs"), ("subs", "next", "done")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("size", "true"), ("enc", "plan")]
    assert result.actions == []
    assert result.continuation is not None and result.continuation.node == "subs"


@pytest.mark.asyncio(loop_scope="module")
async def test_post_stream_guard(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [
            pnode("enc", "transcode.video"),
            pnode("guard", "inspector.stream_guard"),
            tnode("done"),
            tnode("veto", "veto"),
        ],
        [("enc", "plan", "guard"), ("guard", "match", "done"), ("guard", "veto", "veto")],
    )
    source = make_ctx(SIZE_4GIB, [video(), audio()])
    planned = await evaluate(flow, source, host)
    assert planned.continuation is not None

    missing_audio = make_ctx(SIZE_4GIB, [video()])
    missing_audio.source_probe = source.probe
    vetoed = await evaluate_post(planned.continuation, missing_audio, host)

    output_with_audio = make_ctx(SIZE_4GIB, [video(), audio()])
    output_with_audio.source_probe = source.probe
    matched = await evaluate_post(planned.continuation, output_with_audio, host)

    assert vetoed.status == "vetoed"
    assert [(step.node, step.decision) for step in vetoed.steps] == [("guard", "veto"), ("veto", "veto")]
    assert matched.status == "skipped"
    assert [(step.node, step.decision) for step in matched.steps] == [("guard", "match"), ("done", "done")]


@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_preserves_result(host: PluginHost) -> None:
    cases: list[tuple[Literal["skip", "done", "veto"], str]] = [
        ("done", "matched"),
        ("skip", "skipped"),
        ("veto", "vetoed"),
    ]
    for outcome, status in cases:
        flow = graph_flow(
            "done",
            [tnode("done", outcome), pnode("subs", "action.extract_subs")],
            [("done", "next", "subs")],
        )
        result = await evaluate_post(
            FlowContinuation(flow=flow, node="done"), make_ctx(SIZE_4GIB, [video(), audio()]), host
        )
        assert result.status == status
        assert [step.node for step in result.steps] == ["done", "subs"]
        assert result.actions == [
            ExtractSubtitles(formats=["srt"], languages=["eng"], plugin_id="action.extract_subs", config_version=1)
        ]


@pytest.mark.asyncio(loop_scope="module")
async def test_terminal_preserves_stop(host: PluginHost) -> None:
    flow = graph_flow("done", [tnode("done", "skip"), pnode("bad", "action.missing")], [("done", "next", "bad")])
    result = await evaluate_post(
        FlowContinuation(flow=flow, node="done"), make_ctx(SIZE_4GIB, [video(), audio()]), host
    )
    assert result.status == "skipped"
    assert [step.node for step in result.steps] == ["done", "bad"]


@pytest.mark.asyncio(loop_scope="module")
async def test_post_condition_routes(host: PluginHost) -> None:
    flow = graph_flow(
        "gate",
        [cnode("gate", {"media.video.codec": "hevc"}), pnode("subs", "action.extract_subs"), tnode("done")],
        [("gate", "true", "subs"), ("gate", "false", "done"), ("subs", "next", "done")],
    )
    hevc = await evaluate_post(
        FlowContinuation(flow=flow, node="gate"), make_ctx(SIZE_4GIB, [video(codec="hevc"), audio()]), host
    )
    h264 = await evaluate_post(FlowContinuation(flow=flow, node="gate"), make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert hevc.status == "matched"
    assert [(step.node, step.decision) for step in hevc.steps] == [
        ("gate", "true"),
        ("subs", "actions"),
        ("done", "done"),
    ]
    assert hevc.actions == [
        ExtractSubtitles(formats=["srt"], languages=["eng"], plugin_id="action.extract_subs", config_version=1)
    ]
    assert h264.status == "skipped"
    assert [(step.node, step.decision) for step in h264.steps] == [("gate", "false"), ("done", "done")]
    assert h264.actions == []


@pytest.mark.asyncio(loop_scope="module")
async def test_post_error_continues(host: PluginHost) -> None:
    flow = graph_flow(
        "bad",
        [pnode("bad", "action.missing"), pnode("subs", "action.extract_subs")],
        [("bad", "next", "subs")],
    )
    result = await evaluate_post(FlowContinuation(flow=flow, node="bad"), make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "error"
    assert [(step.node, step.decision) for step in result.steps] == [("bad", "error")]
    assert result.steps[0].error is not None
    assert result.actions == []


@pytest.mark.asyncio(loop_scope="module")
async def test_post_facts_win(host: PluginHost) -> None:
    flow = graph_flow(
        "gate",
        [cnode("gate", {"media.video.codec": "hevc"}), pnode("subs", "action.extract_subs"), tnode("done")],
        [("gate", "true", "subs"), ("gate", "false", "done"), ("subs", "next", "done")],
    )
    ctx = make_ctx(SIZE_4GIB, [video(codec="hevc"), audio()])
    ctx.facets["media.video.codec"] = "h264"
    result = await evaluate_post(FlowContinuation(flow=flow, node="gate"), ctx, host)

    assert [(step.node, step.decision) for step in result.steps] == [
        ("gate", "true"),
        ("subs", "actions"),
        ("done", "done"),
    ]
    assert result.facets["media.video.codec"] == "hevc"


def staged_flow() -> FlowDefinition:
    return graph_flow(
        "cleanup",
        [
            pnode("cleanup", "transcode.remux", container="original", audio_keep_languages=["eng"]),
            cnode("size", {"media.file.size_bytes": {"gte": 2000 * 2**20}}),
            pnode("encode", "transcode.video", encoder="libx265", ratio=0.5),
            tnode("done"),
        ],
        [
            ("cleanup", "plan", "size"),
            ("cleanup", "skip", "size"),
            ("size", "true", "encode"),
            ("size", "false", "done"),
            ("encode", "plan", "done"),
            ("encode", "skip", "done"),
        ],
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("cleaned_mib", [1800, 2200])
async def test_cleaned_size_routes(cleaned_mib: int, host: PluginHost) -> None:
    flow = staged_flow()
    ctx = make_ctx(2300 * 2**20, [video(), audio(), audio(index=2, language="jpn")])
    first = await evaluate(flow, ctx, host)
    assert first.plan is not None and first.plan.video is None
    assert first.continuation is not None
    cleaned = make_ctx(cleaned_mib * 2**20, [video(), audio()])
    cleaned.source_probe = ctx.probe
    cleaned.facets = first.facets
    result = await evaluate_post(first.continuation, cleaned, host)
    assert (result.plan is not None) == (cleaned_mib >= 2000)
    assert result.facets["media.file.size_bytes"] == cleaned_mib * 2**20
    assert result.steps[0].node == "size"


@pytest.mark.asyncio(loop_scope="module")
async def test_cleanup_noop_continues(host: PluginHost) -> None:
    result = await evaluate(staged_flow(), make_ctx(2300 * 2**20, [video(), audio()]), host)
    assert [(step.node, step.decision) for step in result.steps] == [
        ("cleanup", "skip"),
        ("size", "true"),
        ("encode", "plan"),
    ]
    assert result.plan is not None and result.plan.video is not None


@pytest.mark.asyncio(loop_scope="module")
async def test_flow_when_skips(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done")],
        [("enc", "plan", "done")],
        when={"media.container": "mp4"},
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "skipped"
    assert result.steps == []
    assert result.plan is None
    assert result.continuation is None
    assert result.requires == []
    assert result.post_actions == []


@pytest.mark.asyncio(loop_scope="module")
async def test_flow_when_runs(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done")],
        [("enc", "plan", "done")],
        when={"media.video.codec": "h264", "media.file.size_bytes": {"gte": 1}},
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [video(), audio()]), host)

    assert result.status == "matched"
    assert [(step.node, step.decision) for step in result.steps] == [("enc", "plan")]


def commentary_audio(index: int, language: str = "eng") -> ProbeStream:
    return ProbeStream(index=index, codec="ac3", codec_type="audio", language=language, title="Director's Commentary")


def subtitle(index: int, language: str) -> ProbeStream:
    return ProbeStream(index=index, codec="subrip", codec_type="subtitle", language=language)


@pytest.mark.asyncio(loop_scope="module")
async def test_stream_policy_inputs(host: PluginHost) -> None:
    flow = graph_flow(
        "enc",
        [
            pnode(
                "enc",
                "transcode.video",
                audio_keep_languages=["eng"],
                audio_drop_descriptive=True,
                audio_max_per_language=1,
                subtitle_strip_all=True,
            )
        ],
        [],
    )
    streams = [
        video(),
        audio(index=1, language="eng"),
        audio(index=2, language="jpn"),
        commentary_audio(index=3),
        audio(index=4, language="eng"),
        subtitle(index=5, language="eng"),
        subtitle(index=6, language="spa"),
    ]
    ctx = make_ctx(SIZE_4GIB, streams)
    result = await evaluate(flow, ctx, host)

    assert result.status == "matched"
    assert result.plan is not None
    assert result.plan.streams is not None
    assert result.plan.streams.audio == StreamSelection(
        keep_languages=["eng"], drop_descriptive=True, max_per_language=1
    )
    assert result.plan.streams.subtitle == StreamSelection(keep_languages=[])

    rendered = render(result.plan, ctx.probe, "/media/in.mkv", "/cache/out.mkv")
    argv = rendered.argv
    drops = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-map" and argv[i + 1].startswith("-0:")]
    assert drops == ["-0:a:1", "-0:a:2", "-0:a:3", "-0:s:0", "-0:s:1"]
    log = {entry.stream_index: entry for entry in rendered.stream_log}
    assert log[1].outcome is StreamOutcome.COPY
    assert log[2].outcome == StreamOutcome.DROP
    assert log[3].outcome == StreamOutcome.DROP
    assert log[4].outcome == StreamOutcome.DROP
    assert {entry.outcome for entry in rendered.stream_log[5:]} == {StreamOutcome.DROP}


@pytest.mark.asyncio(loop_scope="module")
async def test_inspector_skip_continues(host: PluginHost) -> None:
    flow = graph_flow(
        "probe",
        [pnode("probe", "inspector.bitrate_estimate"), pnode("enc", "transcode.video"), tnode("done")],
        [("probe", "match", "enc"), ("enc", "plan", "done")],
    )
    result = await evaluate(flow, make_ctx(SIZE_4GIB, [audio()]), host)

    assert result.status == "skipped"
    assert [(step.node, step.decision) for step in result.steps] == [("probe", "skip")]


def test_filter_rejections() -> None:
    actions = [
        ExtractSubtitles(formats=["srt"], languages=["eng"]),
        PreserveTimestamps(),
        Notify(targets=["stable-target"], on=["job.succeeded"]),
    ]

    dispatchable, rejected = filter_actions(actions)
    assert [action.kind for action in dispatchable] == ["extract_subtitles"]
    assert rejected == []
