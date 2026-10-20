from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from common.ctx import PluginContext
from common.media.probe import ProbeData, ProbeStream

PLUGIN = Path(__file__).parents[1] / "plugins/inspector.stream_guard/plugin.py"
spec = importlib.util.spec_from_file_location("stream_guard", PLUGIN)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def stream(codec_type: str, language: str | None = None) -> ProbeStream:
    return ProbeStream(
        index=0, codec="aac" if codec_type == "audio" else "srt", codec_type=codec_type, language=language
    )


def context(*streams: ProbeStream) -> PluginContext:
    probe = ProbeData(file_path="/media/fixture.mkv", duration_s=1, size_bytes=1, streams=list(streams))
    return PluginContext(probe=probe, source_probe=probe, original_filename="movie.mkv")


def test_subtitle_only() -> None:
    decision = module.decide(context(stream("subtitle")), module.Inputs(min_audio=0, min_subtitles=1))
    assert decision == {"type": "match"}


def test_subtitle_language() -> None:
    ctx = context(stream("subtitle", "eng"), stream("subtitle", "ara"))
    decision = module.decide(ctx, module.Inputs(min_audio=0, min_subtitles=2, subtitle_languages=["eng"]))
    assert decision["type"] == "veto"


def test_both_requirements() -> None:
    decision = module.decide(context(stream("audio")), module.Inputs(min_subtitles=1))
    assert decision["type"] == "veto"


def test_language_aggregate() -> None:
    ctx = context(stream("audio", "eng"), stream("audio", "ara"), stream("audio", "jpn"))
    decision = module.decide(ctx, module.Inputs(min_audio=3, audio_languages=["eng", "ara"]))
    assert decision["type"] == "veto"


def test_language_aliases() -> None:
    ctx = context(stream("audio", "fra"), stream("subtitle", "fra"))
    inputs = module.Inputs(min_subtitles=1, audio_languages=["fre"], subtitle_languages=["fre"])

    assert module.decide(ctx, inputs) == {"type": "match"}


def test_unfiltered_unknown() -> None:
    decision = module.decide(context(stream("audio"), stream("audio", "und")), module.Inputs(min_audio=2))
    assert decision == {"type": "match"}


def test_unknown_language() -> None:
    ctx = context(stream("audio"), stream("audio", "und"))
    assert module.decide(ctx, module.Inputs(min_audio=2, audio_languages=["und"])) == {"type": "match"}


def test_missing_source() -> None:
    ctx = context()
    ctx.source_probe = None
    assert module.decide(ctx, module.Inputs())["type"] == "skip"
