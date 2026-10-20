from __future__ import annotations

from enum import StrEnum


class WebhookType(StrEnum):
    SONARR = "sonarr"
    RADARR = "radarr"
    GENERIC = "generic"


class JobStatus(StrEnum):
    PENDING = "pending"
    STAGING = "staging"
    HOLD = "hold"
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ExecutionLocation(StrEnum):
    LOCAL = "local"
    NODE = "node"
    NODE_LOCAL = "node_local"


class ExecutionResult(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


class HealthStatus(StrEnum):
    PENDING = "pending"
    HEALTHY = "healthy"
    CORRUPT = "corrupt"


class TranscodeStatus(StrEnum):
    PENDING = "pending"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    NOT_NEEDED = "not_needed"
    TRANSCODED = "transcoded"


class Decision(StrEnum):
    MATCH = "match"
    VETO = "veto"
    SKIP = "skip"


class StreamOutcome(StrEnum):
    COPY = "copy"
    TRANSCODE = "transcode"
    DROP = "drop"
    RETAG = "retag"


class WorkerTag(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class HdrFormat(StrEnum):
    HDR10 = "hdr10"
    DOLBY_VISION = "dolby_vision"
    HLG = "hlg"
    SDR = "sdr"


class BitrateSource(StrEnum):
    STREAM = "stream"
    ESTIMATED = "estimated"


class RateControlMode(StrEnum):
    QUALITY = "quality"
    BITRATE = "bitrate"
