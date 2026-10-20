from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from common.ctx import PluginContext
from common.media.probe import ProbeData, ProbeStream


PLUGIN = Path(__file__).parents[1] / "plugins/transcode.burn_subtitles/plugin.py"
spec = importlib.util.spec_from_file_location("burn_subtitles", PLUGIN)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def context(sidecars: list[str], video: bool = True) -> PluginContext:
    streams = [ProbeStream(index=0, codec="h264", codec_type="video")] if video else []
    return PluginContext(
        original_filename="movie.mkv",
        probe=ProbeData(file_path="/media/movie.mkv", duration_s=1, size_bytes=1, streams=streams),
        sidecar_subtitles=sidecars,
    )


def test_matches_naming_patterns() -> None:
    for filename in ("movie.ENG.SRT", "movie.srt", "eng.srt"):
        result = module.decide(context([filename]), module.Inputs())
        assert result["plan"].burn_subtitle.filename == filename


def test_later_stage_naming() -> None:
    ctx = context(["movie.eng.srt"])
    ctx.source_probe = ctx.probe.model_copy()
    ctx.probe = ctx.probe.model_copy(update={"file_path": "jobs/1/attempts/1/output.mkv"})
    result = module.decide(ctx, module.Inputs())
    assert result["plan"].burn_subtitle.filename == "movie.eng.srt"


def test_reorders_priorities() -> None:
    result = module.decide(context(["movie.ass", "movie.srt"]), module.Inputs(format_priority=["ass", "srt"]))
    assert result["plan"].burn_subtitle.filename == "movie.ass"
    result = module.decide(context(["eng.srt", "movie.srt"]), module.Inputs(name_priority=["language", "video"]))
    assert result["plan"].burn_subtitle.filename == "eng.srt"


def test_orders_across_patterns() -> None:
    result = module.decide(context(["movie.eng.ass", "movie.srt"]), module.Inputs(priority_order="format_first"))
    assert result["plan"].burn_subtitle.filename == "movie.srt"
    result = module.decide(context(["movie.eng.ass", "movie.srt"]), module.Inputs())
    assert result["plan"].burn_subtitle.filename == "movie.eng.ass"


def test_handles_ambiguity_and_missing() -> None:
    result = module.decide(context(["movie.srt", "movie.ass"]), module.Inputs(on_multiple="error"))
    assert result["type"] == "error"
    result = module.decide(context([]), module.Inputs(on_missing="error"))
    assert result["type"] == "error"


def test_deduplicates_same_basename() -> None:
    ctx = context(["eng.srt"])
    ctx.original_filename = "eng.mkv"
    result = module.decide(ctx, module.Inputs(on_multiple="error"))
    assert result["plan"].burn_subtitle.filename == "eng.srt"


def test_drops_subtitles_for_mp4() -> None:
    result = module.decide(context(["movie.srt"]), module.Inputs(container="mp4"))
    policy = result["plan"].streams.subtitle
    assert policy.keep_languages == []
    assert policy.keep_forced is False


def test_uses_nvenc_quality() -> None:
    result = module.decide(context(["movie.srt"]), module.Inputs(encoder="h264_nvenc"))
    assert result["plan"].video.rate.quality_option == "cq"


def test_rejects_unsafe_filename() -> None:
    result = module.decide(context(["../movie.srt"]), module.Inputs())
    assert result["type"] == "skip"
