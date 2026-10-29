from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from functools import partial
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from common.fs import identity
from common.fs.acquire import DIRECTORY_FLAGS, directory_fd, entry_stat, private_file
from common.fs.effects import copy_file
from common.process import drain
from common.workspace import resolve_workspace_path, workspace_lock

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Generator
    from typing import BinaryIO

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement

    from server.models import JobAttempt

LOG = logging.getLogger(__name__)
FINISHED_FINALIZATION_STATES = ("completed", "discard_completed", "rolled_back", "discard_rolled_back")
_SAFE_RMTREE = shutil.rmtree.avoids_symlink_attacks
_WORKSPACE_BATCH = 100


class _StagingConflictError(ValueError):
    pass


def _walk(fd: int, relative: str, files: list[dict], stopped: threading.Event) -> float:
    latest = os.fstat(fd).st_mtime
    with os.scandir(fd) as entries:
        for entry in entries:
            if stopped.is_set():
                raise InterruptedError("workspace sampling stopped")
            if entry.name == ".lock":
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            latest = max(latest, info.st_mtime)
            path = f"{relative}/{entry.name}"
            if stat.S_ISDIR(info.st_mode):
                try:
                    child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    continue
                try:
                    latest = max(latest, _walk(child, path, files, stopped))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                files.append(
                    {
                        "path": path,
                        "size_bytes": info.st_size,
                        "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                    }
                )
            else:
                raise ValueError("workspace contains a symlink or non-regular entry")
    return latest


def inspect_directory(jobs_fd: int, job_id: int, stopped: threading.Event) -> dict:
    if type(job_id) is not int or job_id < 0:
        raise ValueError("invalid workspace identifier")
    files: list[dict] = []
    fd = os.open(str(job_id), DIRECTORY_FLAGS, dir_fd=jobs_fd)
    try:
        latest = _walk(fd, f"jobs/{job_id}", files, stopped)
    finally:
        os.close(fd)
    source = [
        file
        for file in files
        if Path(file["path"]).parent == Path(f"jobs/{job_id}") and Path(file["path"]).name.startswith("source.")
    ]
    outputs = [file for file in files if Path(file["path"]).name.startswith(("output.", "output-"))]
    return {
        "workspace_id": job_id,
        "size_bytes": sum(file["size_bytes"] for file in files),
        "file_count": len(files),
        "source_bytes": sum(file["size_bytes"] for file in source) if source else None,
        "output_bytes": sum(file["size_bytes"] for file in outputs) if outputs else None,
        "last_activity_at": datetime.fromtimestamp(latest, UTC).isoformat(),
        "files": sorted(files, key=lambda file: file["path"]),
        "error": None,
    }


def post_action_jobs(*, scoped: bool = False) -> Select[int]:
    from sqlalchemy import select

    from server.models import Job

    return select(Job.id).where(_post_action_pending(scoped=scoped))


def _post_action_pending(*, scoped: bool) -> ColumnElement[bool]:
    from sqlalchemy import and_, exists, func, or_, select
    from sqlalchemy.orm import aliased

    from server.models import Job, JobAttempt, JobEvent

    pending = aliased(JobEvent)
    finished = aliased(JobEvent)
    requested = aliased(JobEvent)
    retrying_success = exists(
        select(requested.id).where(
            requested.job_id == Job.id,
            requested.type == "requested",
            requested.payload["operation"].as_string() == "retry",
            requested.payload["previous"]["status"].as_string() == "succeeded",
            requested.id
            == select(func.max(JobEvent.id))
            .where(JobEvent.job_id == Job.id, JobEvent.type == "requested")
            .correlate(Job)
            .scalar_subquery(),
        )
    )
    completed = select(finished.id).where(
        finished.job_id == pending.job_id,
        finished.attempt_id.is_not_distinct_from(pending.attempt_id),
        finished.type == "actions_finished",
        finished.id > pending.id,
    )
    pending_jobs = select(pending.job_id).where(pending.type == "actions_pending", ~exists(completed))
    current = (
        select(JobAttempt.id)
        .where(JobAttempt.job_id == Job.id, JobAttempt.attempt_number == Job.attempts)
        .correlate(Job)
        .scalar_subquery()
    )
    recorded = (
        select(JobEvent.id)
        .where(
            JobEvent.job_id == Job.id,
            JobEvent.attempt_id.is_not_distinct_from(current),
            JobEvent.type == "actions_finished",
        )
        .correlate(Job)
    )
    deferred = (
        select(JobEvent.id)
        .where(
            JobEvent.job_id == Job.id,
            JobEvent.attempt_id.is_not_distinct_from(current),
            or_(
                and_(JobEvent.type == "post_actions", JobEvent.payload["actions"][0].is_not(None)),
                and_(JobEvent.type == "stage", JobEvent.payload["post_actions"][0].is_not(None)),
            ),
        )
        .correlate(Job)
    )
    unrecorded = and_(
        or_(Job.status == "succeeded", and_(Job.status == "pending", retrying_success)),
        or_(Job.post_actions[0].is_not(None), exists(deferred)),
        ~exists(recorded),
    )
    # Global membership queries need indexed candidate sets. Bounded lookups need
    # correlated records so they do not enumerate other sources' history.
    if scoped:
        return or_(exists(pending_jobs.where(pending.job_id == Job.id).correlate(Job)), unrecorded)
    return Job.id.in_(pending_jobs.union(select(Job.id).where(unrecorded)))


def recovery_jobs(
    *, include_actions: bool = True, include_artifact_checks: bool = True, scoped: bool = False
) -> Select[int]:
    from common.enums import JobStatus
    from sqlalchemy import and_, exists, or_, select
    from sqlalchemy.orm import aliased

    from server.models import FinalizationJournal, Job, JobAttempt, JobEvent

    journals = select(FinalizationJournal.attempt_id).where(
        FinalizationJournal.state.not_in(FINISHED_FINALIZATION_STATES)
    )
    attempts = select(JobAttempt.job_id).where(
        or_(
            and_(
                JobAttempt.workload == "healthcheck",
                JobAttempt.capabilities.is_(None),
                or_(
                    and_(
                        JobAttempt.result == "interrupted",
                        or_(
                            JobAttempt.log_data.is_(None),
                            JobAttempt.finished_at.is_(None),
                            JobAttempt.error.not_in(["requeued", "max attempts exceeded"]),
                        ),
                    ),
                    and_(JobAttempt.log_path.is_not(None), JobAttempt.log_data.is_(None)),
                    JobAttempt.status.not_in(
                        [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED]
                    ),
                ),
            ),
            exists(journals.where(FinalizationJournal.attempt_id == JobAttempt.id))
            if scoped
            else JobAttempt.id.in_(journals),
        ),
    )
    if scoped:
        attempts = attempts.where(JobAttempt.job_id == Job.id).correlate(Job)
    else:
        attempts = attempts.join(Job, Job.id == JobAttempt.job_id)
    newer = aliased(Job)
    artifact_checks = (
        select(JobEvent.job_id)
        .join(JobAttempt, JobAttempt.id == JobEvent.attempt_id)
        .where(
            JobEvent.type == "artifact_check_pending",
            JobEvent.job_id == Job.id,
            Job.kind == "transcode",
            Job.status == JobStatus.SUCCEEDED.value,
            JobAttempt.job_id == Job.id,
            JobAttempt.status == JobStatus.SUCCEEDED.value,
            JobAttempt.attempt_number == Job.attempts,
            ~exists(
                select(newer.id).where(
                    newer.media_file_id == Job.media_file_id,
                    newer.kind == "transcode",
                    newer.id > Job.id,
                )
            ),
        )
        .correlate(Job)
    )
    recovered = select(JobEvent.job_id).where(
        JobEvent.type == "healthcheck_log_recovered", JobEvent.payload["removed"].as_boolean().is_not(True)
    )
    if scoped:
        return select(Job.id).where(
            or_(
                exists(attempts),
                exists(unread_log_jobs().where(JobEvent.job_id == Job.id).correlate(Job)),
                exists(recovered.where(JobEvent.job_id == Job.id).correlate(Job)),
                *([_post_action_pending(scoped=True)] if include_actions else []),
                *([exists(artifact_checks)] if include_artifact_checks else []),
            )
        )
    return select(Job.id).where(
        or_(
            Job.id.in_(attempts),
            Job.id.in_(unread_log_jobs()),
            Job.id.in_(recovered),
            *([Job.id.in_(post_action_jobs())] if include_actions else []),
            *([Job.id.in_(artifact_checks)] if include_artifact_checks else []),
        )
    )


def unread_log_jobs() -> Select[int]:
    from sqlalchemy import and_, exists, select
    from sqlalchemy.orm import aliased

    from server.models import JobAttempt, JobEvent

    recovered = aliased(JobEvent)
    saved = (
        select(recovered.id)
        .join(JobAttempt, JobAttempt.id == recovered.attempt_id)
        .where(
            recovered.job_id == JobEvent.job_id,
            recovered.attempt_id == JobEvent.attempt_id,
            recovered.id > JobEvent.id,
            recovered.type == "healthcheck_log_recovered",
            recovered.payload["removed"].as_boolean().is_(True),
            recovered.payload["log_path"].as_string() == JobEvent.payload["log_path"].as_string(),
            JobAttempt.job_id == JobEvent.job_id,
            JobAttempt.log_path == recovered.payload["log_path"].as_string(),
            JobAttempt.log_data.is_not(None),
        )
    )
    return select(JobEvent.job_id).where(and_(JobEvent.type == "healthcheck_log_unavailable", ~exists(saved)))


async def finish_workspace_actions(session: AsyncSession, job_id: int, *, attempt_id: int | None = None) -> None:
    from common.actions import ActionOutcome
    from sqlalchemy import select

    from server.models import Job, JobAttempt, JobEvent

    job = await session.get(Job, job_id, populate_existing=True, with_for_update=True)
    if job is None or await session.scalar(post_action_jobs(scoped=True).where(Job.id == job_id)) is None:
        return
    events = list(
        await session.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.type.in_(["actions_pending", "actions_finished"]))
            .order_by(JobEvent.id)
        )
    )
    latest = {event.attempt_id: event for event in events}
    pending = {
        identifier: event.payload
        for identifier, event in latest.items()
        if event.type == "actions_pending" and (attempt_id is None or identifier == attempt_id)
    }
    current = await session.scalar(
        select(JobAttempt).where(JobAttempt.job_id == job_id, JobAttempt.attempt_number == job.attempts)
    )
    current_id = current.id if current else None
    if (
        current_id not in latest
        and job.status in {"succeeded", "pending"}
        and (attempt_id is None or attempt_id == current_id)
    ):
        deferred = await session.scalar(
            select(JobEvent)
            .where(
                JobEvent.job_id == job_id,
                JobEvent.attempt_id.is_not_distinct_from(current_id),
                JobEvent.type.in_(["post_actions", "stage"]),
            )
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
        actions = (
            (deferred.payload.get("post_actions" if deferred.type == "stage" else "actions", []) if deferred else [])
            or job.post_actions
            or []
        )
        if actions:
            pending[current_id] = {"actions": actions}
    if not pending:
        return
    error = "Post-actions were abandoned after workspace writers stopped."
    job.warning = f"{job.warning} {error}" if job.warning else error
    for identifier, payload in pending.items():
        actions = payload.get("actions", [])
        if not actions:
            actions = [
                result for result in payload.get("raw_results", []) if result.get("ok") and result.get("artifacts")
            ]
        results = [
            ActionOutcome(kind=action["kind"], ok=False, error=error).model_dump(mode="json")
            for action in actions
            if "kind" in action
        ]
        attempt = (
            await session.get(JobAttempt, identifier, populate_existing=True, with_for_update=True)
            if identifier is not None
            else None
        )
        if attempt is not None:
            attempt.action_results = (attempt.action_results or []) + results
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=identifier,
                type="actions_finished",
                payload={"abandoned": True, "error": error, "results": results},
            )
        )


