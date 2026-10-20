from __future__ import annotations

import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Annotated, Literal

from common.media.probe import ProbeData
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator
from sqlalchemy import case, delete, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import load_only, with_expression

from server.api.auth import PrincipalDep, current_principal
from server.event_rows import ACTIVE_FILE_JOB_STATUSES, media_file_row
from server.events import publish_file_status, publish_job_status
from server.flows.batch import BatchDryRuns, BatchError, BatchStatus
from server.jobs import JobRequestError
from server.metadata_jobs import metadata_source
from server.models import Job, JobAttempt, JobEvent, Library, MediaFile, ProbeRow, utcnow
from server.nodehub.hub import probe_for_file
from server.queue.store import (
    ActiveJobError,
    check_file_idle,
    get_media_file,
)
from server.search import count_files, list_files, search_files
from server.serialize import iso_utc
from server.workspace import recovery_jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker


router = APIRouter(prefix="/api/files", tags=["files"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)

_ACTIVE_JOB_STATUSES = ACTIVE_FILE_JOB_STATUSES


class BulkBody(BaseModel):
    action: Literal["healthcheck", "transcode", "skip_cooldown"]
    file_ids: list[Annotated[PositiveInt, Field(strict=True)]] = Field(min_length=1)

    @field_validator("file_ids")
    @classmethod
    def unique_ids(cls, ids: list[int]) -> list[int]:
        return list(dict.fromkeys(ids))


@router.post("/bulk", status_code=202, summary="Apply an action to selected files")
async def post_bulk(body: BulkBody, request: Request) -> dict:
    if body.action == "skip_cooldown":
        accepted, failed = [], []
        for file_id in body.file_ids:
            try:
                await skip_cooldown(file_id, request)
            except HTTPException as exc:
                failed.append({"file_id": file_id, "status": exc.status_code, "detail": exc.detail})
            else:
                accepted.append({"file_id": file_id})
        return {"accepted": accepted, "failed": failed}
    try:
        return await request.app.state.jobs.files(body.file_ids, kind=body.action)
    except JobRequestError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _file_dict(file: MediaFile, active_job: Job | None = None, *, library: Library | None) -> dict:
    return media_file_row(file, active_job, library=library)


def probe_summary(probe) -> dict | None:
    if probe is None:
        return None
    return {
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
                "filename": stream.filename,
                "mimetype": stream.mimetype,
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
                "disposition_attached_pic": stream.disposition_attached_pic,
            }
            for stream in probe.streams
        ],
    }


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "error": job.error,
        "created_at": iso_utc(job.created_at),
    }


def _change_stats(raw: dict | None) -> dict | None:
    if raw is None:
        return None
    probe = ProbeData.model_validate(raw)
    video = next((stream for stream in probe.streams if stream.codec_type == "video"), None)
    return {
        "size": probe.size_bytes,
        "duration_s": probe.duration_s,
        "bit_rate": round(probe.size_bytes * 8 / probe.duration_s) if probe.size_bytes and probe.duration_s else None,
        "video_codec": video.codec if video else None,
        "width": video.width if video else None,
        "height": video.height if video else None,
        "audio": sum(1 for stream in probe.streams if stream.codec_type == "audio"),
        "subtitle": sum(1 for stream in probe.streams if stream.codec_type == "subtitle"),
    }


def _snapshot_probe(raw: dict | None, size: int | None) -> dict | None:
    summary = probe_summary(ProbeData.model_validate(raw)) if raw is not None else None
    if summary is not None and size is not None:
        summary["size_bytes"] = size
    return summary


