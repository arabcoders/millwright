from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.media.probe import ProbeData, ProbeStream
from common.plan.models import FFmpegPlan, RateControl, SubtitleBurn, VideoTarget
from common.protocol import (
    ActionReport,
    ActionTask,
    ActiveAttempt,
    AttemptMissing,
    Finished,
    Heartbeat,
    Hello,
    JobOffer,
    NodeCapabilities,
    OfferedJob,
    Progress,
    Welcome,
    WorkspaceChallenge,
    WorkspaceResponse,
    parse_message,
)

PLAN = FFmpegPlan(video=VideoTarget(encoder="libx264", rate=RateControl(quality=21, quality_option="crf")))
PROBE = ProbeData(
    file_path="jobs/1/source.mkv",
    duration_s=120.0,
    size_bytes=1_000_000,
    streams=[ProbeStream(index=0, codec="h264", codec_type="video", language="und")],
)
LOG = "jobs/1/attempts/a/ffmpeg.log"


def test_welcome_timezone_roundtrip() -> None:
    message = Welcome(timezone="Asia/Kuwait", active_attempts=[], heartbeat_s=10, lease_ttl_s=60)
    assert parse_message(message.model_dump_json()) == message
    assert message.v == 1
    with pytest.raises(ValidationError) as failure:
        parse_message(message.model_dump(mode="json", exclude={"timezone"}))
    assert [(error["loc"], error["type"]) for error in failure.value.errors()] == [(("welcome", "timezone"), "missing")]


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_protocol_mismatch_rejected(version: object) -> None:
    payload = Hello(token="token", active_attempts=[]).model_dump(mode="json")
    if version is None:
        del payload["v"]
    else:
        payload["v"] = version
    with pytest.raises(ValueError):
        parse_message(payload)


def test_workspace_roundtrip() -> None:
    message = WorkspaceChallenge(challenge_id="c1", challenge_path="jobs/1/challenge", response_path="jobs/1/response")
    assert parse_message(message.model_dump_json()) == message
    response = WorkspaceResponse(
        challenge_id="c1", response_path="jobs/1/response", workspace_ready=True, workspace_free_bytes=42
    )
    assert parse_message(response.model_dump_json()) == response


def test_gpu_usage_roundtrip() -> None:
    message = Heartbeat(
        cpu_percent=42,
        gpus=[{"name": "Intel GPU", "percent": 35}, {"name": "NVIDIA RTX", "percent": None}],
    )
    assert parse_message(message.model_dump_json()) == message
    with pytest.raises(ValidationError):
        Heartbeat(gpus=[{"name": "Intel GPU", "percent": 101}])


def test_execution_attempts_roundtrip() -> None:
    messages = [
        Progress(job_id=1, attempt_id=1, percent=50),
        Finished(
            job_id=1,
            attempt_id=1,
            result="succeeded",
            output_path="jobs/1/output.mkv",
            argv=["ffmpeg", "-i", "source.mkv"],
            argv_hash="hash",
            source_size=1,
            output_size=2,
            output_probe=PROBE.model_copy(update={"file_path": "jobs/1/output.mkv"}),
            log_path=LOG,
        ),
    ]
    for message in messages:
        assert parse_message(json.loads(message.model_dump_json())) == message


def test_finished_null() -> None:
    message = Finished(
        job_id=1,
        attempt_id=1,
        result="failed",
        error="probe failed",
        log_path=LOG,
    )
    payload = message.model_dump(mode="json")
    assert payload["output_path"] is None
    assert parse_message(payload) == message


def test_offer_paths_roundtrip() -> None:
    job = OfferedJob(
        job_id=1,
        attempt_id=1,
        source_path="jobs/1/source.mkv",
        output_path="jobs/1/output.mkv",
        log_path="jobs/1/attempts/a/ffmpeg.log",
        plan=PLAN,
        probe=PROBE,
    )
    parsed = parse_message({"v": 1, "type": "job", "job": json.loads(job.model_dump_json())})
    assert isinstance(parsed, JobOffer)
    assert parsed.job == job


