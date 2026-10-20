from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never
from weakref import WeakValueDictionary

from common.actions import Action
from common.capabilities import low_priority_argv
from common.enums import ExecutionLocation, JobStatus, TranscodeStatus
from common.fs.identity import source_identity
from common.media.tools import ProbeError, run_ffprobe, run_mkvpropedit
from pydantic import TypeAdapter
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from server.fs.source import MediaSnapshot, authorized_library, capture_source, claim_catalog, restore_source_dates
from server.metadata_jobs import enqueue_metadata_job, metadata_source
from server.models import FinalizationJournal, Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.processing_cutoff import processing_allowed
from server.queue.store import (
    ActiveJobError,
    active_job_for_file,
    check_file_idle,
    cooldown_hold_until,
    current_request,
    enqueue_healthcheck_job,
    finish,
    finish_artifact_check,
)
from server.scanner.normalize import normalize
from server.scanner.scan import _lacks_stream_bitrates
from server.settings import read_settings
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable

    from common.media.probe import ProbeData
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_PROBE_LOCKS: WeakValueDictionary[tuple[async_sessionmaker[AsyncSession], Path], asyncio.Lock] = WeakValueDictionary()


@dataclass(kw_only=True, slots=True)
class ProbeResult:
    media: MediaSnapshot
    probe: ProbeData
    source: dict
    healthcheck_id: int | None
    diagnostics: tuple[str, ...]


class LibraryProbeError(ProbeError):
    def __init__(
        self,
        message: str,
        *,
        reason: Literal["missing", "unauthorized", "busy", "superseded", "failed"],
        cleanup: Literal["refused", "completed", "unresolved"],
        media_id: int | None,
        diagnostics: tuple[str, ...] = (),
        failure: ProbeError | None = None,
    ) -> None:
        super().__init__(
            message,
            tool=failure.tool if failure is not None else None,
            exit_code=failure.exit_code if failure is not None else None,
            stderr=failure.stderr if failure is not None else None,
            stdout=failure.stdout if failure is not None else None,
        )
        self.reason = reason
        self.cleanup = cleanup
        self.media_id = media_id
        self.diagnostics = diagnostics