async def wait_workspace_io[T](operation: Callable[[], T]) -> T:
    # Locks must remain held while the filesystem operation is running.
    return await drain(asyncio.create_task(asyncio.to_thread(operation)))


def _check_directory(path: Path, error: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(error)


@contextmanager
def _file(directory: int, name: str) -> Generator[BinaryIO]:
    with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("workspace file must be a regular file")
        yield handle


def _manifest(directory: int, job_id: int) -> dict:
    with _file(directory, "manifest.json") as handle:
        manifest = json.load(handle)
    relative = manifest.get("source_relative_path")
    if (
        manifest.get("job_id") != job_id
        or not isinstance(relative, str)
        or relative.rsplit("/", 1)[0] != f"jobs/{job_id}"
        or not relative.rsplit("/", 1)[-1].startswith("source")
        or not isinstance(manifest.get("subtitles"), dict)
    ):
        raise _StagingConflictError("invalid staging manifest")
    return manifest


def source_identity(source: Path) -> dict[str, int]:
    stat = source.stat()
    return identity.source_identity(stat)


def staged_dates(root: Path, job_id: int) -> dict[str, int]:
    staged_source(root, job_id)
    with directory_fd(root / "jobs" / str(job_id)) as directory:
        manifest = _manifest(directory, job_id)
    dates = manifest["dates"]
    if set(dates) != {"atime_ns", "mtime_ns"} or any(type(value) is not int for value in dates.values()):
        raise ValueError("invalid preservation dates")
    return dates


def verify_source_identity(source: Path, expected: dict) -> dict[str, int]:
    size = source.stat().st_size
    if source.is_symlink() or expected.get("path") != str(source) or expected.get("size") != size:
        raise ValueError("Source changed since evaluation. Rescan and enqueue a new job.")
    return {"size": size}


def verify_staged_source(root: Path, job_id: int, source: Path) -> dict[str, int]:
    with directory_fd(root / "jobs" / str(job_id)) as directory:
        manifest = _manifest(directory, job_id)
    return verify_source_identity(source, {"path": manifest["path"], "size": manifest["size"]})


def _stage_source(
    root: Path,
    job_id: int,
    source: Path,
    *,
    expected_source: dict,
    fresh: bool = False,
) -> str:
    if type(job_id) is not int or job_id < 0:
        raise ValueError("invalid workspace identifier")
    jobs = root / "jobs"
    _check_directory(jobs, "jobs directory must not be a symlink")
    job = resolve_workspace_path(root, f"jobs/{job_id}")
    _check_directory(job, "job workspace must not be a symlink")
    if source.is_symlink():
        raise ValueError("source must not be a symlink")
    destination_name = f"source{source.suffix}"
    destination = job / destination_name
    generation = verify_source_identity(source, expected_source)
    dates = expected_source["dates"]
    with directory_fd(job, create=True) as directory, workspace_lock(job, exclusive=True):
        if fresh:
            for name in os.listdir(directory):
                if name == "manifest.json" or name.startswith(("source.", "subtitle.")):
                    _remove_entry(name, directory)
        try:
            manifest = _manifest(directory, job_id)
        except FileNotFoundError:
            manifest = None
        except (OSError, ValueError) as exc:
            message = f"Staging manifest could not be verified at {job / 'manifest.json'}: {exc}"
            raise _StagingConflictError(message) from exc
        if manifest is not None:
            expected = f"jobs/{job_id}/{destination_name}"
            if manifest["source_relative_path"] != expected:
                raise _StagingConflictError("invalid staging manifest")
            if manifest["path"] == str(source) and manifest["size"] == generation["size"]:
                try:
                    with _file(directory, destination_name) as handle:
                        if os.fstat(handle.fileno()).st_size != generation["size"]:
                            raise _StagingConflictError("Staged source size changed.")
                        return expected
                except FileNotFoundError:
                    pass
                except (OSError, ValueError) as exc:
                    message = f"Staged source could not be verified at {destination}: {exc}"
                    raise _StagingConflictError(message) from exc
            else:
                destination.unlink(missing_ok=True)
        try:
            copy_file(source, destination)
        except FileExistsError as exc:
            raise _StagingConflictError("Staged source already exists without a manifest.") from exc
        try:
            verify_source_identity(source, expected_source)
            verify_source_identity(destination, {"path": str(destination), "size": generation["size"]})
        except Exception:
            destination.unlink()
            raise
        manifest = {
            "job_id": job_id,
            "source_relative_path": f"jobs/{job_id}/{destination_name}",
            "dates": dates,
            "path": str(source),
            **generation,
            "subtitles": {} if manifest is None else manifest["subtitles"],
        }
        try:
            _write_manifest(directory, manifest)
        except BaseException:
            destination.unlink()
            raise
    return f"jobs/{job_id}/{destination_name}"


def _write_manifest(directory: int, manifest: dict) -> None:
    with private_file(directory) as (_, private_fd, fd), os.fdopen(os.dup(fd), "w+b") as handle:
        try:
            handle.write(json.dumps(manifest, separators=(",", ":")).encode())
            handle.flush()
            os.fsync(handle.fileno())
            os.replace("file", "manifest.json", src_dir_fd=private_fd, dst_dir_fd=directory)
        except (OSError, ValueError) as exc:
            raise _StagingConflictError("Staging manifest could not be written.") from exc


def _stage_subtitle(root: Path, job_id: int, source: Path, filename: str) -> str:
    if type(job_id) is not int or job_id < 0:
        raise ValueError("invalid workspace identifier")
    if not filename or filename in (".", "..") or any(char in filename for char in ("/", "\\", "\x00")):
        raise ValueError("subtitle filename must be a safe basename")
    if "." not in filename or filename.rsplit(".", 1)[1].lower() not in {"srt", "ass", "ssa", "vtt"}:
        raise ValueError("subtitle filename has unsupported extension")
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be a regular file")
    selected = source.parent / filename
    if selected.is_symlink() or not selected.is_file():
        raise ValueError("subtitle source must be a regular adjacent file")
    jobs = root / "jobs"
    _check_directory(jobs, "jobs directory must not be a symlink")
    job = jobs / str(job_id)
    _check_directory(job, "job workspace must not be a symlink")
    destination = job / f"subtitle.{filename.rsplit('.', 1)[1].lower()}"
    if destination.exists() and (destination.is_symlink() or not destination.is_file()):
        raise ValueError("staged subtitle must be a regular file")
    with directory_fd(job) as directory, workspace_lock(job, exclusive=True):
        manifest = _manifest(directory, job_id)
        existing_subtitle = manifest["subtitles"].get(destination.name)
        if existing_subtitle is not None:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                raise _StagingConflictError("Staged subtitle record changed.") from exc
        try:
            copy_file(selected, destination)
        except FileExistsError as exc:
            raise _StagingConflictError("Staged subtitle already exists without a manifest record.") from exc
        manifest["subtitles"][destination.name] = filename
        try:
            _write_manifest(directory, manifest)
        except BaseException:
            destination.unlink()
            raise
    return f"jobs/{job_id}/{destination.name}"


def staged_subtitle(root: Path, job_id: int, filename: str) -> str:
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or "." not in filename
        or filename.rsplit(".", 1)[1].lower() not in {"srt", "ass", "ssa", "vtt"}
    ):
        raise ValueError("subtitle filename must be a safe basename")
    if type(job_id) is not int or job_id < 0:
        raise ValueError("invalid workspace identifier")
    jobs = root / "jobs"
    _check_directory(jobs, "jobs directory must not be a symlink")
    job = jobs / str(job_id)
    _check_directory(job, "job workspace must not be a symlink")
    path = job / f"subtitle.{filename.rsplit('.', 1)[1].lower()}"
    if path.is_symlink() or not path.is_file():
        raise ValueError("staged subtitle must be a regular file")
    with directory_fd(job) as directory, _file(directory, path.name):
        pass
    return f"jobs/{job_id}/{path.name}"