def test_offer_subtitle_requires_path() -> None:
    with pytest.raises(ValidationError):
        OfferedJob(
            job_id=1,
            attempt_id=1,
            source_path="jobs/1/source.mkv",
            output_path="jobs/1/output.mkv",
            log_path=LOG,
            plan=FFmpegPlan(video=VideoTarget(encoder="libx264"), burn_subtitle=SubtitleBurn(filename="captions.srt")),
            probe=PROBE,
        )


def test_offer_subtitle_path_safe() -> None:
    with pytest.raises(ValidationError):
        OfferedJob(
            job_id=1,
            attempt_id=1,
            source_path="jobs/1/source.mkv",
            output_path="jobs/1/output.mkv",
            log_path=LOG,
            subtitle_path="../captions.srt",
            plan=PLAN,
            probe=PROBE,
        )


@pytest.mark.parametrize(
    "value", ["", "/absolute", "../escape", "a/../b", "a/./b", "a\\b", "a//b", ".", "a\x00b", "C:\\file"]
)
def test_workspace_paths_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Finished(job_id=1, attempt_id=1, result="failed", log_path=value)


def test_paths_ignore_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "jobs").symlink_to(unrelated, target_is_directory=True)
    monkeypatch.chdir(cwd)
    message = Finished(job_id=1, attempt_id=1, result="failed", log_path=LOG)
    assert parse_message(message.model_dump_json()) == message


def test_capability_values_roundtrip() -> None:
    capabilities = NodeCapabilities(
        max_transcodes=3,
        hostname="worker-1",
        encoders=["aac", "libx264"],
        video_encoders=["libx264"],
        audio_encoders=["aac"],
        decoders=["aac", "h264"],
        audio_decoders=["aac"],
    )
    message = Hello(node_id="n", token="t", capabilities=capabilities, active_attempts=[])
    assert parse_message(message.model_dump_json()) == message


@pytest.mark.parametrize("field", ["max_transcodes"])
def test_capability_zero_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        NodeCapabilities.model_validate({field: 0})


def test_missing_attempt_rejected() -> None:
    with pytest.raises(ValidationError):
        Progress.model_validate({"v": 1, "type": "progress", "job_id": 1})


def test_active_attempts_roundtrip() -> None:
    message = Hello(token="t", active_attempts=[ActiveAttempt(job_id=7, attempt_id=12)])
    assert parse_message(message.model_dump_json()) == message
    welcome = Welcome(timezone="UTC", active_attempts=message.active_attempts)
    assert parse_message(welcome.model_dump_json()) == welcome
    missing = AttemptMissing(job_id=7, attempt_id=12, log_path=LOG)
    assert parse_message(missing.model_dump_json()) == missing


def test_inventory_required() -> None:
    with pytest.raises(ValidationError):
        Hello.model_validate({"token": "t"})
    with pytest.raises(ValidationError) as failure:
        Welcome.model_validate({"timezone": "UTC"})
    assert any(error["loc"] == ("active_attempts",) and error["type"] == "missing" for error in failure.value.errors())


def test_task_attempt() -> None:
    with pytest.raises(ValidationError):
        ActionTask.model_validate({"v": 1, "type": "action_task", "job_id": 1, "path": "jobs/1/output.mkv"})


@pytest.mark.parametrize("has_probe", [False, True])
def test_action_probe_roundtrip(has_probe: bool) -> None:
    path = "jobs/1/output.mkv"
    probe = PROBE.model_copy(update={"file_path": path}) if has_probe else None
    task = ActionTask(job_id=1, attempt_id=1, path=path, probe=probe)
    assert parse_message(task.model_dump_json()) == task


@pytest.mark.parametrize("path", ["/library/movie.mkv", "../escape", "jobs/1/source.mkv"])
def test_action_probe_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        ActionTask(
            job_id=1,
            attempt_id=1,
            path="jobs/1/output.mkv",
            probe=PROBE.model_copy(update={"file_path": path}),
        )


def test_report_attempt() -> None:
    with pytest.raises(ValidationError):
        ActionReport.model_validate({"v": 1, "type": "action_report", "job_id": 1, "results": []})


def test_worker_tag_vocab() -> None:
    with pytest.raises(ValidationError):
        NodeCapabilities(worker_tags=["tpu"])
