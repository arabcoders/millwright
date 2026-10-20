from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import threading
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from common.fs.acquire import DIRECTORY_FLAGS, directory_fd, entry_stat
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from server import workspace
from server.models import Job, JobAttempt, MediaFile, NodeRow
from server.retention import TERMINAL

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.staging import StagingService

LOG = logging.getLogger(__name__)
_DIRECTORY = DIRECTORY_FLAGS
_IDENTIFIER = re.compile(r"0|[1-9][0-9]*")
QUERY_BATCH = 500


def scan_disk(root: Path, stopped: threading.Event) -> tuple[list[dict], int]:
    with directory_fd(root) as root_fd:
        free = os.fstatvfs(root_fd)
        try:
            fd = os.open("jobs", _DIRECTORY, dir_fd=root_fd)
        except FileNotFoundError:
            return [], free.f_bavail * free.f_frsize
    try:
        free = os.fstatvfs(fd)
        entries = []
        for name in sorted(os.listdir(fd)):
            if stopped.is_set():
                raise InterruptedError("workspace sampling stopped")
            if not _IDENTIFIER.fullmatch(name):
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                continue
            job_id = int(name)
            try:
                row = workspace.inspect_directory(fd, job_id, stopped)
                try:
                    with workspace.lock_job(root, job_id):
                        pass
                    row["busy"] = False
                except BlockingIOError:
                    row["busy"] = True
                entries.append(row)
            except FileNotFoundError:
                continue  # Automatic cleanup can remove a directory during a sample.
            except (OSError, ValueError) as exc:
                LOG.exception("Workspace %s under %s could not be inspected", job_id, root)
                entries.append(
                    {
                        "workspace_id": job_id,
                        "size_bytes": None,
                        "file_count": None,
                        "source_bytes": None,
                        "output_bytes": None,
                        "last_activity_at": None,
                        "files": [],
                        "error": f"Workspace cannot be safely inspected. {type(exc).__name__}: {exc}",
                    }
                )
        return entries, free.f_bavail * free.f_frsize
    finally:
        os.close(fd)


async def job_details(session: AsyncSession, ids: list[int]) -> dict[int, dict]:
    result = {}
    for batch in batched(ids, QUERY_BATCH, strict=False):
        rows = (
            await session.execute(
                select(
                    Job.id,
                    Job.status,
                    Job.kind,
                    Job.attempts,
                    Job.progress,
                    MediaFile.id.label("file_id"),
                    MediaFile.path,
                    Job.node_id,
                    NodeRow.display_name,
                )
                .outerjoin(MediaFile, MediaFile.id == Job.media_file_id)
                .outerjoin(NodeRow, NodeRow.id == Job.node_id)
                .where(Job.id.in_(batch))
            )
        ).all()
        attempts = (
            await session.execute(
                select(
                    JobAttempt.job_id,
                    JobAttempt.id,
                    JobAttempt.attempt_number,
                    JobAttempt.status,
                    JobAttempt.result,
                    JobAttempt.error,
                    JobAttempt.node_name,
                    JobAttempt.capabilities.is_not(None).label("node_workspace"),
                    JobAttempt.workload,
                    JobAttempt.log_path,
                )
                .where(JobAttempt.job_id.in_(batch))
                .order_by(JobAttempt.attempt_number)
            )
        ).all()
        history_by_job = {}
        for attempt in attempts:
            history_by_job.setdefault(attempt.job_id, []).append(attempt)
        protected = set((await session.scalars(workspace.recovery_jobs(scoped=True).where(Job.id.in_(batch)))).all())
        for row in rows:
            history = history_by_job.get(row.id, [])
            node_workspace = any(attempt.node_workspace for attempt in history)
            reason = None
            releasable = row.status in (*TERMINAL, "verifying", "finalizing")
            if not releasable:
                reason = f"Job is {row.status}."
            elif row.kind == "healthcheck" and row.id in protected:
                reason = "Healthcheck diagnostics are pending recovery."
            elif row.kind == "mkv_stats" and row.id in protected:
                reason = "Metadata publication is pending recovery."
            result[row.id] = {
                "job_id": row.id,
                "status": row.status,
                "attempt_number": row.attempts,
                "progress": row.progress,
                "file_id": row.file_id,
                "filename": Path(row.path).name if row.path else None,
                "node_id": row.node_id,
                "node_name": row.display_name or (history[-1].node_name if history else None),
                "eligible": releasable
                and (row.kind == "transcode" or (node_workspace and row.kind in {"healthcheck", "mkv_stats"}))
                and not (row.kind in {"healthcheck", "mkv_stats"} and row.id in protected),
                "empty_only": row.status in TERMINAL
                and row.kind == "healthcheck"
                and not node_workspace
                and row.id not in protected,
                "reason": reason or "Unused",
                "attempts": [
                    {
                        "id": a.id,
                        "attempt_number": a.attempt_number,
                        "status": a.status,
                        "result": a.result,
                        "node_name": a.node_name,
                    }
                    for a in history
                ],
            }
    return result


