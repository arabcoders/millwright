from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.fs.acquire import directory_fd, private_file
from sqlalchemy import and_, or_, select, update

from server.models import Job, JobAttempt, JobEvent
from server.workspace import _file, wait_workspace_io

LOG = logging.getLogger("server.healthlogs")
QUERY_BATCH = 500

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def healthcheck_log_path(root: Path, relative: str) -> Path:
    if not relative.endswith(".log") or not relative[:-4].isdigit() or relative != f"{int(relative[:-4])}.log":
        raise ValueError("Invalid healthcheck diagnostic path.")
    return root / relative


@dataclass(kw_only=True)
class DiagnosticLog:
    path: Path

    def append(self, text: str, *, sync: bool = False) -> None:
        with directory_fd(self.path.parent) as directory:
            fd = os.open(self.path.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Healthcheck diagnostic must be a regular file.")
                handle.write(text)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())


def create_healthcheck_log(root: Path, attempt_id: int) -> DiagnosticLog:
    path = healthcheck_log_path(root, f"{attempt_id}.log")
    with directory_fd(root, create=True) as directory, private_file(directory) as (_, private, fd):
        os.fchmod(fd, 0o600)
        os.link("file", path.name, src_dir_fd=private, dst_dir_fd=directory)
    return DiagnosticLog(path=path)


def capture_healthcheck_log(root: Path, relative: str) -> bytes:
    path = healthcheck_log_path(root, relative)
    with directory_fd(root) as directory, _file(directory, path.name) as handle:
        return handle.read()


def remove_healthcheck_log(root: Path, relative: str) -> None:
    path = healthcheck_log_path(root, relative)
    with directory_fd(root) as directory:
        try:
            os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass


async def snapshot_healthcheck_log(session: AsyncSession, root: Path, attempt: JobAttempt) -> None:
    job_id = attempt.job_id
    if await session.get(Job, job_id, with_for_update=True, populate_existing=True) is None:
        return
    current = await session.get(JobAttempt, attempt.id, with_for_update=True, populate_existing=True)
    if current is None or current.job_id != job_id:
        return
    attempt = current
    if attempt.log_data is not None or attempt.log_path is None:
        return
    acquired = await session.scalar(
        select(JobEvent)
        .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "healthcheck_log_created")
        .execution_options(populate_existing=True)
    )
    try:
        if acquired is None or acquired.payload["log_path"] != attempt.log_path:
            raise ValueError("Healthcheck log creation record is missing.")  # noqa: TRY301
        data = await wait_workspace_io(lambda: capture_healthcheck_log(root, attempt.log_path or ""))
    except (OSError, ValueError) as exc:
        previous = await session.scalar(
            select(JobEvent)
            .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "healthcheck_log_unavailable")
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
        payload = {"error": str(exc), "log_path": attempt.log_path}
        if previous is not None and previous.payload == payload:
            return
        session.add(
            JobEvent(
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                type="healthcheck_log_unavailable",
                payload=payload,
            )
        )
    else:
        attempt.log_data = data
        session.add(
            JobEvent(
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                type="healthcheck_log_recovered",
                payload={"log_path": attempt.log_path, "removed": False},
            )
        )


async def cleanup_healthcheck_log(session: AsyncSession, root: Path, attempt_id: int) -> None:
    # Retries and deletion must keep this task registered until the unlink thread exits.
    job_id = await session.scalar(select(JobAttempt.job_id).where(JobAttempt.id == attempt_id))
    if job_id is None or await session.get(Job, job_id, with_for_update=True, populate_existing=True) is None:
        return
    current_attempt_id = await session.scalar(
        update(JobAttempt)
        .where(
            JobAttempt.id == attempt_id,
            JobAttempt.job_id == job_id,
            JobAttempt.workload == "healthcheck",
            JobAttempt.capabilities.is_(None),
            JobAttempt.status.in_(["succeeded", "failed", "cancelled", "skipped"]),
            JobAttempt.finished_at.is_not(None),
            or_(
                JobAttempt.result.is_distinct_from("interrupted"),
                and_(JobAttempt.finished_at.is_not(None), JobAttempt.error.in_(["requeued", "max attempts exceeded"])),
            ),
            JobAttempt.log_data.is_not(None),
        )
        .values(status=JobAttempt.status)
        .returning(JobAttempt.id)
    )
    if current_attempt_id is None:
        return
    record = await session.scalar(
        select(JobEvent)
        .where(JobEvent.attempt_id == attempt_id, JobEvent.type == "healthcheck_log_recovered")
        .execution_options(populate_existing=True)
    )
    attempt = await session.get(JobAttempt, attempt_id, populate_existing=True)
    if record is None or attempt is None or record.payload["removed"]:
        return
    if record.payload["log_path"] != attempt.log_path:
        raise ValueError("Healthcheck diagnostic record changed.")
    await wait_workspace_io(lambda: remove_healthcheck_log(root, record.payload["log_path"]))
    record.payload = {**record.payload, "removed": True}


async def recover_healthcheck_logs(
    sessionmaker: async_sessionmaker[AsyncSession], root: Path, *, startup: bool = False
) -> None:
    pending = select(JobEvent.attempt_id).where(
        JobEvent.type == "healthcheck_log_recovered", JobEvent.payload["removed"].as_boolean().is_not(True)
    )
    eligible = and_(
        JobAttempt.workload == "healthcheck",
        JobAttempt.capabilities.is_(None),
        or_(
            and_(JobAttempt.log_path.is_not(None), JobAttempt.log_data.is_(None)),
            JobAttempt.id.in_(pending),
            *([JobAttempt.status.in_(["claimed", "running"])] if startup else []),
        ),
        *([] if startup else [JobAttempt.status.in_(["succeeded", "failed", "cancelled", "skipped"])]),
        or_(
            JobAttempt.result.is_distinct_from("interrupted"),
            JobAttempt.error.in_(["requeued", "max attempts exceeded"]),
        ),
    )
    last_id = 0
    while True:
        async with sessionmaker() as session:
            attempts = list(
                (
                    await session.scalars(
                        select(JobAttempt.id)
                        .where(eligible, JobAttempt.id > last_id)
                        .order_by(JobAttempt.id)
                        .limit(QUERY_BATCH)
                    )
                ).all()
            )
        if not attempts:
            break
        last_id = attempts[-1]
        for old in attempts:
            try:
                async with sessionmaker() as session:
                    job_id = await session.scalar(select(JobAttempt.job_id).where(JobAttempt.id == old))
                    if (
                        job_id is None
                        or await session.get(Job, job_id, with_for_update=True, populate_existing=True) is None
                    ):
                        continue
                    current_attempt_id = await session.scalar(
                        update(JobAttempt)
                        .where(JobAttempt.id == old, JobAttempt.job_id == job_id, eligible)
                        .values(status=JobAttempt.status)
                        .returning(JobAttempt.id)
                    )
                    if current_attempt_id is None:
                        continue
                    attempt = await session.get(JobAttempt, old, populate_existing=True)
                    if attempt is None:
                        continue
                    await snapshot_healthcheck_log(session, root, attempt)
                    if attempt.log_path is None and attempt.status in {"claimed", "running"}:
                        attempt.log_data = b"healthcheck: interrupted during restart\n"
                    await session.commit()
                    await cleanup_healthcheck_log(session, root, attempt.id)
                    await session.commit()
            except OSError, ValueError:
                LOG.warning("Healthcheck diagnostic recovery withheld for attempt %s", old, exc_info=True)
