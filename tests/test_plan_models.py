from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.plan import AudioPlanInputs, FFmpegPlan, RateControl, StreamSelection, render
from common.media.probe import ProbeData, ProbeStream


def test_rejects_bad_languages() -> None:
    for code in ("english", "EN"):
        with pytest.raises(ValidationError):
            StreamSelection(keep_languages=[code])


def test_rejects_bad_rates() -> None:
    with pytest.raises(ValidationError):
        RateControl.model_validate({"mode": "automatic"})
    for multiplier in (0, -0.5, 1.5):
        with pytest.raises(ValidationError):
            RateControl(from_source_bitrate=multiplier)
    for tolerance in (0, -0.1, 1.5):
        with pytest.raises(ValidationError):
            RateControl(tolerance=tolerance)


def test_rejects_bad_container() -> None:
    with pytest.raises(ValidationError):
        FFmpegPlan(container="avi")


def test_empty_audio_copies() -> None:
    inputs = AudioPlanInputs(audio_codec="", audio_bitrate="")
    probe = ProbeData(
        file_path="source.mkv",
        duration_s=60,
        size_bytes=100,
        streams=[ProbeStream(index=0, codec="ac3", codec_type="audio")],
    )
    argv = render(FFmpegPlan(audio_encode=inputs.audio_targets()), probe, "source.mkv", "output.mkv").argv
    assert inputs.audio_targets() == []
    assert argv[argv.index("-c") + 1] == "copy"
    assert "" not in argv