def staged_source(root: Path, job_id: int) -> str:
    resolve_workspace_path(root, f"jobs/{job_id}/manifest.json")
    with directory_fd(root / "jobs" / str(job_id)) as directory:
        manifest = _manifest(directory, job_id)
        relative = manifest["source_relative_path"]
        resolve_workspace_path(root, relative)
        with _file(directory, relative.rsplit("/", 1)[-1]):
            pass
    return relative


def create_attempt_layout(root: Path, job_id: int, attempt_number: int, destination_suffix: str) -> dict[str, str]:
    if type(job_id) is not int or type(attempt_number) is not int or job_id < 0 or attempt_number < 0:
        raise ValueError("invalid workspace identifier")
    if not destination_suffix.startswith(".") or "/" in destination_suffix or "\\" in destination_suffix:
        raise ValueError("invalid destination suffix")
    _check_directory(root / "jobs", "jobs directory must not be a symlink")
    _check_directory(root / "jobs" / str(job_id), "job workspace must not be a symlink")
    _check_directory(root / "jobs" / str(job_id) / "attempts", "attempt workspace must not be a symlink")
    resolve_workspace_path(root, f"jobs/{job_id}/attempts/{attempt_number}")
    with directory_fd(root / "jobs" / str(job_id) / "attempts", create=True) as parent:
        os.mkdir(str(attempt_number), dir_fd=parent)
        directory = os.open(str(attempt_number), DIRECTORY_FLAGS, dir_fd=parent)
        try:
            os.mkdir("artifacts", dir_fd=directory)
        finally:
            os.close(directory)
    return {
        "output": f"jobs/{job_id}/attempts/{attempt_number}/output{destination_suffix}",
        "log": f"jobs/{job_id}/attempts/{attempt_number}/ffmpeg.log",
        "artifacts": f"jobs/{job_id}/attempts/{attempt_number}/artifacts",
    }


