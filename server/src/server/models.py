from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from common.enums import HealthStatus, JobStatus, TranscodeStatus
from pydantic import JsonValue  # noqa: TC002 -- SQLAlchemy resolves mapped annotations at runtime.
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.current_timestamp()
    )


class KeyValue(Base):
    __tablename__ = "key_values"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True)
    value: Mapped[JsonValue] = mapped_column(JSONB, nullable=False)


class WebhookRegistration(Base):
    __tablename__ = "webhook_registrations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String)
    api_key: Mapped[str] = mapped_column(String, unique=True)
    path_replacements: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list, server_default="[]")


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    roots: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    # Wait this many seconds after first_seen before dispatch. Source changes
    # reset first_seen. Zero disables the cooldown.
    cooldown_s: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    schedule: Mapped[dict | None] = mapped_column(JSONB)
    watch: Mapped[bool] = mapped_column(default=False, server_default=false())
    suspended: Mapped[bool] = mapped_column(default=False, server_default=false())
    deleted: Mapped[bool] = mapped_column(default=False, server_default=false())
    delete_error: Mapped[str | None] = mapped_column(Text)
    scan_on_start: Mapped[bool] = mapped_column(default=False, server_default=false())
    scan_id: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    scan_interval_s: Mapped[int | None]
    # Rewrite MKV track statistics tags when probes lack per-stream bitrates.
    mkv_stats: Mapped[bool] = mapped_column(default=False, server_default=false())
    healthcheck_enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    processing_cutoff: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    trash_mode: Mapped[Literal["none", "transcode", "all"]] = mapped_column(
        String, default="none", server_default="none"
    )
    trash_retention_days: Mapped[int] = mapped_column(default=30, server_default=text("30"))
    flow_id: Mapped[int | None] = mapped_column(ForeignKey("flows.id"))


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_files_library_seen_id", "library_id", "last_seen_scan_id", "id"),
        Index(
            "ix_media_files_pending_metadata",
            "metadata_priority",
            "id",
            postgresql_where=text("probe_id IS NULL AND health_status = 'pending' AND metadata_cancelled IS FALSE"),
        ),
        Index("ix_media_files_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id"), index=True)
    last_seen_scan_id: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Globally unique: a file belongs to exactly one library index entry.
    path: Mapped[str] = mapped_column(String, unique=True)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    source_identity: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    # Set when the scanner adds the row. Reset whenever the source changes.
    # The churn cooldown compares dispatch time against this stamp.
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default="1970-01-01 00:00:00+00"
    )
    container: Mapped[str] = mapped_column(index=True)
    video_codec: Mapped[str | None] = mapped_column(index=True)
    width: Mapped[int | None]
    height: Mapped[int | None]
    resolution_bucket: Mapped[str | None] = mapped_column(index=True)
    duration_s: Mapped[float | None]
    bit_rate: Mapped[int | None] = mapped_column(BigInteger)
    medium: Mapped[str] = mapped_column(index=True)
    transcode_status: Mapped[str] = mapped_column(default=TranscodeStatus.PENDING.value)
    transcode_decided: Mapped[bool] = mapped_column(default=False, server_default=false())
    health_status: Mapped[str] = mapped_column(default=HealthStatus.PENDING.value)
    # Path and size associated with the stored healthcheck result.
    health_source: Mapped[dict | None] = mapped_column(JSONB)
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Cyclic FK with probes.media_file_id. use_alter keeps create_all ordering valid.
    probe_id: Mapped[int | None] = mapped_column(
        ForeignKey("probes.id", use_alter=True, name="fk_media_files_probe_id")
    )
    # Cancellation outlives removable job history for this source generation.
    metadata_cancelled: Mapped[bool] = mapped_column(default=False, server_default=false())
    metadata_priority: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', regexp_replace(coalesce(path, '') || ' ' || coalesce(container, '') || ' ' || "
            "coalesce(video_codec, '') || ' ' || coalesce(resolution_bucket, '') || ' ' || coalesce(medium, ''), "
            "'[^[:alnum:]]+', ' ', 'g'))",
            persisted=True,
        ),
        deferred=True,
    )


class ProbeRow(Base):
    __tablename__ = "probes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    media_file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), unique=True)
    raw_json: Mapped[str] = mapped_column(Text)


class Flow(Base):
    __tablename__ = "flows"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    version: Mapped[int] = mapped_column(default=1)
    definition: Mapped[str] = mapped_column(Text)