class LibraryFS:
    def __init__(
        self,
        library_id: int,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        ffprobe_bin: str | Path,
        mkvpropedit_bin: str | Path,
        workspace_dir: Path,
        stats_allowed: Callable[[], bool] | None = None,
        on_progress: Callable[[str, int | None], None] | None = None,
        on_tool: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.library_id = library_id
        self.sessionmaker = sessionmaker
        self.ffprobe_bin = ffprobe_bin
        self.mkvpropedit_bin = mkvpropedit_bin
        self.workspace_dir = workspace_dir
        self.stats_allowed = stats_allowed
        self.on_progress = on_progress
        self.on_tool = on_tool

    async def run_job(self, job_id: int, attempt_id: int) -> ProbeResult:
        async with self.sessionmaker() as session:
            job = await session.get(Job, job_id)
            if job is None or job.kind not in {"probe", "mkv_stats"}:
                raise LibraryProbeError("Metadata job not found.", reason="missing", cleanup="refused", media_id=None)
            return await _ProbeOperation(
                fs=self,
                session=session,
                purpose="basic" if job.kind == "probe" else "statistics",
                job_id=job_id,
                attempt_id=attempt_id,
            ).run(job.media_file_id)

    async def refresh_current(
        self, media_id: int, *, job_id: int | None = None, request_id: int | None = None
    ) -> ProbeResult:
        async with self.sessionmaker() as session:
            return await _ProbeOperation(
                fs=self, session=session, purpose="current", job_id=job_id, request_id=request_id
            ).run(media_id)

    async def refresh_published(self, attempt_id: int) -> ProbeResult:
        async with self.sessionmaker() as session:
            return await _ProbeOperation(fs=self, session=session, purpose="published").run(attempt_id)


@dataclass(kw_only=True, slots=True)
class _ProbeOperation:
    fs: LibraryFS
    session: AsyncSession
    purpose: Literal["basic", "statistics", "current", "published"]
    row: MediaFile | None = None
    snapshot: MediaSnapshot | None = None
    path: Path | None = None
    stat: os.stat_result | None = None
    library: Library | None = None
    job: Job | None = None
    attempt_id: int | None = None
    journal_id: int | None = None
    dates: tuple[int, int] | None = None
    diagnostics: list[str] = field(default_factory=list)
    effects: bool = False
    metadata_committed: bool = False
    probe_failure: ProbeError | None = None
    job_id: int | None = None
    request_id: int | None = None
    selected_flow: bool = False
    argv_prefix: tuple[str, ...] = ()

    @property
    def media_id(self) -> int | None:
        return self.snapshot.id if self.snapshot is not None else None

    def error(
        self, message: str, reason: Literal["missing", "unauthorized", "busy", "superseded", "failed"]
    ) -> LibraryProbeError:
        return LibraryProbeError(message, reason=reason, cleanup="refused", media_id=self.media_id)

    async def run(self, identifier: int) -> ProbeResult:
        handled = False
        try:
            if self.purpose == "published":
                self.attempt_id = identifier
                await self._load_publication(identifier)
            else:
                await self._load_media(identifier)
            assert self.path is not None
            path = self.path
            self.stat = await wait_workspace_io(path.stat)
            # Serialize metadata tools without holding a database writer.
            await self._authorize()
            await self.session.commit()
            lock = _PROBE_LOCKS.setdefault((self.fs.sessionmaker, path), asyncio.Lock())
            async with lock:
                try:
                    return await self._probe()
                except (Exception, asyncio.CancelledError) as exc:
                    handled = True
                    await self._fail(exc)
        except LibraryProbeError:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            if handled:
                raise
            await self._fail(exc)

    async def _load_media(self, media_id: int) -> None:
        self.row = await self.session.get(MediaFile, media_id)
        if self.row is None or self.row.library_id != self.fs.library_id:
            raise LibraryProbeError("catalog source not found", reason="missing", cleanup="refused", media_id=media_id)
        self.snapshot = MediaSnapshot.from_media(self.row)
        self.path = Path(self.snapshot.path)
        self.library = await authorized_library(self.session, self.fs.library_id, self.snapshot)
        if self.library is None:
            raise self.error("library no longer authorizes probing", "unauthorized")
        if self.purpose == "current":
            if await claim_catalog(self.session, self.snapshot) is None:
                raise self.error(
                    "Another operation updated this file's metadata before the probe started.",
                    "superseded",
                )
            await self._check_idle()

    async def _load_publication(self, attempt_id: int) -> None:
        attempt = await self.session.get(JobAttempt, attempt_id)
        job = await self.session.get(Job, attempt.job_id) if attempt is not None else None
        if (
            attempt is None
            or job is None
            or job.library_id != self.fs.library_id
            or attempt.attempt_number != job.attempts
            or attempt.status != "succeeded"
            or job.status != "succeeded"
        ):
            raise self.error("completed publication job not found", "unauthorized")
        journal = await self.session.scalar(
            select(FinalizationJournal).where(FinalizationJournal.attempt_id == attempt_id)
        )
        publication = await self.session.scalar(
            select(JobEvent).where(JobEvent.attempt_id == attempt_id, JobEvent.type == "publication")
        )
        if journal is None or journal.state not in {"completed", "discard_completed"} or publication is None:
            raise self.error("completed publication record not found", "unauthorized")
        payload = publication.payload
        if payload.get("library_id") != self.fs.library_id or type(payload.get("preserve_timestamps")) is not bool:
            raise self.error("publication settings are missing", "unauthorized")
        self.attempt_id = attempt_id
        self.journal_id = journal.id
        self.job = job
        self.path = Path(journal.destination_path)
        self.row = await self.session.scalar(select(MediaFile).where(MediaFile.path == str(self.path)))
        if self.row is not None:
            if self.row.library_id != self.fs.library_id:
                raise self.error("published path belongs to another file record", "unauthorized")
            self.snapshot = MediaSnapshot.from_media(self.row)
        candidate = self.row if self.row is not None else MediaFile(library_id=self.fs.library_id, path=str(self.path))
        self.library = await authorized_library(self.session, self.fs.library_id, candidate)
        if self.library is None:
            raise self.error("library no longer authorizes publication refresh", "unauthorized")
        if payload["preserve_timestamps"]:
            dates = payload["dates"]
            self.dates = dates["atime_ns"], dates["mtime_ns"]

    async def _check_idle(self) -> None:
        if self.snapshot is None:
            return
        try:
            if self.job_id is not None and self.purpose == "current":
                assert self.request_id is not None
                request = await current_request(self.session, self.job_id, self.request_id)
                self.selected_flow = "flow_snapshot" in request.payload
            await check_file_idle(
                self.session,
                self.snapshot.id,
                excluding_job=self.job_id,
                metadata=self.purpose in {"basic", "statistics"},
            )
        except ActiveJobError as exc:
            raise self.error(str(exc), "busy") from exc

    async def _authorize(self) -> None:
        assert self.path is not None
        candidate = self.snapshot or MediaFile(library_id=self.fs.library_id, path=str(self.path))
        library = await authorized_library(self.session, self.fs.library_id, candidate)
        if library is None:
            raise self.error("library no longer authorizes probing", "unauthorized")
        if self.snapshot is not None and await claim_catalog(self.session, self.snapshot) is None:
            raise self.error(
                "Another operation updated this file's metadata while the probe was waiting or running.",
                "superseded",
            )
        if self.snapshot is None:
            self.row = await self.session.scalar(
                select(MediaFile)
                .where(MediaFile.path == str(self.path))
                .with_for_update(of=MediaFile)
                .execution_options(populate_existing=True)
            )
            if self.row is not None:
                if self.row.library_id != self.fs.library_id:
                    raise self.error("published path now belongs to another file record", "unauthorized")
                self.snapshot = MediaSnapshot.from_media(self.row)
        self.library = library
        if self.purpose in {"basic", "statistics"}:
            job = await self.session.get(Job, self.job_id, populate_existing=True, with_for_update=True)
            attempt = await self.session.get(JobAttempt, self.attempt_id, populate_existing=True, with_for_update=True)
            expected = await self.session.scalar(
                select(JobEvent.payload)
                .where(JobEvent.job_id == self.job_id, JobEvent.type == "metadata_source")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            if (
                job is None
                or attempt is None
                or job.status != "running"
                or attempt.status != "running"
                or attempt.job_id != job.id
                or attempt.attempt_number != job.attempts
                or self.snapshot is None
                or metadata_source(self.snapshot) != expected
            ):
                raise self.error("Metadata attempt no longer owns this source.", "superseded")
            self.job = job
            requested = await self.session.scalar(
                select(JobEvent.id)
                .where(
                    JobEvent.job_id == job.id,
                    JobEvent.attempt_id == attempt.id,
                    JobEvent.type == "metadata_cancel_requested",
                )
                .limit(1)
            )
            if requested is not None:
                raise asyncio.CancelledError
            await self._check_idle()
        if self.purpose == "current":
            await self._check_idle()
        if self.purpose == "published":
            assert self.job is not None
            job = await self.session.get(Job, self.job.id, populate_existing=True, with_for_update=True)
            attempt = await self.session.get(JobAttempt, self.attempt_id, populate_existing=True, with_for_update=True)
            journal = await self.session.get(
                FinalizationJournal, self.journal_id, populate_existing=True, with_for_update=True
            )
            publication = await self.session.scalar(
                select(JobEvent)
                .where(JobEvent.attempt_id == self.attempt_id, JobEvent.type == "publication")
                .execution_options(populate_existing=True)
            )
            if (
                journal is None
                or journal.state not in {"completed", "discard_completed"}
                or journal.destination_path != str(self.path)
                or job is None
                or job.library_id != self.fs.library_id
                or job.status != "succeeded"
                or attempt is None
                or attempt.status != "succeeded"
                or attempt.attempt_number != job.attempts
                or publication is None
            ):
                raise self.error("publication attempt changed during refresh", "superseded")
            self.job = job

    async def _probe(self) -> ProbeResult:
        assert self.stat is not None
        assert self.path is not None
        path = self.path
        await self._authorize()
        settings = await read_settings(self.session)
        if self.purpose == "statistics":
            self.argv_prefix = low_priority_argv()
        raw = None
        if (
            self.purpose == "statistics"
            and self.snapshot is not None
            and self.job is not None
            and self.job.attempts == 1
            and self.snapshot.size == self.stat.st_size
        ):
            stored = await self.session.scalar(select(ProbeRow).where(ProbeRow.media_file_id == self.snapshot.id))
            if stored is not None:
                raw = json.loads(stored.raw_json)
        await self.session.commit()
        if raw is None:
            if self.fs.on_progress is not None:
                self.fs.on_progress("ffprobe", None)
            raw = await self._read_probe()
        self.stat = await wait_workspace_io(self.path.stat)
        source_payload = await wait_workspace_io(lambda: capture_source(path))
        await self._authorize()
        assert self.library is not None
        if self.purpose in {"basic", "statistics"}:
            attempt = await self.session.get(JobAttempt, self.attempt_id)
            assert attempt is not None
            attempt.input_probe = normalize(self.path, self.stat, raw).probe.model_dump(mode="json")
            attempt.source_size = self.stat.st_size
        editable = self.library.mkv_stats and self.path.suffix.lower() == ".mkv" and _lacks_stream_bitrates(raw)
        stats_seen = (
            self.snapshot.first_seen if self.snapshot is not None and self.snapshot.size == self.stat.st_size else None
        )
        if editable and self.purpose != "current":
            try:
                await self._check_idle()
            except LibraryProbeError:
                editable = False
        defer_stats = (
            editable
            and self.purpose in {"current", "published"}
            and settings.mkv_stats_location != ExecutionLocation.LOCAL
        )
        if editable and not defer_stats:
            if self.purpose in {"basic", "statistics"} and self.snapshot is not None:
                seen = stats_seen or utcnow()
                if (
                    self.purpose == "basic"
                    or stats_seen is None
                    or cooldown_hold_until(self.library.cooldown_s, seen, utcnow()) is not None
                ):
                    row, probe = await apply_probe_metadata(
                        self.session,
                        path=self.path,
                        stat=self.stat,
                        raw=raw,
                        row=self.row,
                        library=self.library,
                        purpose="probe",
                    )
                    # The saved raw probe and pending catalog state let statistics work continue after restart.
                    row.probe_id = None
                    row.health_status = "pending"
                    row.health_source = None
                    row.first_seen = seen
                    self.row = row
                    await self._complete(row, probe, pending_stats=True)
                    await self.session.commit()
                    self.metadata_committed = True
                    self.row = row
                    self.snapshot = MediaSnapshot.from_media(row)
                    return ProbeResult(
                        media=self.snapshot,
                        probe=probe,
                        source=source_payload,
                        healthcheck_id=None,
                        diagnostics=tuple(self.diagnostics),
                    )
            if self.purpose == "statistics" and self.fs.stats_allowed is not None and not self.fs.stats_allowed():
                raise self.error("Scanner paused before statistics editing.", "unauthorized")
            # Commit authorization before tools run, then recheck catalog state and the current request.
            await self.session.commit()
            raw = await self._edit(raw)
            source_payload = await wait_workspace_io(lambda: capture_source(path))
            await self._authorize()
        elif self.dates is not None:
            self.effects = True
            path, dates = self.path, self.dates
            await self.session.commit()
            try:
                self.stat = await wait_workspace_io(lambda: restore_source_dates(path, dates))
            finally:
                self.dates = None
            source_payload = await wait_workspace_io(lambda: capture_source(path))
            await self._authorize()
        if raw is None:
            assert self.probe_failure is not None
            raise self.probe_failure
        assert self.library is not None
        transcode_state = (
            (self.row.transcode_status, self.row.transcode_decided, self.row.first_seen)
            if self.purpose == "statistics" and self.effects and self.row is not None
            else None
        )
        row, probe = await apply_probe_metadata(
            self.session,
            path=self.path,
            stat=self.stat,
            raw=raw,
            row=self.row,
            library=self.library,
            purpose="probe" if self.purpose in {"basic", "statistics"} else self.purpose,
        )
        self.row = row
        if transcode_state is not None:
            # Statistics edits keep the pending transcode choice, as node edits do.
            row.transcode_status, row.transcode_decided, row.first_seen = transcode_state
        if self.selected_flow:
            # This refresh belongs to the selected request, not automatic scheduling.
            row.transcode_decided = True
        if defer_stats:
            row.probe_id = None
            row.health_status = "pending"
            row.health_source = None
        if editable and stats_seen is not None:
            # Statistics edits must preserve when the file was first seen.
            row.first_seen = stats_seen
        if self.purpose in {"basic", "statistics"}:
            await self._complete(row, probe, pending_stats=False)
        healthcheck_id = None
        if self.purpose in {"basic", "statistics"}:
            healthcheck_id = await metadata_handoff(self.session, row, self.library)
        await self.session.flush()
        if self.purpose == "current":
            await self._check_idle()
        snapshot = MediaSnapshot.from_media(row)
        await self.session.commit()
        self.metadata_committed = True
        self.snapshot = snapshot
        if defer_stats and self.purpose == "published":
            await enqueue_metadata_job(self.session, row, self.library, kind="mkv_stats", priority=0)
            await self.session.commit()
            return ProbeResult(
                media=MediaSnapshot.from_media(row),
                probe=probe,
                source=source_payload,
                healthcheck_id=None,
                diagnostics=tuple(self.diagnostics),
            )
        if self.purpose == "published":
            await self._authorize()
            assert self.library is not None
            await self.session.refresh(row)
            row.health_status = "pending" if self.library.healthcheck_enabled else "healthy"
            row.health_source = (
                None if self.library.healthcheck_enabled else {"path": str(self.path), **source_identity(self.stat)}
            )
            if self.library.healthcheck_enabled and processing_allowed(self.library, row):
                healthcheck = await enqueue_healthcheck_job(self.session, snapshot.id, self.fs.library_id)
                assert self.job is not None
                actions = TypeAdapter(list[Action]).validate_python(self.job.pre_actions or [])
                notifications = [
                    action.model_dump(mode="json")
                    for action in actions
                    if action.kind == "notify" and "job.failed" in action.on
                ]
                healthcheck.pre_actions = notifications or None
                await self.session.flush()
                healthcheck_id = healthcheck.id
            else:
                assert self.attempt_id is not None
                await finish_artifact_check(self.session, attempt_id=self.attempt_id, library_id=self.fs.library_id)
            await self.session.commit()
            snapshot = MediaSnapshot.from_media(row)
            self.snapshot = snapshot
        return ProbeResult(
            media=snapshot,
            probe=probe,
            source=source_payload,
            healthcheck_id=healthcheck_id,
            diagnostics=tuple(self.diagnostics),
        )

    async def _complete(self, row: MediaFile, probe: ProbeData, *, pending_stats: bool) -> None:
        assert self.job is not None
        assert self.attempt_id is not None
        assert self.library is not None
        attempt = await self.session.get(JobAttempt, self.attempt_id)
        assert attempt is not None
        attempt.input_probe = attempt.input_probe or probe.model_dump(mode="json")
        lines = [
            f"Metadata read for {row.path}.",
            f"Container: {probe.container}. Duration: {probe.duration_s:g}s. Size: {probe.size_bytes} bytes.",
        ]
        for stream in probe.streams:
            details = [f"Stream {stream.index}: {stream.codec_type}, {stream.codec}"]
            if stream.width and stream.height:
                details.append(f"{stream.width}x{stream.height}")
            if stream.channels:
                details.append(f"{stream.channels} channels")
            if stream.language:
                details.append(stream.language)
            lines.append(", ".join(details) + ".")
        attempt.log_data = (attempt.log_data or b"") + ("\n".join(lines) + "\n").encode()
        if pending_stats and self.purpose == "statistics":
            attempt.status = "succeeded"
            attempt.result = "deferred"
            attempt.output_probe = probe.model_dump(mode="json")
            attempt.finished_at = utcnow()
            self.job.hold_until = cooldown_hold_until(self.library.cooldown_s, row.first_seen, utcnow())
            self.job.status = "hold" if self.job.hold_until is not None else "queued"
            self.job.progress = None
            self.session.add(JobEvent(job_id=self.job.id, type="metadata_source", payload=metadata_source(row)))
            return
        await finish(
            self.session,
            self.job.id,
            JobStatus.SUCCEEDED,
            source_size=attempt.source_size,
            output_probe=probe.model_dump(mode="json"),
        )
        row.metadata_priority = 0
        if pending_stats:
            await self.session.flush()
            await enqueue_metadata_job(self.session, row, self.library, kind="mkv_stats", priority=self.job.priority)

    async def _read_probe(self) -> dict:
        assert self.path is not None
        if self.fs.on_tool is not None:
            await self.fs.on_tool(
                {
                    "tool": "ffprobe",
                    "percent": None,
                    "stage": "started",
                    "argv": [
                        *self.argv_prefix,
                        str(self.fs.ffprobe_bin),
                        "-v",
                        "error",
                        "-print_format",
                        "json",
                        "-show_streams",
                        "-show_format",
                        str(self.path),
                    ],
                }
            )
        return await run_ffprobe(
            self.fs.ffprobe_bin,
            self.path,
            on_result=self.fs.on_tool,
            argv_prefix=self.argv_prefix,
        )

    async def _edit(self, raw: dict) -> dict | None:
        assert self.stat is not None
        assert self.path is not None
        before = await wait_workspace_io(self.path.stat)
        if not self.argv_prefix:
            self.argv_prefix = low_priority_argv()
        dates = self.dates or (before.st_atime_ns, before.st_mtime_ns)
        self.dates = dates
        self.effects = True
        edited_stat = None
        result: dict | None = raw
        on_progress = self.fs.on_progress
        try:
            edited = False
            try:
                if on_progress is not None:
                    on_progress("mkvpropedit", None)
                if self.fs.on_tool is not None:
                    await self.fs.on_tool(
                        {
                            "tool": "mkvpropedit",
                            "percent": None,
                            "stage": "started",
                            "argv": [
                                *self.argv_prefix,
                                str(self.fs.mkvpropedit_bin),
                                "--ui-language",
                                "en_US",
                                "--add-track-statistics-tags",
                                str(self.path),
                            ],
                        }
                    )

                async def progress(percent: int) -> None:
                    if on_progress is not None:
                        on_progress("mkvpropedit", percent)
                    if self.fs.on_tool is not None:
                        await self.fs.on_tool({"tool": "mkvpropedit", "percent": percent, "stage": "running"})

                await run_mkvpropedit(
                    self.fs.mkvpropedit_bin,
                    self.path,
                    on_progress=progress,
                    on_result=self.fs.on_tool,
                    argv_prefix=self.argv_prefix,
                )
                edited = True
            except ProbeError as exc:
                self.diagnostics.append(str(exc))
                self.probe_failure = exc
                result = None
            finally:
                edited_stat = await wait_workspace_io(self.path.stat)
                self.stat = edited_stat
            if edited:
                try:
                    if on_progress is not None:
                        on_progress("ffprobe", None)
                    result = await self._read_probe()
                except ProbeError as exc:
                    self.diagnostics.append(str(exc))
                    self.probe_failure = exc
                    result = None
        finally:
            if edited_stat is not None:
                path = self.path
                try:
                    self.stat = await wait_workspace_io(lambda: restore_source_dates(path, dates))
                finally:
                    self.dates = None
        return result

    async def _invalidate(self) -> bool:
        assert self.snapshot is not None
        await self.session.scalar(
            select(Library.id).where(Library.id == self.snapshot.library_id).with_for_update(read=True, of=Library)
        )
        if await claim_catalog(self.session, self.snapshot) is None:
            return False
        row = await self.session.get(MediaFile, self.snapshot.id, populate_existing=True)
        if row is None:
            return False
        probe_id = row.probe_id
        statistics_edit = self.purpose == "statistics" and self.effects
        changed = True
        if self.path is not None and self.stat is not None:
            try:
                stat = await wait_workspace_io(self.path.stat)
            except OSError:
                row.source_identity = None
            else:
                identity = source_identity(stat)
                changed = row.size != stat.st_size
                if changed and not statistics_edit:
                    row.first_seen = utcnow()
                row.size = stat.st_size
                row.mtime_ns = stat.st_mtime_ns
                row.source_identity = identity
        else:
            row.source_identity = None
        row.probe_id = None
        row.health_status = "probe_failed"
        row.health_source = None
        row.last_scanned_at = utcnow()
        if changed:
            if row.transcode_status != TranscodeStatus.TRANSCODED.value and not statistics_edit:
                row.transcode_status = TranscodeStatus.PENDING.value
                row.transcode_decided = self.selected_flow
            row.video_codec = None
            row.width = None
            row.height = None
            row.resolution_bucket = None
            row.duration_s = None
            row.bit_rate = None
            row.medium = "other"
        await self.session.flush()
        if probe_id is not None or self.effects:
            await self.session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == row.id))
        if self.job_id is not None and self.purpose in {"basic", "statistics"}:
            self.session.add(JobEvent(job_id=self.job_id, type="metadata_source", payload=metadata_source(row)))
        await self.session.commit()
        return True

    async def _cleanup(self, exc: BaseException) -> tuple[bool, Exception | None]:
        restoration_error = None
        if (
            self.dates is not None
            and self.path is not None
            and self.stat is not None
            and (not isinstance(exc, LibraryProbeError) or self.effects)
        ):
            path, dates = self.path, self.dates
            try:
                await self._authorize()
                self.effects = True
                self.stat = await wait_workspace_io(lambda: restore_source_dates(path, dates))
            except Exception as restoration:
                restoration_error = restoration
                self.diagnostics.append(f"Date restoration failed ({type(restoration).__name__}): {restoration}")
            finally:
                self.dates = None
        await self.session.rollback()
        if (
            self.metadata_committed
            or self.snapshot is None
            or (isinstance(exc, (LibraryProbeError, asyncio.CancelledError)) and not self.effects)
        ):
            source_current = True
        else:
            source_current = await self._invalidate()
        if source_current and self.purpose == "published" and self.attempt_id is not None:
            # Cleanup has drained. This invocation will not retry, including on cancellation.
            await finish_artifact_check(self.session, attempt_id=self.attempt_id, library_id=self.fs.library_id)
            await self.session.commit()
        return source_current, restoration_error

    async def _fail(self, exc: BaseException) -> Never:
        failure = self.probe_failure or (exc if isinstance(exc, ProbeError) else None)
        cleanup_task = asyncio.create_task(self._cleanup(exc))
        cancelled = isinstance(exc, asyncio.CancelledError)
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            source_current, restoration_error = cleanup_task.result()
        except Exception as cleanup:
            error = LibraryProbeError(
                str(exc) or type(exc).__name__,
                reason="failed",
                cleanup="unresolved",
                media_id=self.media_id,
                diagnostics=(*self.diagnostics, f"Cleanup failed ({type(cleanup).__name__}): {cleanup}"),
                failure=failure,
            )
            error.add_note(f"Cleanup failed ({type(cleanup).__name__}): {cleanup}")
            raise error from exc
        if cancelled and isinstance(exc, asyncio.CancelledError):
            if restoration_error is not None:
                raise LibraryProbeError(
                    str(restoration_error) or type(restoration_error).__name__,
                    reason="failed",
                    cleanup="completed",
                    media_id=self.media_id,
                    diagnostics=tuple(self.diagnostics),
                    failure=restoration_error if isinstance(restoration_error, ProbeError) else None,
                ) from restoration_error
            raise exc
        if isinstance(exc, LibraryProbeError):
            exc.cleanup = "completed" if self.effects or self.metadata_committed else "refused"
            exc.diagnostics = tuple(self.diagnostics)
            raise exc
        reason: Literal["missing", "unauthorized", "busy", "superseded", "failed"] = "failed"
        if not source_current:
            reason = "superseded"
        elif isinstance(exc, FileNotFoundError):
            reason = "missing"
        raise LibraryProbeError(
            str(exc) or type(exc).__name__,
            reason=reason,
            cleanup="completed",
            media_id=self.media_id,
            diagnostics=tuple(self.diagnostics),
            failure=failure,
        ) from exc