def workspace_row(disk: dict, jobs: dict[int, dict], *, staging: StagingService | None = None) -> dict:
    details = jobs.get(
        disk["workspace_id"],
        {
            "job_id": None,
            "status": None,
            "file_id": None,
            "filename": None,
            "node_id": None,
            "node_name": None,
            "progress": None,
            "attempt_number": None,
            "attempts": [],
            "eligible": True,
            "reason": "Unused",
        },
    )
    row = {**disk, **details}
    busy = row.pop("busy", False)
    empty_only = row.pop("empty_only", False)
    if empty_only and disk["file_count"] == 0 and disk["error"] is None:
        row.update(eligible=True, reason="Unused")
    if staging is not None and disk["workspace_id"] in staging.active_jobs:
        row.update(eligible=False, reason="Source files are still being copied.")
    if busy or (staging is not None and disk["workspace_id"] in staging.busy_jobs):
        row.update(eligible=False, reason="Job is still processing.")
    if disk["error"]:
        row.update(eligible=False, reason=disk["error"])
    return row


class WorkspaceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        root: Path,
        *,
        staging: StagingService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.root = root
        self.staging = staging
        self.lock = asyncio.Lock()
        self._stopped = threading.Event()
        self._scan_task: asyncio.Task | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def cleanup(self) -> int:
        snapshot = await self.sample()
        if snapshot["error"]:
            raise ValueError(snapshot["error"])
        removed = 0
        for item in snapshot["items"]:
            outcome = await self.staging.prune_job(item["workspace_id"], attempt_number=item["attempt_number"])
            if outcome["outcome"] == "removed":
                removed += 1
            else:
                for attempt in item["attempts"]:
                    path = self.root / "jobs" / str(item["workspace_id"]) / "attempts" / str(attempt["attempt_number"])
                    if await asyncio.to_thread(entry_stat, path) is not None:
                        removed += await self.staging.prune_attempt(attempt["id"])
        if removed:
            LOG.info("Workspace cleanup removed %s directories", removed)
        return removed

    async def sample(self) -> dict:
        async with self.lock:
            if self._stopped.is_set():
                raise RuntimeError("workspace inspection is closed")
            sampled_at = datetime.now(UTC).isoformat()
            try:
                self._scan_task = asyncio.create_task(asyncio.to_thread(scan_disk, self.root, self._stopped))
                # Keep the lock and worker until filesystem IO has finished.
                drain = asyncio.gather(self._scan_task, return_exceptions=True)
                cancelled = False
                while not drain.done():
                    try:
                        await asyncio.shield(drain)
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
                disk, free = self._scan_task.result()
                async with self.sessionmaker() as session:
                    jobs = await job_details(session, [entry["workspace_id"] for entry in disk])
                items: list[dict] = [
                    {**workspace_row(entry, jobs, staging=self.staging), "sampled_at": sampled_at} for entry in disk
                ]
                measured = all(item["size_bytes"] is not None for item in items)
                unused = [item for item in items if item["eligible"]]
                summary = {
                    "count": len(items),
                    "active_count": sum(
                        item["status"] in {"pending", "hold", "staging", "queued", "claimed", "running"}
                        or item["reason"] in {"Job is still processing.", "Source files are still being copied."}
                        for item in items
                    ),
                    "unused_count": len(unused),
                    "unused_size_bytes": sum(item["size_bytes"] for item in unused)
                    if all(item["size_bytes"] is not None for item in unused)
                    else None,
                    "protected_count": sum(not item["eligible"] for item in items),
                    "size_bytes": sum(item["size_bytes"] for item in items) if measured else None,
                    "free_bytes": free,
                }
                snapshot = {"items": items, "summary": summary, "sampled_at": sampled_at, "error": None}
            except (OSError, ValueError, SQLAlchemyError) as exc:
                LOG.exception("Workspace sampling failed for %s", self.root)
                snapshot = {
                    "items": [],
                    "summary": {
                        "count": None,
                        "active_count": None,
                        "unused_count": None,
                        "unused_size_bytes": None,
                        "protected_count": None,
                        "size_bytes": None,
                        "free_bytes": None,
                    },
                    "sampled_at": sampled_at,
                    "error": f"Workspace data is unavailable or unsafe. {type(exc).__name__}: {exc}",
                }
            return snapshot

    async def listing(
        self,
        *,
        query: str | None = None,
        eligibility: Literal["all", "unused", "protected"] = "all",
        sort_by: Literal[
            "workspace_id", "filename", "status", "node_name", "size_bytes", "last_activity_at"
        ] = "workspace_id",
        sort_dir: Literal["asc", "desc"] = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        snapshot = await self.sample()
        search = query.strip().casefold() if query else ""
        items = [
            item
            for item in snapshot["items"]
            if (eligibility == "all" or item["eligible"] == (eligibility == "unused"))
            and search
            in " ".join(
                str(item[key]) if item[key] is not None else ""
                for key in ("filename", "workspace_id", "node_name", "status")
            ).casefold()
        ]

        def sort_key(item: dict) -> tuple:
            value = item[sort_by]
            if isinstance(value, str):
                value = value.casefold()
            return value, item["workspace_id"]

        known = sorted((item for item in items if item[sort_by] is not None), key=sort_key, reverse=sort_dir == "desc")
        unknown = sorted(
            (item for item in items if item[sort_by] is None),
            key=lambda item: item["workspace_id"],
            reverse=sort_dir == "desc",
        )
        return {
            "items": (known + unknown)[offset : offset + limit],
            "total": len(items),
            **{key: snapshot[key] for key in ("summary", "sampled_at", "error")},
        }

    async def detail(self, job_id: int) -> dict:
        def scan() -> dict:
            with directory_fd(self.root / "jobs") as fd:
                row = workspace.inspect_directory(fd, job_id, self._stopped)
                try:
                    with workspace.lock_job(self.root, job_id):
                        pass
                    row["busy"] = False
                except BlockingIOError:
                    row["busy"] = True
                return row

        async with self.lock:
            if self._stopped.is_set():
                raise RuntimeError("workspace inspection is closed")
            sampled_at = datetime.now(UTC).isoformat()
            disk = await workspace.wait_workspace_io(scan)
            async with self.sessionmaker() as session:
                jobs = await job_details(session, [job_id])
            return {
                **workspace_row(disk, jobs, staging=self.staging),
                "sampled_at": sampled_at,
            }

    async def aclose(self) -> None:
        if self._close_task is None:
            self._stopped.set()
            self._close_task = asyncio.create_task(self._close())
        closing = self._close_task
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        closing.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> None:
        if self._scan_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._scan_task), timeout=5)
            except TimeoutError:
                LOG.error("workspace scan did not stop within five seconds")
            except OSError, ValueError:
                pass
            await asyncio.gather(self._scan_task, return_exceptions=True)
        async with self.lock:
            self._scan_task = None
