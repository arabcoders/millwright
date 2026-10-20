from __future__ import annotations

from common.plan.models import AudioTarget, FFmpegPlan, StreamPolicy, StreamSelection, VideoTarget
from common.media.probe import ProbeData, ProbeStream
from common.protocol import NodeCapabilities
from server.nodehub.matching import can_execute, tags_match

PLAN = FFmpegPlan(video=VideoTarget(encoder="libx264"))
PROBE = ProbeData(file_path="movie.mkv", duration_s=1, size_bytes=1, streams=[])


def test_tags_match() -> None:
    assert tags_match([], ["cpu"]) is True
    assert tags_match(["gpu", "nvenc"], ["cpu", "nvenc"]) is True
    assert tags_match(["nvenc"], ["gpu", "cpu"]) is False


def test_workspace_not_ready() -> None:
    assert can_execute(NodeCapabilities(encoders=["libx264"]), False, PLAN, PROBE)[0] is False


def test_encoder_matching() -> None:
    assert can_execute(NodeCapabilities(encoders=[]), True, PLAN, PROBE)[0] is False
    assert can_execute(NodeCapabilities(encoders=["libx264"]), True, PLAN, PROBE) == (True, "")


def test_requirements_matching() -> None:
    caps = NodeCapabilities(encoders=["libx264", "hevc_nvenc"], worker_tags=["gpu"])
    assert can_execute(caps, True, PLAN, PROBE, ["cpu", "gpu"], ["libx264", "hevc_nvenc"]) == (True, "")
    assert can_execute(caps, True, PLAN, PROBE, ["cpu"], [])[0] is False
    assert can_execute(caps, True, PLAN, PROBE, [], ["av1"])[0] is False


def test_plan_required() -> None:
    assert can_execute(NodeCapabilities(), True, None, PROBE)[0] is False


def test_audio_encoder_matching() -> None:
    probe = PROBE.model_copy(
        update={
            "streams": [
                ProbeStream(index=0, codec="ac3", codec_type="audio", language="eng"),
                ProbeStream(index=1, codec="dts", codec_type="audio", language="spa"),
            ]
        }
    )
    plan = FFmpegPlan(audio_encode=[AudioTarget(codec="libfdk_aac", bitrate="128k", when_codec_in=["dts"])])
    assert can_execute(NodeCapabilities(), True, plan, probe)[0] is False
    assert can_execute(NodeCapabilities(encoders=["libfdk_aac"]), True, plan, probe) == (True, "")
    plan.streams = StreamPolicy(audio=StreamSelection(keep_languages=["eng"]))
    assert can_execute(NodeCapabilities(), True, plan, probe) == (True, "")
    plan.audio_encode = [AudioTarget(codec="libfdk_aac", bitrate="128k", when_codec_in=["ac3"])]
    plan.streams.audio = StreamSelection(keep_languages=[])
    assert can_execute(NodeCapabilities(), True, plan, probe)[0] is False
    plan.audio_encode = [AudioTarget(codec="copy", bitrate="128k")]
    assert can_execute(NodeCapabilities(), True, plan, probe) == (True, "")
