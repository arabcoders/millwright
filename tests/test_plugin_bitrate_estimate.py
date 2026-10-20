from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from common.ctx import PluginContext
from common.media.probe import ProbeData, ProbeStream
from common.plan import FFmpegPlan, RateControl, VideoTarget, render
from common.enums import RateControlMode

PLUGIN = Path(__file__).parents[1] / "plugins/inspector.bitrate_estimate/plugin.py"
spec = importlib.util.spec_from_file_location("bitrate_estimate", PLUGIN)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

GIB = 2**30


def video(bit_rate: int | None = None) -> ProbeStream:
    return ProbeStream(index=0, codec="h264", codec_type="video", bit_rate=bit_rate)


def context(*, duration_s: float = 3000.0, streams: list[ProbeStream]) -> PluginContext:
    return PluginContext(
        original_filename="movie.mkv",
        probe=ProbeData(
            file_path="/media/fixture.mkv", container="mkv", duration_s=duration_s, size_bytes=4 * GIB, streams=streams
        ),
    )


def test_stream_bitrate_facets() -> None:
    decision = module.decide(context(streams=[video(bit_rate=8_000_000)]), module.Inputs())
    assert decision == {
        "type": "match",
        "facets": {
            "avc_kbps": 6000,
            "avc_size_bytes": 2_250_000_000,
            "hevc_kbps": 4000,
            "hevc_size_bytes": 1_500_000_000,
            "av1_kbps": 3600,
            "av1_size_bytes": 1_350_000_000,
        },
    }


def test_estimated_bitrate_facets() -> None:
    decision = module.decide(context(streams=[video()]), module.Inputs())
    assert decision == {
        "type": "match",
        "facets": {
            "avc_kbps": 8590,
            "avc_size_bytes": 3_221_250_000,
            "hevc_kbps": 5727,
            "hevc_size_bytes": 2_147_625_000,
            "av1_kbps": 5154,
            "av1_size_bytes": 1_932_750_000,
        },
    }


def test_ratio_overrides() -> None:
    decision = module.decide(
        context(streams=[video(bit_rate=8_000_000)]), module.Inputs(avc_ratio=0.6, hevc_ratio=0.4, av1_ratio=0.3)
    )
    assert decision["facets"] == {
        "avc_kbps": 4800,
        "avc_size_bytes": 1_800_000_000,
        "hevc_kbps": 3200,
        "hevc_size_bytes": 1_200_000_000,
        "av1_kbps": 2400,
        "av1_size_bytes": 900_000_000,
    }


def test_skip_no_video() -> None:
    ctx = context(streams=[ProbeStream(index=1, codec="ac3", codec_type="audio")])
    assert module.decide(ctx, module.Inputs())["type"] == "skip"


def test_skip_no_bitrate() -> None:
    decision = module.decide(context(duration_s=0.0, streams=[video()]), module.Inputs())
    assert decision["type"] == "skip"


def test_container_rate_rounding() -> None:
    ctx = context(streams=[video(bit_rate=2_000_000)])
    ctx.probe.raw = {"format": {"bit_rate": "3001500"}}
    decision = module.decide(ctx, module.Inputs(source="container", hevc_ratio=0.5))
    assert decision["facets"]["hevc_kbps"] == 1501


@pytest.mark.parametrize(
    ("video_rate", "container_rate", "expected"),
    [(4_000_000, "6000000", 2500), (None, "6000000", 3000), (4_000_000, None, 2000)],
)
def test_average_rate(video_rate: int | None, container_rate: str | None, expected: int) -> None:
    ctx = context(duration_s=0, streams=[video(bit_rate=video_rate)])
    ctx.probe.raw = {"format": {"bit_rate": container_rate}}
    decision = module.decide(ctx, module.Inputs(source="average", hevc_ratio=0.5))
    plan = FFmpegPlan(
        video=VideoTarget(
            encoder="libx265",
            rate=RateControl(mode=RateControlMode.BITRATE, bitrate_source="average", from_source_bitrate=0.5),
        )
    )
    argv = render(plan, ctx.probe, "input.mkv", "output.mkv").argv
    assert decision["facets"]["hevc_kbps"] == expected
    assert argv[argv.index("-b:v:0") + 1] == f"{expected}k"