async def metadata_handoff(session: AsyncSession, row: MediaFile, library: Library) -> int | None:
    healthcheck_id = None
    if (
        library.healthcheck_enabled
        and row.health_status == "pending"
        and (library.flow_id is None or row.transcode_status == TranscodeStatus.TRANSCODED.value)
        and processing_allowed(library, row)
        and await active_job_for_file(session, row.id) is None
    ):
        healthcheck = await enqueue_healthcheck_job(session, row.id, library.id)
        healthcheck_id = healthcheck.id
    elif row.transcode_status == TranscodeStatus.TRANSCODED.value:
        attempts = await session.scalars(
            select(JobAttempt.id)
            .join(Job, Job.id == JobAttempt.job_id)
            .join(JobEvent, JobEvent.attempt_id == JobAttempt.id)
            .where(
                Job.media_file_id == row.id,
                Job.library_id == library.id,
                Job.kind == "transcode",
                Job.status == "succeeded",
                JobAttempt.status == "succeeded",
                JobAttempt.attempt_number == Job.attempts,
                JobEvent.job_id == Job.id,
                JobEvent.type == "artifact_check_pending",
            )
        )
        for attempt_id in attempts:
            await finish_artifact_check(session, attempt_id=attempt_id, library_id=library.id)
    return healthcheck_id