@contextmanager
def lock_job(root: Path, job_id: int) -> Generator[int]:
    if type(job_id) is not int or job_id < 0:
        raise ValueError("invalid job workspace")
    resolve_workspace_path(root, f"jobs/{job_id}")
    with directory_fd(root / "jobs") as directory, ExitStack() as locks:
        job_fd = locks.enter_context(directory_fd(root / "jobs" / str(job_id)))
        locks.enter_context(workspace_lock(root / "jobs" / str(job_id), exclusive=True))
        try:
            attempts_info = os.stat("attempts", dir_fd=job_fd, follow_symlinks=False)
        except FileNotFoundError:
            attempts_info = None
        if attempts_info is not None and stat.S_ISDIR(attempts_info.st_mode):
            attempts_fd = locks.enter_context(directory_fd(root / "jobs" / str(job_id) / "attempts"))
            for name in os.listdir(attempts_fd):
                info = os.stat(name, dir_fd=attempts_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    locks.enter_context(workspace_lock(root / "jobs" / str(job_id) / "attempts" / name, exclusive=True))
        yield directory


def _remove_job(root: Path, job_id: int, *, empty_only: bool = False) -> bool:
    try:
        if empty_only:
            if type(job_id) is not int or job_id < 0:
                raise ValueError("invalid job workspace")
            resolve_workspace_path(root, f"jobs/{job_id}")
            with directory_fd(root / "jobs") as directory:
                return _remove_empty_directory(str(job_id), directory)
        if not _SAFE_RMTREE:
            raise ValueError("safe workspace removal is unavailable")
        with ExitStack() as deletion:
            with lock_job(root, job_id) as directory:
                private_fd = deletion.enter_context(_detached_directory(str(job_id), directory))
            # Regular .lock descriptors must close before NFS unlinks them.
            shutil.rmtree("entry", dir_fd=private_fd)
    except FileNotFoundError:
        pass
    return True


def _remove_attempt(root: Path, job_id: int, attempt_number: int, *, empty_only: bool = False) -> bool:
    if type(job_id) is not int or type(attempt_number) is not int or job_id < 0 or attempt_number < 0:
        raise ValueError("invalid workspace identifier")
    resolve_workspace_path(root, f"jobs/{job_id}/attempts/{attempt_number}")
    try:
        if empty_only:
            with directory_fd(root / "jobs" / str(job_id) / "attempts") as directory:
                return _remove_empty_directory(str(attempt_number), directory)
        if not _SAFE_RMTREE:
            raise ValueError("safe workspace removal is unavailable")
        with ExitStack() as deletion:
            with (
                directory_fd(root / "jobs" / str(job_id) / "attempts") as directory,
                workspace_lock(root / "jobs" / str(job_id) / "attempts" / str(attempt_number), exclusive=True),
            ):
                private_fd = deletion.enter_context(_detached_directory(str(attempt_number), directory))
            shutil.rmtree("entry", dir_fd=private_fd)
    except FileNotFoundError:
        pass
    return True


async def snapshot_attempt_log(session: AsyncSession, root: Path, attempt: JobAttempt) -> None:
    from server.models import JobEvent

    if attempt.log_data is not None or not attempt.log_path:
        return
    try:
        attempt.log_data = await read_attempt_logs(session, root, attempt)
    except (OSError, ValueError) as exc:
        LOG.debug("Execution log unavailable for attempt %s", attempt.id, extra={"error": str(exc)})
        session.add(
            JobEvent(job_id=attempt.job_id, attempt_id=attempt.id, type="log_unavailable", payload={"error": str(exc)})
        )
        await retain_unread_log(session, root, attempt, exc)


async def retain_unread_log(session: AsyncSession, root: Path, attempt: JobAttempt, error: Exception) -> None:
    from server.models import JobEvent

    if not attempt.log_path:
        return
    try:
        path = resolve_workspace_path(root, attempt.log_path)
        if entry_stat(path, follow_symlinks=True) is None:
            return
    except FileNotFoundError:
        return
    except OSError, ValueError:
        pass
    session.add(
        JobEvent(
            job_id=attempt.job_id,
            attempt_id=attempt.id,
            type="execution_log_unavailable",
            payload={"error": str(error), "log_path": attempt.log_path},
        )
    )


async def read_attempt_logs(session: AsyncSession, root: Path, attempt: JobAttempt) -> bytes:
    from sqlalchemy import select

    from server.models import JobEvent

    current = await asyncio.to_thread(read_attempt_log, root, attempt.log_path or "")
    stages = list(
        (
            await session.scalars(
                select(JobEvent)
                .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "stage")
                .order_by(JobEvent.id)
            )
        ).all()
    )
    earlier = [
        event.payload
        for event in stages
        if event.payload.get("log") is not None and event.payload.get("log_path") != attempt.log_path
    ]
    if not earlier:
        return current
    logs = [await asyncio.to_thread(read_attempt_log, root, data["log_path"]) for data in earlier]
    return b"\n".join([*logs, current])


