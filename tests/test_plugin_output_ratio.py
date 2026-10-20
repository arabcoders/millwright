from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from common.ctx import PluginContext
from common.media.probe import ProbeData, ProbeStream

PLUGIN = Path(__file__).parents[1] / "plugins/inspector.output_ratio/plugin.py"
spec = importlib.util.spec_from_file_location("output_ratio", PLUGIN)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def probe(*, size_bytes: int, duration_s: float = 3000.0, bit_rate: int | None = None) -> ProbeData:
    return ProbeData(
        file_path="/media/fixture.mkv",
        container="mkv",
        duration_s=duration_s,
        size_bytes=size_bytes,
        streams=[ProbeStream(index=0, codec="h264", codec_type="video", bit_rate=bit_rate)],
    )


def test_match_above_ratio() -> None:
    ctx = PluginContext(
        probe=probe(size_bytes=6 * 2**30), source_probe=probe(size_bytes=10 * 2**30), original_filename="movie.mkv"
    )
    assert module.decide(ctx, module.Inputs()) == {"type": "match"}


def test_veto_below_ratio() -> None:
    ctx = PluginContext(
        probe=probe(size_bytes=2 * 2**30), source_probe=probe(size_bytes=10 * 2**30), original_filename="movie.mkv"
    )
    assert module.decide(ctx, module.Inputs())["type"] == "veto"


def test_bitrate_stream_rates() -> None:
    ctx = PluginContext(
        original_filename="movie.mkv",
        probe=probe(size_bytes=2**30, bit_rate=4_000_000),
        source_probe=probe(size_bytes=2**30, bit_rate=8_000_000),
    )
    assert module.decide(ctx, module.Inputs(metric="bitrate")) == {"type": "match"}


def test_veto_undeterminable_bitrate() -> None:
    ctx = PluginContext(
        original_filename="movie.mkv",
        probe=probe(size_bytes=2**30, bit_rate=4_000_000),
        source_probe=probe(size_bytes=2**30, duration_s=0.0),
    )
    assert module.decide(ctx, module.Inputs(metric="bitrate"))["type"] == "veto"


def test_skip_no_probe() -> None:
    ctx = PluginContext(probe=probe(size_bytes=2**30), original_filename="movie.mkv")
    assert module.decide(ctx, module.Inputs())["type"] == "skip"
