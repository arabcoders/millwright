from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

# Pydantic needs the Action union at runtime to resolve model annotations.
from common.actions import Action, ActionOutcome
from common.enums import ExecutionResult, WorkerTag
from common.media.probe import ProbeData
from common.plan.models import FFmpegPlan
from common.workspace import DEFAULT_HARD_SIZE_CEILING, DEFAULT_MIN_FREE_GB

PROTOCOL_VERSION = 1
NODE_LOG_ERROR_LIMIT = 16_384
NODE_LOG_TRACEBACK_LIMIT = 16_384


class _MessageBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = PROTOCOL_VERSION


def _workspace_path(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or "\x00" in value or "\\" in value or value.startswith("/") or "//" in value:
        msg = "workspace path must be a relative POSIX path"
        raise ValueError(msg)
    if any(part in {".", ".."} for part in value.split("/")):
        msg = "workspace path must remain beneath the root"
        raise ValueError(msg)
    return value


WorkspacePath = Annotated[str, Field(min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeCapabilities(_StrictModel):
    max_transcodes: int = Field(default=1, ge=1)
    max_healthchecks: int = Field(default=1, ge=0)
    max_mkv_stats: int = Field(default=1, ge=0)
    mkvpropedit: bool = False
    hostname: str = ""
    ffmpeg_version: str = ""
    encoders: list[str] = Field(default_factory=list)
    video_encoders: list[str] = Field(default_factory=list)
    audio_encoders: list[str] = Field(default_factory=list)
    decoders: list[str] = Field(default_factory=list)
    audio_decoders: list[str] = Field(default_factory=list)
    hwaccels: list[str] = Field(default_factory=list)
    worker_tags: list[str] = Field(default_factory=list)
    # Host facts are informational, not dispatch criteria.
    cpu_model: str = ""
    cpu_cores: int = 0
    memory_gb: float = 0.0
    gpus: list[str] = Field(default_factory=list)

    @field_validator("worker_tags")
    @classmethod
    def check_worker_tags(cls, value: list[str]) -> list[str]:
        known = {tag.value for tag in WorkerTag}
        for tag in value:
            if tag not in known:
                msg = f"unknown worker tag: {tag!r}"
                raise ValueError(msg)
        return value


class ActiveAttempt(_StrictModel):
    job_id: int = Field(gt=0)
    attempt_id: int = Field(gt=0)


class Hello(_MessageBase):
    type: Literal["hello"] = "hello"

    node_id: str | None = Field(default=None, min_length=1)
    token: str = Field(min_length=1)
    capabilities: NodeCapabilities = Field(default_factory=NodeCapabilities)
    active_attempts: list[ActiveAttempt]


class WorkspaceChallenge(_MessageBase):
    type: Literal["workspace_challenge"] = "workspace_challenge"

    challenge_id: str = Field(min_length=1)
    challenge_path: WorkspacePath
    response_path: WorkspacePath

    _validate_paths = field_validator("challenge_path", "response_path")(_workspace_path)


class WorkspaceResponse(_MessageBase):
    type: Literal["workspace_response"] = "workspace_response"

    challenge_id: str = Field(min_length=1)
    response_path: WorkspacePath
    workspace_ready: bool
    workspace_free_bytes: int = Field(ge=0)
    error: str | None = None

    _validate_path = field_validator("response_path")(_workspace_path)


class Enrolled(_MessageBase):
    type: Literal["enrolled"] = "enrolled"

    node_id: str = Field(min_length=1)
    token: str = Field(min_length=1)


class EnrollmentSaved(_MessageBase):
    type: Literal["enrollment_saved"] = "enrollment_saved"

    node_id: str = Field(min_length=1)


class GPUUsage(_StrictModel):
    name: str = Field(min_length=1)
    percent: float | None = Field(default=None, ge=0, le=100)


class Heartbeat(_MessageBase):
    type: Literal["heartbeat"] = "heartbeat"

    load: float | None = None
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    gpus: list[GPUUsage] = Field(default_factory=list)


class Progress(_MessageBase):
    type: Literal["progress"] = "progress"

    job_id: int
    attempt_id: int = Field(gt=0)
    percent: float | None = None
    fps: float | None = None
    speed: str | None = None
    out_time_us: int | None = None
    total_size: int | None = None
    phase: str | None = None


class HealthResult(_StrictModel):
    healthy: bool
    error_count: int = Field(default=0, ge=0)
    samples: list[str] = Field(default_factory=list)


class Finished(_MessageBase):
    type: Literal["finished"] = "finished"

    job_id: int
    attempt_id: int = Field(gt=0)
    kind: Literal["transcode", "healthcheck", "mkv_stats"] = "transcode"
    health: HealthResult | None = None
    raw_probe: dict | None = None
    result: ExecutionResult | Literal["stalled"]
    error: str | None = None
    output_path: WorkspacePath | None = None
    argv: list[str] = Field(default_factory=list)
    argv_hash: str = ""
    output_size: int | None = None
    source_size: int | None = None
    # Only successful, verified outputs carry a probe.
    output_probe: ProbeData | None = None
    action_results: list[ActionOutcome] = Field(default_factory=list)
    log_path: WorkspacePath

    _validate_path = field_validator("output_path")(_workspace_path)
    _validate_log_path = field_validator("log_path")(_workspace_path)

    @model_validator(mode="after")
    def check_result(self) -> Self:
        if self.result == "succeeded" and self.kind == "healthcheck":
            if self.health is None or self.output_path is not None or self.output_probe is not None:
                raise ValueError("Successful healthchecks require a health result and no output file.")
        elif self.result == "succeeded":
            if self.output_path is None or self.output_probe is None:
                msg = "successful transcodes require output path and probe"
                raise ValueError(msg)
        elif self.output_path is not None or self.output_probe is not None:
            msg = "unsuccessful reports must not report output"
            raise ValueError(msg)
        if self.result != "succeeded" and self.health is not None:
            raise ValueError("Unsuccessful reports must not carry a health result.")
        if self.output_probe is not None:
            _workspace_path(self.output_probe.file_path)
            if self.output_path != self.output_probe.file_path:
                msg = "output probe path must match output path"
                raise ValueError(msg)
        return self


class Welcome(_MessageBase):
    type: Literal["welcome"] = "welcome"

    timezone: str = Field(min_length=1)
    active_attempts: list[ActiveAttempt]
    heartbeat_s: float = 15.0
    lease_ttl_s: float = 300.0


class OfferedJob(_StrictModel):
    job_id: int
    attempt_id: int = Field(gt=0)
    kind: Literal["transcode", "healthcheck", "mkv_stats"] = "transcode"
    healthcheck_hwaccel: bool = True
    statistics_edit: bool = True
    source_path: WorkspacePath
    output_path: WorkspacePath
    log_path: WorkspacePath
    subtitle_path: WorkspacePath | None = None
    plan: FFmpegPlan | None = None
    probe: ProbeData
    # Server policy checks pre-phase actions. The node reports their outcomes in Finished.
    actions: list[Action] = Field(default_factory=list)
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    hard_size_ceiling: float = DEFAULT_HARD_SIZE_CEILING
    stall_timeout_s: float = 600.0

    _validate_paths = field_validator("source_path", "output_path", "log_path", "subtitle_path")(_workspace_path)

    @model_validator(mode="after")
    def check_probe(self) -> Self:
        if self.kind == "transcode" and self.plan is None:
            raise ValueError("Transcodes require a plan.")
        if (self.plan is not None and self.plan.burn_subtitle is not None) != (self.subtitle_path is not None):
            msg = "subtitle path must match burn subtitle plan"
            raise ValueError(msg)
        _workspace_path(self.probe.file_path)
        if self.probe.file_path != self.source_path:
            msg = "probe path must match source path"
            raise ValueError(msg)
        return self


class JobOffer(_MessageBase):
    type: Literal["job"] = "job"

    job: OfferedJob


class Cancel(_MessageBase):
    type: Literal["cancel"] = "cancel"

    job_id: int
    attempt_id: int = Field(gt=0)
    reason: str = ""
    log_path: WorkspacePath | None

    _validate_path = field_validator("log_path")(_workspace_path)


class AttemptMissing(_MessageBase):
    type: Literal["attempt_missing"] = "attempt_missing"

    job_id: int = Field(gt=0)
    attempt_id: int = Field(gt=0)
    log_path: WorkspacePath | None

    _validate_path = field_validator("log_path")(_workspace_path)


class SetLimits(_MessageBase):
    type: Literal["set_limits"] = "set_limits"

    max_transcodes: int = Field(gt=0)
    max_healthchecks: int = Field(ge=0)
    max_mkv_stats: int = Field(ge=0)


class ActionTask(_MessageBase):
    """Post-phase actions dispatched to the node that finished the job."""

    type: Literal["action_task"] = "action_task"

    job_id: int
    attempt_id: int = Field(gt=0)
    path: WorkspacePath
    probe: ProbeData | None = None
    actions: list[Action] = Field(default_factory=list)

    _validate_path = field_validator("path")(_workspace_path)

    @model_validator(mode="after")
    def check_probe(self) -> Self:
        if self.probe is not None:
            _workspace_path(self.probe.file_path)
            if self.probe.file_path != self.path:
                msg = "action probe path must match action path"
                raise ValueError(msg)
        return self


class ActionReport(_MessageBase):
    type: Literal["action_report"] = "action_report"

    job_id: int
    attempt_id: int = Field(gt=0)
    results: list[ActionOutcome] = Field(default_factory=list)


class NodeLog(_StrictModel):
    logger: str = Field(pattern=r"^node\.[a-z0-9_.-]+$", max_length=80)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    message: str = Field(min_length=1, max_length=2048)
    job_id: int | None = None
    attempt_id: int | None = Field(default=None, gt=0)
    stage: str | None = Field(default=None, max_length=40)
    outcome: str | None = Field(default=None, max_length=40)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    count: int | None = Field(default=None, ge=0, le=1_000_000)
    error_class: str | None = Field(default=None, max_length=80)
    error: str | None = Field(default=None, max_length=NODE_LOG_ERROR_LIMIT)
    traceback: str | None = Field(default=None, max_length=NODE_LOG_TRACEBACK_LIMIT)


class NodeLogs(_MessageBase):
    type: Literal["node_logs"] = "node_logs"
    entries: list[NodeLog] = Field(min_length=1, max_length=50)


class Error(_MessageBase):
    type: Literal["error"] = "error"

    code: str
    message: str = ""


Message = Annotated[
    Hello
    | WorkspaceChallenge
    | WorkspaceResponse
    | Enrolled
    | EnrollmentSaved
    | Heartbeat
    | Progress
    | Finished
    | Welcome
    | JobOffer
    | Cancel
    | AttemptMissing
    | SetLimits
    | ActionTask
    | ActionReport
    | NodeLogs
    | Error,
    Field(discriminator="type"),
]

_MESSAGE_MODELS = (
    Hello,
    WorkspaceChallenge,
    WorkspaceResponse,
    Enrolled,
    EnrollmentSaved,
    Heartbeat,
    Progress,
    Finished,
    Welcome,
    JobOffer,
    Cancel,
    AttemptMissing,
    SetLimits,
    ActionTask,
    ActionReport,
    NodeLogs,
    Error,
)
_MESSAGE_TYPES = frozenset(model.model_fields["type"].default for model in _MESSAGE_MODELS)
_ADAPTER = TypeAdapter(Message)


def parse_message(data: dict | str) -> Message:
    if isinstance(data, str):
        data = json.loads(data)
    version = data.get("v")
    if version != PROTOCOL_VERSION:
        msg = f"unsupported protocol version: {version!r}"
        raise ValueError(msg)
    if data.get("type") not in _MESSAGE_TYPES:
        msg = f"unknown message type: {data.get('type')!r}"
        raise ValueError(msg)
    return _ADAPTER.validate_python(data)