async def snapshot_interrupted_logs(session: AsyncSession, root: Path) -> None:
    from common.enums import JobStatus
    from sqlalchemy import or_, select
    from sqlalchemy.orm import load_only

    from server.models import Job, JobAttempt

    after = 0
    while True:
        attempts = list(
            await session.scalars(
                select(JobAttempt)
                .join(Job, JobAttempt.job_id == Job.id)
                .where(
                    or_(JobAttempt.workload == "transcode", JobAttempt.capabilities.is_not(None)),
                    Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING]),
                    JobAttempt.attempt_number == Job.attempts,
                    JobAttempt.id > after,
                    JobAttempt.log_data.is_(None),
                    JobAttempt.log_path.is_not(None),
                )
                .order_by(JobAttempt.id)
                .limit(_WORKSPACE_BATCH)
                .options(
                    load_only(
                        JobAttempt.id, JobAttempt.job_id, JobAttempt.log_path, JobAttempt.log_data, raiseload=True
                    )
                )
            )
        )
        if not attempts:
            break
        after = attempts[-1].id
        for attempt in attempts:
            await snapshot_attempt_log(session, root, attempt)
        await session.flush()


def attempt_log_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("attempt log path is missing")
    path = resolve_workspace_path(root, relative_path)
    current = root
    if any((current := current / part).is_symlink() for part in path.relative_to(root).parts) or not path.is_file():
        raise ValueError("attempt log must be a regular file")
    return path