@router.get("", summary="List or search files")
async def get_files(
    request: Request,
    query: Annotated[str | None, Query()] = None,
    library_id: Annotated[int | None, Query()] = None,
    transcode_status: Annotated[str | None, Query(description="Comma-separated transcode statuses.")] = None,
    health_status: Annotated[str | None, Query(description="Comma-separated health statuses.")] = None,
    limit: Annotated[int, Query(ge=1, le=999)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Literal[
        "id", "path", "size", "duration_s", "bit_rate", "transcode_status", "health_status", "created_at", "updated_at"
    ] = "id",
    sort_dir: Literal["asc", "desc"] = "desc",
) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        if query is not None and query.strip():
            files = await search_files(
                session,
                query,
                limit,
                offset,
                library_id=library_id,
                transcode_status=transcode_status,
                health_status=health_status,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        else:
            files = await list_files(
                session,
                limit,
                offset,
                library_id=library_id,
                transcode_status=transcode_status,
                health_status=health_status,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
        total = await count_files(
            session,
            query if query and query.strip() else None,
            library_id=library_id,
            transcode_status=transcode_status,
            health_status=health_status,
        )
        ids = [file.id for file in files]
        active_jobs: dict[int, Job] = {}
        changes: dict[int, JobAttempt] = {}
        libraries: dict[int, Library] = {}
        if ids:
            libraries = {
                library.id: library
                for library in await session.scalars(
                    select(Library).where(Library.id.in_({f.library_id for f in files}))
                )
            }
            rows = await session.execute(
                select(Job)
                .where(Job.media_file_id.in_(ids), Job.status.in_(_ACTIVE_JOB_STATUSES))
                .order_by(Job.waiting_metadata.desc(), Job.id)
                .options(
                    load_only(
                        Job.id,
                        Job.media_file_id,
                        Job.kind,
                        Job.status,
                        Job.progress,
                        Job.updated_at,
                        raiseload=True,
                    )
                )
            )
            active_jobs = {job.media_file_id: job for job in rows.scalars()}
            change_rows = await session.execute(
                select(Job.media_file_id, JobAttempt)
                .join(JobAttempt, JobAttempt.job_id == Job.id)
                .where(
                    Job.media_file_id.in_(ids),
                    Job.kind == "transcode",
                    JobAttempt.result == "succeeded",
                )
                .distinct(Job.media_file_id)
                .order_by(Job.media_file_id, JobAttempt.id.desc())
                .options(
                    load_only(
                        JobAttempt.job_id,
                        JobAttempt.attempt_number,
                        raiseload=True,
                    ),
                    *(
                        with_expression(column, case((column == JSONB.NULL, None), else_=column.op("-")("raw")))
                        for column in (JobAttempt.input_probe, JobAttempt.output_probe)
                    ),
                )
            )
            changes = dict(change_rows.all())
    results = []
    for file in files:
        row = _file_dict(file, active_jobs.get(file.id), library=libraries.get(file.library_id))
        attempt = changes.get(file.id)
        if attempt is not None:
            before = _change_stats(attempt.input_probe)
            after = _change_stats(attempt.output_probe)
            if before is not None and after is not None:
                row["last_change"] = {
                    "job_id": attempt.job_id,
                    "attempt_number": attempt.attempt_number,
                    "before": before,
                    "after": after,
                }
        results.append(row)
    return {"items": results, "pagination": {"limit": limit, "offset": offset, "total": total}}


class SelectedDryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    file_ids: list[Annotated[PositiveInt, Field(strict=True)]] = Field(min_length=1)

    @field_validator("file_ids")
    @classmethod
    def unique_ids(cls, ids: list[int]) -> list[int]:
        return sorted(set(ids))


@router.post("/dry-runs", status_code=202, summary="Preview selected indexed files")
async def start_dry_run(body: SelectedDryRunBody, request: Request, principal: PrincipalDep) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.start(None, body.flow_id, body.expected_version, principal, file_ids=body.file_ids)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload()


@router.get("/dry-runs", summary="List retained previews for selected files")
async def list_dry_runs(
    request: Request,
    principal: PrincipalDep,
    file_ids: Annotated[list[PositiveInt], Query(min_length=1)],
) -> list[dict]:
    service: BatchDryRuns = request.app.state.dry_runs
    return await service.list_reports(None, principal, file_ids=file_ids)


@router.get("/dry-runs/{run_id}", summary="Read selected-file preview progress and results")
async def read_dry_run(
    run_id: str,
    request: Request,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[list[BatchStatus] | None, Query()] = None,
) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(None, run_id, principal)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload(limit=limit, offset=offset, statuses=status)


@router.post("/dry-runs/{run_id}/cancel", summary="Cancel a selected-file preview")
async def cancel_dry_run(run_id: str, request: Request, principal: PrincipalDep) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(None, run_id, principal)
        await service.cancel(report)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload()


@router.get("/dry-runs/{run_id}/export.csv", summary="Export retained selected-file preview results")
async def export_dry_run(run_id: str, request: Request, principal: PrincipalDep) -> Response:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(None, run_id, principal)
        content = service.export(report)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="batch-dry-run-{report.id}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{file_id}", summary="Show a file with probe and job history")
async def read_file(file_id: int, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        file = await get_media_file(session, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        probe = await probe_for_file(session, file)
        library = await session.get(Library, file.library_id)
        job_rows = await session.execute(
            select(Job)
            .where(Job.media_file_id == file_id)
            .order_by(Job.id.desc())
            .options(
                load_only(
                    Job.id,
                    Job.kind,
                    Job.status,
                    Job.error,
                    Job.created_at,
                    Job.progress,
                    Job.updated_at,
                    raiseload=True,
                )
            )
        )
        jobs = list(job_rows.scalars())
        attempts = list(
            await session.scalars(
                select(JobAttempt)
                .join(Job, JobAttempt.job_id == Job.id)
                .where(Job.media_file_id == file_id, Job.kind == "transcode")
                .order_by(Job.id.desc(), JobAttempt.attempt_number.desc())
                .options(
                    load_only(
                        JobAttempt.job_id,
                        JobAttempt.attempt_number,
                        JobAttempt.created_at,
                        JobAttempt.finished_at,
                        JobAttempt.result,
                        JobAttempt.source_size,
                        JobAttempt.output_size,
                        raiseload=True,
                    ),
                    *(
                        with_expression(column, case((column == JSONB.NULL, None), else_=column.op("-")("raw")))
                        for column in (JobAttempt.input_probe, JobAttempt.output_probe)
                    ),
                )
            )
        )
        stages = await session.execute(
            select(
                JobEvent.id,
                JobEvent.attempt_id,
                JobEvent.ts,
                JobEvent.payload["number"].as_integer().label("number"),
                case(
                    (JobEvent.payload["output_probe"] == JSONB.NULL, None),
                    else_=JobEvent.payload["output_probe"].op("-")("raw"),
                ).label("output_probe"),
                JobEvent.payload["output_size"].label("output_size"),
                JobEvent.payload["result"].as_string().label("result"),
            )
            .join(JobAttempt, JobEvent.attempt_id == JobAttempt.id)
            .join(Job, JobAttempt.job_id == Job.id)
            .where(Job.media_file_id == file_id, Job.kind == "transcode", JobEvent.type == "stage")
            .order_by(JobEvent.id)
        )
        stages_by_attempt: dict[int, list] = {}
        for stage in stages:
            stages_by_attempt.setdefault(stage.attempt_id, []).append(stage)
    active_job = next((job for job in jobs if job.status in _ACTIVE_JOB_STATUSES), None)
    detail = _file_dict(file, active_job, library=library)
    summary = probe_summary(probe)
    # Raw ffprobe rides the detail endpoint only. List and job responses
    # keep the strong-field projection.
    if probe is not None and probe.raw is not None and summary is not None:
        summary["raw"] = probe.raw
    detail["probe"] = summary
    detail["jobs"] = [_job_dict(job) for job in jobs]
    snapshots = []
    for attempt in attempts:
        original = _snapshot_probe(attempt.input_probe, attempt.source_size)
        if original is not None:
            snapshots.append(
                {
                    "id": f"attempt-{attempt.id}-original",
                    "job_id": attempt.job_id,
                    "attempt_number": attempt.attempt_number,
                    "stage_number": None,
                    "kind": "original",
                    "created_at": iso_utc(attempt.created_at),
                    "probe": original,
                    "result": attempt.result,
                }
            )
        stage_probes = []
        for stage in stages_by_attempt.get(attempt.id, []):
            output = _snapshot_probe(stage.output_probe, stage.output_size)
            if output is None:
                continue
            stage_probes.append(output)
            snapshots.append(
                {
                    "id": f"stage-{stage.id}",
                    "job_id": attempt.job_id,
                    "attempt_number": attempt.attempt_number,
                    "stage_number": stage.number,
                    "kind": "stage",
                    "created_at": iso_utc(stage.ts),
                    "probe": output,
                    "result": stage.result,
                }
            )
        output = _snapshot_probe(attempt.output_probe, attempt.output_size)
        if output is not None and output not in stage_probes:
            snapshots.append(
                {
                    "id": f"attempt-{attempt.id}-result",
                    "job_id": attempt.job_id,
                    "attempt_number": attempt.attempt_number,
                    "stage_number": None,
                    "kind": "result",
                    "created_at": iso_utc(attempt.finished_at or attempt.created_at),
                    "probe": output,
                    "result": attempt.result,
                }
            )
    detail["media_snapshots"] = snapshots
    last = max((attempt for attempt in attempts if attempt.result == "succeeded"), key=lambda row: row.id, default=None)
    if last is not None:
        detail["last_transcode"] = {
            "job_id": last.job_id,
            "attempt_number": last.attempt_number,
            "source_size": last.source_size,
            "output_size": last.output_size,
            "input_probe": probe_summary(ProbeData.model_validate(last.input_probe))
            if last.input_probe is not None
            else None,
            "output_probe": probe_summary(ProbeData.model_validate(last.output_probe))
            if last.output_probe is not None
            else None,
        }
    return detail


@router.post("/{file_id}/skip-cooldown", summary="Expire a file's cooldown")
async def skip_cooldown(file_id: int, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        file = await session.get(MediaFile, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        library = await session.get(Library, file.library_id, with_for_update={"read": True})
        if library is None or library.deleted:
            raise HTTPException(status_code=409, detail="Library is unavailable.")
        file = await session.get(MediaFile, file_id, populate_existing=True, with_for_update=True)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        if file.library_id != library.id:
            raise HTTPException(status_code=409, detail="File library changed. Retry the request.")
        job = await session.scalar(
            select(Job)
            .where(
                Job.media_file_id == file_id,
                Job.status.in_(_ACTIVE_JOB_STATUSES),
                ~((Job.status == "pending") & Job.waiting_metadata.is_(True)),
            )
            .with_for_update()
        )
        if job is not None and (job.status != "hold" or job.delete_requested):
            raise HTTPException(status_code=409, detail="File has active work. Only cooldown holds can be skipped.")
        if job is None and file.transcode_status != "pending" and file.health_status != "pending":
            raise HTTPException(status_code=409, detail="File has no pending work.")
        try:
            await check_file_idle(
                session,
                file_id,
                excluding_job=job.id if job is not None else None,
                metadata=job is not None and job.kind in {"probe", "mkv_stats"},
            )
        except ActiveJobError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        now = utcnow()
        file.first_seen = min(file.first_seen.replace(tzinfo=UTC), now - timedelta(seconds=library.cooldown_s))
        if job is not None:
            job.hold_until = now
            if job.kind in {"probe", "mkv_stats"}:
                # Metadata source checks include the cooldown baseline in the snapshot.
                session.add(JobEvent(job_id=job.id, type="metadata_source", payload=metadata_source(file)))
            session.add(
                JobEvent(job_id=job.id, type="cooldown_skipped", payload={"reason": "Cooldown skipped by operator."})
            )
        await session.commit()
    if job is not None:
        await publish_job_status(request.app.state.events, db, job.id)
    else:
        await publish_file_status(request.app.state.events, db, file_id)
    request.app.state.staging.wake()
    request.app.state.probes.wake()
    request.app.state.flow_enqueue.wake()
    return {"id": file_id}


@router.post("/{file_id}/probe", status_code=202, summary="Queue a fresh metadata probe")
async def probe_file(file_id: int, request: Request) -> dict:
    if request.app.state.scanner.paused:
        raise HTTPException(status_code=409, detail="Activity is paused. Resume activity before probing.")
    if request.app.state.probes.failed:
        raise HTTPException(status_code=409, detail="Probe service failed. Check the server logs.")
    async with request.app.state.db() as session:
        file = await session.get(MediaFile, file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        library = await session.get(Library, file.library_id, with_for_update={"read": True, "of": Library})
        if library is None or library.suspended or library.deleted:
            raise HTTPException(status_code=409, detail="Library is unavailable or suspended.")
        file = await session.get(MediaFile, file_id, populate_existing=True, with_for_update=True)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        if file.library_id != library.id:
            raise HTTPException(status_code=409, detail="File library changed. Retry the request.")
        active = await session.scalar(
            select(Job.id)
            .where(
                Job.media_file_id == file_id,
                or_(
                    Job.status.in_(ACTIVE_FILE_JOB_STATUSES),
                    Job.id.in_(recovery_jobs()),
                    Job.id.in_(request.app.state.staging.active_jobs),
                ),
            )
            .limit(1)
        )
        if active is not None:
            raise HTTPException(
                status_code=409, detail="File has active work or pending recovery. Finish it before probing."
            )
        file.probe_id = None
        if file.health_status not in {"healthy", "corrupt"} or file.health_source != {
            "path": file.path,
            "size": file.size,
        }:
            file.health_status = "pending"
            file.health_source = None
        file.metadata_cancelled = False
        file.metadata_priority = 1
        await session.flush()
        await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == file_id))
        from server.metadata_jobs import enqueue_metadata_job

        job = await enqueue_metadata_job(session, file, library, kind="probe", priority=1)
        await session.commit()
    request.app.state.probes.wake()
    await publish_file_status(request.app.state.events, request.app.state.db, file_id)
    await publish_job_status(request.app.state.events, request.app.state.db, job.id)
    return {"file_id": file_id, "job_id": job.id, "kind": "probe", "status": job.status}


@router.post("/{file_id}/healthcheck", status_code=status.HTTP_202_ACCEPTED, summary="Enqueue a healthcheck")
async def post_healthcheck(file_id: int, request: Request) -> dict:
    return await _request_file(file_id, request, kind="healthcheck")


class TranscodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: PositiveInt
    expected_version: PositiveInt


@router.post("/{file_id}/transcode", status_code=202, summary="Enqueue a transcode")
async def post_transcode(
    file_id: int, request: Request, body: TranscodeBody | None = None, *, refresh: bool = False
) -> dict:
    return await _request_file(
        file_id,
        request,
        kind="transcode",
        refresh=refresh,
        selected_flow=(body.flow_id, body.expected_version) if body is not None else None,
    )


async def _request_file(
    file_id: int,
    request: Request,
    *,
    kind: Literal["transcode", "healthcheck"],
    refresh: bool = False,
    selected_flow: tuple[int, int] | None = None,
) -> dict:
    try:
        result = await request.app.state.jobs.files([file_id], kind=kind, refresh=refresh, selected_flow=selected_flow)
    except JobRequestError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    if result["failed"]:
        failure = result["failed"][0]
        raise HTTPException(status_code=failure["status"], detail=failure["detail"])
    return {key: value for key, value in result["accepted"][0].items() if key != "file_id"}
