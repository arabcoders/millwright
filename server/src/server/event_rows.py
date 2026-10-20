from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.enums import JobStatus
from common.media.probe import ProbeData
from pydantic import ValidationError
from sqlalchemy import func, select

from server.models import Job, JobAttempt, Library, MediaFile
from server.serialize import iso_utc

ACTIVE_FILE_JOB_STATUSES = (
    JobStatus.PENDING.value,
    JobStatus.STAGING.value,
    JobStatus.HOLD.value,
    JobStatus.QUEUED.value,
    JobStatus.CLAIMED.value,
    JobStatus.RUNNING.value,
    JobStatus.VERIFYING.value,
    JobStatus.FINALIZING.value,
)


FILE_TRANSCODE_STATUS = func.coalesce(
    select(Job.status)
    .where(
        Job.media_file_id == MediaFile.id,
        Job.kind == "transcode",
        Job.status.in_(ACTIVE_FILE_JOB_STATUSES),
    )
    .order_by(Job.id.desc())
    .limit(1)
    .correlate(MediaFile)
    .scalar_subquery(),
    MediaFile.transcode_status,
)


def media_file_row(file: MediaFile, active_job: Job | None = None, *, library: Library | None) -> dict:
    return {
        "id": file.id,
        "library_id": file.library_id,
        "path": file.path,
        "size": file.size,
        "modified_at": iso_utc(datetime.fromtimestamp(file.mtime_ns / 1_000_000_000, UTC)),
        "cooldown_started_at": iso_utc(file.first_seen) if library is not None and library.cooldown_s > 0 else None,
        "cooldown_until": (
            iso_utc(file.first_seen + timedelta(seconds=library.cooldown_s))
            if library is not None and library.cooldown_s > 0
            else None
        ),
        "created_at": iso_utc(file.created_at),
        "updated_at": iso_utc(file.updated_at),
        "container": file.container,
        "video_codec": file.video_codec,
        "width": file.width,
        "height": file.height,
        "duration_s": file.duration_s,
        "bit_rate": file.bit_rate,
        "medium": file.medium,
        "transcode_status": (
            active_job.status if active_job is not None and active_job.kind == "transcode" else file.transcode_status
        ),
        "health_status": file.health_status,
        "active_job_kind": active_job.kind if active_job is not None else None,
        "active_job_status": active_job.status if active_job is not None else None,
        "active_job_id": active_job.id if active_job is not None else None,
        "active_job_progress": active_job.progress if active_job is not None else None,
        "active_job_updated_at": iso_utc(active_job.updated_at) if active_job is not None else None,
    }


def file_summary(file: MediaFile) -> dict:
    return {
        name: getattr(file, name)
        for name in ("id", "library_id", "path", "size", "container", "video_codec", "width", "height", "duration_s")
    }


def job_row(job: Job, file: MediaFile | None = None, attempt: JobAttempt | None = None) -> dict:
    return {
        "id": job.id,
        "media_file_id": job.media_file_id,
        "library_id": job.library_id,
        "kind": job.kind,
        "status": job.status,
        "priority": job.priority,
        "attempts": job.attempts,
        "node_id": job.node_id,
        "error": job.error,
        "warning": job.warning,
        "cooldown_started_at": (
            iso_utc(file.first_seen) if job.status == JobStatus.HOLD.value and file is not None else None
        ),
        "hold_until": iso_utc(job.hold_until),
        "created_at": iso_utc(job.created_at),
        "updated_at": iso_utc(job.updated_at),
        "started_at": iso_utc(job.started_at),
        "finished_at": iso_utc(job.finished_at),
        "progress": job.progress,
        "decision_log": job.decision_log,
        "file": file_summary(file) if file is not None else None,
        "node_name": attempt.node_name if attempt is not None else None,
        "latest_attempt": attempt_row(attempt) if attempt is not None else None,
    }


def attempt_row(attempt: JobAttempt) -> dict:
    fields = (
        "id",
        "job_id",
        "attempt_number",
        "node_id",
        "node_name",
        "capabilities",
        "workload",
        "status",
        "result",
        "error",
        "source_path",
        "output_path",
        "log_path",
        "argv",
        "argv_hash",
        "source_size",
        "output_size",
        "health",
        "action_results",
        "claimed_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )
    result = {name: getattr(attempt, name) for name in fields}
    for name in ("created_at", "updated_at", "claimed_at", "started_at", "finished_at"):
        result[name] = iso_utc(result[name])
    for name in ("input_probe", "output_probe"):
        value = getattr(attempt, name)
        try:
            probe = ProbeData.model_validate(value) if value is not None else None
        except ValidationError:
            probe = None
        result[name] = (
            None
            if probe is None
            else {
                "container": probe.container,
                "duration_s": probe.duration_s,
                "size_bytes": probe.size_bytes,
                "streams": [
                    {
                        "index": stream.index,
                        "codec": stream.codec,
                        "codec_type": stream.codec_type,
                        "language": stream.language,
                        "title": stream.title,
                        "fps": stream.fps,
                        "pix_fmt": stream.pix_fmt,
                        "profile": stream.profile,
                        "sample_rate": stream.sample_rate,
                        "channels": stream.channels,
                        "channel_layout": stream.channel_layout,
                        "bit_rate": stream.bit_rate,
                        "width": stream.width,
                        "height": stream.height,
                        "default": stream.disposition_default,
                        "forced": stream.disposition_forced,
                    }
                    for stream in probe.streams
                ],
            }
        )
    return result