def read_attempt_log(root: Path, relative_path: str) -> bytes:
    path = attempt_log_path(root, relative_path)
    with directory_fd(path.parent) as directory, _file(directory, path.name) as handle:
        return handle.read()


@contextmanager
def _detached_directory(name: str, directory: int) -> Generator[int]:
    parent_fd = os.dup(directory)
    private = f".{secrets.token_hex(8)}.cleanup"
    try:
        os.mkdir(private, dir_fd=parent_fd)
        private_fd = os.open(private, DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            os.rename(name, "entry", src_dir_fd=parent_fd, dst_dir_fd=private_fd)
            try:
                yield private_fd
            except BaseException:
                try:
                    os.stat("entry", dir_fd=private_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    # Keep unfinished removal visible to the next cleanup pass.
                    os.rename("entry", name, src_dir_fd=private_fd, dst_dir_fd=parent_fd)
                raise
        finally:
            os.close(private_fd)
            os.rmdir(private, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _remove_entry(name: str, directory: int) -> None:
    if stat.S_ISDIR(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
        if not _SAFE_RMTREE:
            raise ValueError("safe workspace removal is unavailable")
        with _detached_directory(name, directory) as private_fd:
            shutil.rmtree("entry", dir_fd=private_fd)
    else:
        os.unlink(name, dir_fd=directory)


def _remove_empty_directory(name: str, directory: int) -> bool:
    try:
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return True
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name == ".lock" and entry.is_file(follow_symlinks=False):
                    continue
                if not entry.is_dir(follow_symlinks=False) or not _remove_empty_directory(entry.name, fd):
                    return False
        try:
            os.unlink(".lock", dir_fd=fd)
        except FileNotFoundError:
            pass
        try:
            # A file created after traversal makes rmdir fail. Never unlink it or fall back to rmtree.
            os.rmdir(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                return False
            raise
        return True
    finally:
        os.close(fd)


def _cleanup_filesystem(
    root: Path,
    names: list[str],
    jobs: set[int],
    active_jobs: set[int],
    active: dict[int, set[int]],
    empty_only: set[int],
    healthchecks: set[int],
    busy_jobs: set[int],
) -> tuple[dict[str, int], list[int], list[tuple[int, int]]]:
    report = {"removed": 0, "preserved": 0}
    removed_jobs: list[int] = []
    removed_attempts: list[tuple[int, int]] = []
    jobs_root = root / "jobs"
    if jobs_root.is_symlink() or not jobs_root.exists() or not jobs_root.is_dir():
        return report, removed_jobs, removed_attempts
    with directory_fd(jobs_root) as directory:
        for name in names:
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode) or not re.fullmatch(r"0|[1-9][0-9]*", name):
                _remove_entry(name, directory)
                report["removed"] += 1
                continue
            job_id = int(name)
            if job_id in empty_only:
                try:
                    removed = _remove_job(root, job_id, empty_only=True)
                except BlockingIOError:
                    removed = False
                except (OSError, ValueError) as exc:
                    LOG.warning("Could not clean up empty workspace for job %s", job_id, extra={"error": str(exc)})
                    removed = False
                report["removed" if removed else "preserved"] += 1
                continue
            if job_id not in active_jobs:
                try:
                    _remove_job(root, job_id)
                except BlockingIOError:
                    report["preserved"] += 1
                    continue
                except (OSError, ValueError) as exc:
                    LOG.warning("Could not clean up workspace for job %s", job_id, extra={"error": str(exc)})
                    report["preserved"] += 1
                    continue
                report["removed"] += 1
                removed_jobs.append(job_id)
                continue
            report["preserved"] += 1
            if job_id not in jobs:
                continue
            if job_id in healthchecks or job_id in busy_jobs:
                # Healthcheck logs and live attempts have separate cleanup tasks.
                continue
            job_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
            try:
                try:
                    info = os.stat("attempts", dir_fd=job_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    os.unlink("attempts", dir_fd=job_fd)
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                attempts_fd = os.open("attempts", DIRECTORY_FLAGS, dir_fd=job_fd)
                try:
                    for attempt_name in os.listdir(attempts_fd):
                        info = os.stat(attempt_name, dir_fd=attempts_fd, follow_symlinks=False)
                        if (
                            not stat.S_ISDIR(info.st_mode)
                            or not re.fullmatch(r"0|[1-9][0-9]*", attempt_name)
                            or int(attempt_name) not in active.get(job_id, set())
                        ):
                            try:
                                if stat.S_ISDIR(info.st_mode) and re.fullmatch(r"0|[1-9][0-9]*", attempt_name):
                                    _remove_attempt(root, job_id, int(attempt_name))
                                    removed_attempts.append((job_id, int(attempt_name)))
                                else:
                                    _remove_entry(attempt_name, attempts_fd)
                                report["removed"] += 1
                            except BlockingIOError:
                                pass
                            except (OSError, ValueError) as exc:
                                LOG.warning(
                                    "Could not clean up workspace for job %s attempt %s",
                                    job_id,
                                    attempt_name,
                                    extra={"error": str(exc)},
                                )
                finally:
                    os.close(attempts_fd)
            finally:
                os.close(job_fd)
    return report, removed_jobs, removed_attempts


def _cleanup_nodes(nodes_root: Path) -> int:
    with directory_fd(nodes_root.parent) as directory:
        try:
            info = os.stat(nodes_root.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(info.st_mode):
            os.unlink(nodes_root.name, dir_fd=directory)
            return 1
        if not stat.S_ISDIR(info.st_mode):
            return 0
        nodes_fd = os.open(nodes_root.name, DIRECTORY_FLAGS, dir_fd=directory)
        try:
            names = os.listdir(nodes_fd)
            for name in names:
                _remove_entry(name, nodes_fd)
            return len(names)
        finally:
            os.close(nodes_fd)


async def cleanup_workspace(sessionmaker, root: Path, *, busy_jobs: set[int] | None = None) -> dict[str, int]:
    from common.enums import JobStatus
    from sqlalchemy import or_, select
    from sqlalchemy.orm import load_only

    from server.models import Job, JobAttempt

    try:
        removed_nodes = await wait_workspace_io(lambda: _cleanup_nodes(root / "nodes"))
    except FileNotFoundError:
        removed_nodes = 0
    jobs_root = root / "jobs"
    report = {"removed": removed_nodes, "preserved": 0}
    if jobs_root.is_symlink() or (jobs_root.exists() and not jobs_root.is_dir()):
        return report

    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED}
    busy = busy_jobs or set()
    after = 0
    while True:
        async with sessionmaker() as session:
            snapshots = list(
                await session.scalars(
                    select(JobAttempt)
                    .join(Job, Job.id == JobAttempt.job_id)
                    .where(
                        Job.kind == "transcode",
                        Job.id.not_in(busy),
                        JobAttempt.id > after,
                        JobAttempt.log_data.is_(None),
                        JobAttempt.log_path.is_not(None),
                        or_(
                            Job.status.in_(terminal | {JobStatus.VERIFYING, JobStatus.FINALIZING}),
                            JobAttempt.attempt_number < Job.attempts,
                        ),
                    )
                    .order_by(JobAttempt.id)
                    .limit(_WORKSPACE_BATCH)
                    .options(
                        load_only(
                            JobAttempt.id, JobAttempt.job_id, JobAttempt.log_path, JobAttempt.log_data, raiseload=True
                        )
                    )
                )
            )
            if not snapshots:
                break
            after = snapshots[-1].id
            await session.execute(
                select(Job.id)
                .where(Job.id.in_({attempt.job_id for attempt in snapshots}))
                .order_by(Job.id)
                .with_for_update()
            )
            for attempt in snapshots:
                job = await session.get(Job, attempt.job_id, populate_existing=True)
                attempt = await session.scalar(
                    select(JobAttempt)
                    .where(JobAttempt.id == attempt.id)
                    .options(
                        load_only(
                            JobAttempt.id,
                            JobAttempt.job_id,
                            JobAttempt.attempt_number,
                            JobAttempt.log_path,
                            JobAttempt.log_data,
                            raiseload=True,
                        )
                    )
                    .with_for_update(of=JobAttempt)
                    .execution_options(populate_existing=True)
                )
                if (
                    job is None
                    or attempt is None
                    or job.kind != "transcode"
                    or job.id in busy
                    or attempt.log_data is not None
                    or attempt.log_path is None
                    or (
                        job.status not in terminal | {JobStatus.VERIFYING, JobStatus.FINALIZING}
                        and attempt.attempt_number >= job.attempts
                    )
                ):
                    continue
                await snapshot_attempt_log(session, root, attempt)
            await session.commit()

    entries_stack = ExitStack()
    try:
        try:
            entries = await wait_workspace_io(lambda: entries_stack.enter_context(os.scandir(jobs_root)))
        except FileNotFoundError:
            entries = None
        while entries is not None:
            names = await wait_workspace_io(lambda: [entry.name for entry in islice(entries, _WORKSPACE_BATCH)])
            if not names:
                break
            ids = [int(name) for name in names if re.fullmatch(r"0|[1-9][0-9]*", name)]
            async with sessionmaker() as session:
                # Keep each batch's job rows locked until its filesystem work finishes.
                jobs = {
                    job.id: job
                    for job in await session.execute(
                        select(Job.id, Job.kind, Job.status, Job.attempts)
                        .where(Job.id.in_(ids))
                        .order_by(Job.id)
                        .with_for_update()
                    )
                }
                await session.execute(
                    select(JobAttempt.id).where(JobAttempt.job_id.in_(ids)).order_by(JobAttempt.id).with_for_update()
                )
                protected_healthchecks = set(
                    await session.scalars(recovery_jobs(scoped=True).where(Job.id.in_(ids), Job.kind == "healthcheck"))
                )
                active: dict[int, set[int]] = {}
                for job_id, number in await session.execute(
                    select(JobAttempt.job_id, JobAttempt.attempt_number)
                    .join(Job, Job.id == JobAttempt.job_id)
                    .where(
                        Job.id.in_(ids),
                        Job.status.in_(
                            {JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING}
                        ),
                        JobAttempt.attempt_number == Job.attempts,
                    )
                ):
                    active.setdefault(job_id, set()).add(number)
                active_jobs = (
                    {
                        job_id
                        for job_id, job in jobs.items()
                        if job.status not in terminal | {JobStatus.VERIFYING, JobStatus.FINALIZING}
                    }
                    | protected_healthchecks
                    | busy
                )
                empty_only = {
                    job_id
                    for job_id, job in jobs.items()
                    if job.kind == "healthcheck" and job.status in terminal and job_id not in protected_healthchecks
                }
                filesystem_report, removed_jobs, removed_attempts = await wait_workspace_io(
                    partial(
                        _cleanup_filesystem,
                        root,
                        names,
                        set(jobs),
                        active_jobs,
                        active,
                        empty_only,
                        {job_id for job_id, job in jobs.items() if job.kind == "healthcheck"},
                        busy,
                    )
                )
                for job_id in removed_jobs:
                    await finish_workspace_actions(session, job_id)
                for job_id, number in removed_attempts:
                    attempt_id = await session.scalar(
                        select(JobAttempt.id).where(JobAttempt.job_id == job_id, JobAttempt.attempt_number == number)
                    )
                    if attempt_id is not None:
                        await finish_workspace_actions(session, job_id, attempt_id=attempt_id)
                await session.commit()
                report["removed"] += filesystem_report["removed"]
                report["preserved"] += filesystem_report["preserved"]
    finally:
        await wait_workspace_io(entries_stack.close)

    after = 0
    while True:
        async with sessionmaker() as session:
            pending = list(
                await session.scalars(
                    select(Job.id)
                    .where(
                        Job.status.in_(terminal), Job.id.in_(post_action_jobs()), Job.id.not_in(busy), Job.id > after
                    )
                    .order_by(Job.id)
                    .limit(_WORKSPACE_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            if not pending:
                break
            after = pending[-1]
            for job_id in pending:
                if await wait_workspace_io(lambda job_id=job_id: entry_stat(root / "jobs" / str(job_id))) is None:
                    await finish_workspace_actions(session, job_id)
            await session.commit()
    return report