async def apply_probe_metadata(
    session: AsyncSession,
    *,
    path: Path,
    stat: os.stat_result,
    raw: dict,
    row: MediaFile | None,
    library: Library,
    purpose: Literal["probe", "current", "published", "healthcheck"],
) -> tuple[MediaFile, ProbeData]:
    # Callers own the catalog and disk generation checks, and commit their operation atomically.
    try:
        norm = normalize(path, stat, raw)
    except (AttributeError, TypeError, ValueError) as exc:
        message = f"invalid probe metadata for {path}: {exc}"
        raise ProbeError(message) from exc
    if row is None:
        row = await session.scalar(
            insert(MediaFile)
            .values(
                library_id=library.id,
                path=str(path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                container=norm.container,
                medium=norm.medium,
                last_scanned_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=[MediaFile.path])
            .returning(MediaFile)
        )
        if row is None:
            message = f"Catalog path was assigned to another file during probing: {path}"
            raise ProbeError(message)
    else:
        await session.refresh(row)
    identity = source_identity(stat)
    changed = row.first_seen is None or row.size != stat.st_size
    row.size = stat.st_size
    row.mtime_ns = stat.st_mtime_ns
    row.source_identity = identity
    if changed:
        # Our verified replacement must preserve when the file was first seen.
        if purpose != "published" or row.first_seen is None:
            row.first_seen = utcnow()
        if row.transcode_status != TranscodeStatus.TRANSCODED.value:
            row.transcode_status = TranscodeStatus.PENDING.value
            row.transcode_decided = False
    row.container = norm.container
    row.video_codec = norm.video_codec
    row.width = norm.width
    row.height = norm.height
    row.resolution_bucket = norm.resolution_bucket
    row.duration_s = norm.duration_s
    row.bit_rate = norm.bit_rate
    row.medium = norm.medium
    if (
        purpose != "healthcheck"
        and not library.healthcheck_enabled
        and (purpose != "current" or changed or row.health_status in {"pending", "probe_failed"})
    ):
        row.health_status = "healthy"
        row.health_source = {"path": str(path), **identity}
    elif (
        changed
        or row.health_status == "probe_failed"
        or purpose == "published"
        or (purpose == "probe" and row.health_source != {"path": str(path), **identity})
    ):
        row.health_status = "pending"
        row.health_source = None
    row.last_scanned_at = utcnow()
    await session.flush()
    # The probe FK cycle requires null-before-delete ordering.
    row.probe_id = None
    await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == row.id))
    probe = ProbeRow(media_file_id=row.id, raw_json=json.dumps(raw, sort_keys=True), created_at=utcnow())
    session.add(probe)
    await session.flush()
    row.probe_id = probe.id
    return row, norm.probe