class NodeRow(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String)
    capabilities: Mapped[dict] = mapped_column(JSONB, default=dict)
    workspace_ready: Mapped[bool] = mapped_column(default=False, server_default=false())
    workspace_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(default="offline")
    # Paused nodes stay connected and finish in-flight work. Dispatch skips them.
    paused: Mapped[bool] = mapped_column(default=False, server_default=false())
    max_concurrency: Mapped[int] = mapped_column(default=1)
    transcode_limit: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    healthcheck_limit: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    mkv_stats_limit: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    usage: Mapped[dict | None] = mapped_column(JSONB)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_hold_kind_until", "kind", "hold_until", "id", postgresql_where=text("status = 'hold'")),
        Index(
            "ix_jobs_local_queue",
            "kind",
            "priority",
            "created_at",
            "id",
            postgresql_where=text("status = 'queued' AND delete_requested IS FALSE"),
        ),
        Index("ix_jobs_status_priority_created_at", "status", "priority", "created_at"),
        # Avoid sorting the entire status bucket for each history page.
        Index("ix_jobs_status_created_at", "status", "created_at"),
        Index(
            "ix_jobs_dispatch_order",
            "kind",
            "priority",
            "library_id",
            "created_at",
            "id",
            "status",
            "node_id",
            postgresql_where=text("delete_requested IS FALSE"),
        ),
        Index("ix_jobs_finished_at", "finished_at"),
        Index(
            "uq_jobs_active_media_file",
            "media_file_id",
            unique=True,
            postgresql_where=text(
                "status IN ("
                + ", ".join(
                    f"'{status.value}'"
                    for status in JobStatus
                    if status
                    in {
                        JobStatus.PENDING,
                        JobStatus.HOLD,
                        JobStatus.STAGING,
                        JobStatus.QUEUED,
                        JobStatus.CLAIMED,
                        JobStatus.RUNNING,
                        JobStatus.VERIFYING,
                        JobStatus.FINALIZING,
                    }
                )
                + ") AND NOT (status = 'pending' AND waiting_metadata IS TRUE)"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    media_file_id: Mapped[int] = mapped_column(index=True)
    library_id: Mapped[int] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(default="transcode")
    priority: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    # Waiting requests must not block the metadata job they need.
    waiting_metadata: Mapped[bool] = mapped_column(default=False, server_default=false())
    delete_requested: Mapped[bool] = mapped_column(default=False, server_default=false(), index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cooldown hold: the job waits in `hold` until this instant, then stages.
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan: Mapped[dict | None] = mapped_column(JSONB)
    decision_log: Mapped[list | None] = mapped_column(JSONB)
    flow_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    # Pinned graph resume point for evaluation against the output probe.
    continuation: Mapped[dict | None] = mapped_column(JSONB)
    facets: Mapped[dict | None] = mapped_column(JSONB)
    # Evaluated pre-phase actions. Policy-checked again at dispatch time.
    pre_actions: Mapped[list | None] = mapped_column(JSONB)
    # Flow actions dispatched with the post pass.
    post_actions: Mapped[list | None] = mapped_column(JSONB)
    # Worker tags required by the flow's plugins. A node needs at least one to claim the job.
    requires: Mapped[list | None] = mapped_column(JSONB)
    # Encoders required by the flow's plugins. A node needs all of them to claim the job.
    requires_encoders: Mapped[list | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # Dispatch-time blocker a queued job is waiting on, e.g. no node with the plan's encoder.
    warning: Mapped[str | None] = mapped_column(Text)
    # Last 64 KiB of ffmpeg stderr from the node's finished report, for the UI.
    stderr_tail: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_type_job_id_attempt_id", "type", "job_id", "attempt_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("job_attempts.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (Index("ix_job_attempts_job_id_attempt_number", "job_id", "attempt_number", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt_number: Mapped[int]
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"), index=True)
    node_name: Mapped[str | None] = mapped_column(String)
    # Keep this after node removal to distinguish node work from local work.
    # Store local checks as SQL NULL, not JSON null.
    capabilities: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    workload: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    result: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(String)
    output_path: Mapped[str | None] = mapped_column(String)
    log_path: Mapped[str | None] = mapped_column(String)
    log_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    argv: Mapped[list | None] = mapped_column(JSONB)
    argv_hash: Mapped[str | None] = mapped_column(String)
    source_size: Mapped[int | None] = mapped_column(BigInteger)
    output_size: Mapped[int | None] = mapped_column(BigInteger)
    input_probe: Mapped[dict | None] = mapped_column(JSONB)
    output_probe: Mapped[dict | None] = mapped_column(JSONB)
    health: Mapped[dict | None] = mapped_column(JSONB)
    action_results: Mapped[list | None] = mapped_column(JSONB)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrashEntry(Base):
    __tablename__ = "trash_entries"

    row_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(16), unique=True)
    library_id: Mapped[int] = mapped_column(index=True)
    original_path: Mapped[str] = mapped_column(String)
    library_root: Mapped[str] = mapped_column(String)
    suffix: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    trashed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    purge_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TrashRestoreJournal(Base):
    __tablename__ = "trash_restore_journal"

    row_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True)
    entry_id: Mapped[str] = mapped_column(String(16), index=True)
    target_path: Mapped[str] = mapped_column(String)
    displaced_entry_id: Mapped[str | None] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String, index=True)


class FinalizationJournal(Base):
    __tablename__ = "finalization_journal"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("job_attempts.id"), unique=True, index=True)
    source_path: Mapped[str] = mapped_column(String)
    destination_path: Mapped[str] = mapped_column(String)
    partial_path: Mapped[str] = mapped_column(String)
    trash_path: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(String)


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", name="sessions_user_id_fkey"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Missing rows mean enabled. Disabled plugins are not loaded.
class PluginState(Base):
    __tablename__ = "plugin_states"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plugin_id: Mapped[str] = mapped_column(String, unique=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())


class NotificationTarget(Base):
    __tablename__ = "notification_targets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(Text)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sent', 'failed')", name="ck_notification_delivery_status"),
        Index("uq_notification_delivery_event_target", "job_event_id", "target_name", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_event_id: Mapped[int] = mapped_column(ForeignKey("job_events.id"), index=True)
    target_name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
