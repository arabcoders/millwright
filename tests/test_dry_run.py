from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from common.enums import RateControlMode, StreamOutcome
from server.flows.definition import ConditionNode, FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.dryrun import dry_run
from server.plugins.host import PluginHost
from server.scanner.normalize import normalize
from tests.helpers import plugin_host

if TYPE_CHECKING:
    from common.media.probe import ProbeData

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_PLUGINS = REPO_ROOT / "plugins"
SRC = "/media/input.mkv"
SIZE_4GIB = 4 * 2**30

H264_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "disposition": {},
            "tags": {},
        },
        {
            "index": 1,
            "codec_name": "ac3",
            "codec_type": "audio",
            "channels": 2,
            "disposition": {},
            "tags": {"language": "eng"},
        },
    ],
    "format": {"duration": "3000.0"},
}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def host() -> AsyncIterator[PluginHost]:
    # Share only stateless starter decisions. Lifecycle and fake-plugin tests keep fresh hosts.
    async with plugin_host([STARTER_PLUGINS]) as host:
        yield host


def probe(name: str, size: int, raw: dict) -> ProbeData:
    stat = os.stat_result((0, 0, 0, 0, 0, 0, size, 0, 0, 0))
    return normalize(Path(f"/lib/{name}"), stat, raw).probe


def pnode(node_id: str, plugin: str, **inputs: Any) -> PluginNode:
    return PluginNode(id=node_id, plugin=plugin, inputs=inputs)


def graph(entry: str, nodes: list, edges: list[tuple[str, str, str]]) -> FlowDefinition:
    return FlowDefinition(
        entry=entry,
        nodes=nodes,
        edges=[FlowEdge(src=src, port=port, dst=dst) for src, port, dst in edges],
    )


def big_h264_flow() -> FlowDefinition:
    return graph(
        "size",
        [
            ConditionNode(id="size", when={"media.file.size_bytes": {"gte": 2 * 2**30}}),
            ConditionNode(id="codec", when={"media.video.codec": "h264"}),
            pnode("enc", "transcode.video"),
            TerminalNode(id="skip", outcome="skip"),
            TerminalNode(id="veto", outcome="veto"),
            TerminalNode(id="done", outcome="done"),
        ],
        [
            ("size", "true", "codec"),
            ("size", "false", "veto"),
            ("codec", "true", "enc"),
            ("codec", "false", "skip"),
            ("enc", "plan", "done"),
        ],
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("mode", list(RateControlMode))
async def test_dry_run_argv(host: PluginHost, mode: RateControlMode) -> None:
    flow = big_h264_flow()
    encode = next(node for node in flow.nodes if node.id == "enc")
    assert isinstance(encode, PluginNode)
    encode.inputs["rate_control"] = mode.value
    result = await dry_run(
        flow,
        probe("movie.mkv", SIZE_4GIB, H264_PROBE),
        host,
        SRC,
        library_name="movies",
    )

    assert result.flow.status == "matched"
    assert result.render is not None
    # 4 GiB over 3000 s gives 11_453_246 bps. A 50% target rounds to 5727k.
    rate_args = (
        ["-crf:v:0", "21"]
        if mode == RateControlMode.QUALITY
        else [
            "-b:v:0",
            "5727k",
            "-minrate:v:0",
            "4295k",
            "-maxrate:v:0",
            "7159k",
            "-bufsize:v:0",
            "11453k",
        ]
    )
    assert result.render.argv == [
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
        *rate_args,
        "-f",
        "matroska",
        SRC,
    ]
    assert [(entry.stream_index, entry.outcome) for entry in result.render.stream_log] == [
        (0, StreamOutcome.TRANSCODE),
        (1, StreamOutcome.COPY),
    ]


def post_extract_flow() -> FlowDefinition:
    return graph(
        "size",
        [
            ConditionNode(id="size", when={"media.file.size_bytes": {"gte": 2 * 2**30}}),
            pnode("enc", "transcode.video"),
            pnode("subs", "action.extract_subs"),
            TerminalNode(id="veto", outcome="veto"),
            TerminalNode(id="done", outcome="done"),
        ],
        [("size", "true", "enc"), ("size", "false", "veto"), ("enc", "plan", "subs"), ("subs", "next", "done")],
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_defers_post(host: PluginHost) -> None:
    result = await dry_run(
        post_extract_flow(),
        probe("movie.mkv", SIZE_4GIB, H264_PROBE),
        host,
        SRC,
    )

    assert result.flow.status == "deferred"
    assert [(step.node, step.decision) for step in result.flow.steps] == [
        ("size", "true"),
        ("enc", "plan"),
        ("subs", "deferred"),
    ]
    assert result.flow.continuation is not None and result.flow.continuation.node == "subs"


@pytest.mark.asyncio(loop_scope="module")
async def test_defers_encoder(host: PluginHost) -> None:
    flow = graph("enc", [pnode("enc", "transcode.video", encoder_priority=["libx265"])], [])
    result = await dry_run(flow, probe("movie.mkv", SIZE_4GIB, H264_PROBE), host, SRC)
    assert result.flow.status == "deferred"
    assert result.flow.plan is not None and result.render is None
    assert [(step.node, step.decision) for step in result.flow.steps] == [("enc", "plan"), ("enc", "deferred")]


@pytest.mark.asyncio(loop_scope="module")
async def test_skip_is_final(host: PluginHost) -> None:
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("subs", "action.extract_subs")],
        [("enc", "plan", "subs")],
    )
    source = probe("movie.mkv", SIZE_4GIB, H264_PROBE)
    source.real_video[0].codec = "hevc"
    result = await dry_run(flow, source, host, SRC)
    assert result.flow.status == "skipped"
    assert result.flow.plan is None and result.render is None
    assert [(step.node, step.decision) for step in result.flow.steps] == [("enc", "skip")]


@pytest.mark.asyncio(loop_scope="module")
async def test_veto_skips_post(host: PluginHost) -> None:
    result = await dry_run(
        post_extract_flow(),
        probe("small.mkv", 2**30, H264_PROBE),
        host,
        SRC,
    )

    assert result.flow.status == "vetoed"
    assert result.render is None
    assert [(step.node, step.decision) for step in result.flow.steps] == [("size", "false"), ("veto", "veto")]
