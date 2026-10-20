from __future__ import annotations

import asyncio
import logging
from itertools import batched
from typing import TYPE_CHECKING, Annotated, Literal

from common.process import drain
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator
from sqlalchemy import select

from server.api.auth import PrincipalDep, current_principal
from server.flows.batch import BatchDryRuns, BatchError, BatchStatus
from server.models import Library
from server.processing_cutoff import DisabledCutoff, ProcessingCutoff
from server.queue.store import create_library, get_library, list_libraries, update_library
from server.scheduler.schedule import GridSchedule
from server.serialize import iso_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

router = APIRouter(prefix="/api/libraries", tags=["libraries"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)
QUERY_BATCH = 500


def _clean_roots(value: list[str]) -> list[str]:
    roots = [root.strip() for root in value]
    if not roots or not all(roots):
        msg = "roots must be a non-empty list of non-empty path strings"
        raise ValueError(msg)
    return roots


class LibraryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    roots: list[str]
    cooldown_s: int = Field(default=0, ge=0)
    schedule: GridSchedule | None = None
    watch: bool = False
    suspended: bool = False
    scan_on_start: bool = False
    scan_interval_s: int | None = Field(default=None, gt=0)
    mkv_stats: bool = False
    healthcheck_enabled: bool = False
    processing_cutoff: ProcessingCutoff | DisabledCutoff = Field(default_factory=DisabledCutoff)
    trash_mode: Literal["none", "transcode", "all"] = "none"
    trash_retention_days: int = Field(default=30, ge=1)
    flow_id: int | None = Field(default=None, ge=1)

    @field_validator("roots")
    @classmethod
    def _roots_valid(cls, value: list[str]) -> list[str]:
        return _clean_roots(value)


class LibraryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    roots: list[str] | None = None
    cooldown_s: int | None = Field(default=None, ge=0)
    schedule: GridSchedule | None = None
    watch: bool | None = None
    suspended: bool = False
    scan_on_start: bool | None = None
    scan_interval_s: int | None = Field(default=None, gt=0)
    mkv_stats: bool | None = None
    healthcheck_enabled: bool = False
    processing_cutoff: ProcessingCutoff | DisabledCutoff = Field(default_factory=DisabledCutoff)
    trash_mode: Literal["none", "transcode", "all"] | None = None
    trash_retention_days: int | None = Field(default=None, ge=1)
    flow_id: int | None = Field(default=None, ge=1)

    @field_validator("roots")
    @classmethod
    def _roots_valid(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_roots(value)


def _library_dict(library: Library) -> dict:
    return {
        "id": library.id,
        "created_at": iso_utc(library.created_at),
        "updated_at": iso_utc(library.updated_at),
        "deleted": library.deleted,
        "delete_error": library.delete_error,
        "name": library.name,
        "roots": library.roots,
        "cooldown_s": library.cooldown_s,
        "schedule": library.schedule,
        "watch": library.watch,
        "suspended": library.suspended,
        "scan_on_start": library.scan_on_start,
        "scan_interval_s": library.scan_interval_s,
        "mkv_stats": library.mkv_stats,
        "healthcheck_enabled": library.healthcheck_enabled,
        "processing_cutoff": library.processing_cutoff,
        "trash_mode": library.trash_mode,
        "trash_retention_days": library.trash_retention_days,
        "flow_id": library.flow_id,
    }


@router.get("", summary="List libraries")
async def get_libraries(request: Request) -> list[dict]:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        return [_library_dict(library) for library in await list_libraries(session)]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a library")
async def post_library(body: LibraryBody, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        try:
            library = await create_library(session, **body.model_dump())
            await session.commit()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if library.flow_id is not None:
        request.app.state.flow_enqueue.wake()
    await request.app.state.scanner.update_watchers()
    LOG.info("Created library %s", library.id, extra={"operation": "library_create", "library_id": library.id})
    return _library_dict(library)


@router.get("/progress", summary="Read library discovery and probing progress")
async def get_library_progress(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    libraries = await request.app.state.probes.progress_snapshot()
    return {
        "scanner": {"paused": request.app.state.scanner.paused, "failed": request.app.state.scanner.failed},
        "probing": {"failed": request.app.state.probes.failed},
        "libraries": libraries,
    }


@router.get("/scanner", summary="Read global activity pause and scanner state")
async def get_scanner(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"paused": request.app.state.scanner.paused, "failed": request.app.state.scanner.failed}


@router.post("/scanner/{action}", summary="Pause or resume activity across all libraries")
async def control_scanner(action: Literal["pause", "resume"], request: Request) -> dict:
    async def apply() -> dict:
        paused = action == "pause"
        # Claims finish before the persisted flag and runtime gate change together.
        async with (
            request.app.state.hub.dispatch_lock,
            request.app.state.healthchecks.dispatch_lock,
            request.app.state.probes.dispatch_lock,
        ):
            await request.app.state.store.put("activity_paused", paused)
            request.app.state.scanner.set_paused(paused=paused)
            request.app.state.probes.wake()
            request.app.state.hub.wake()
            request.app.state.healthchecks.wake()
            return {"paused": paused, "failed": request.app.state.scanner.failed}

    # A disconnected client must not leave the persisted flag ahead of the live gate.
    return await drain(asyncio.create_task(apply()))


class BatchScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library_ids: list[Annotated[PositiveInt, Field(strict=True)]] = Field(min_length=1)

    @field_validator("library_ids")
    @classmethod
    def unique_ids(cls, ids: list[int]) -> list[int]:
        return list(dict.fromkeys(ids))


@router.post("/scan", status_code=202, summary="Queue scans for selected libraries")
async def scan_libraries(body: BatchScanBody, request: Request) -> dict:
    if request.app.state.scanner.paused:
        raise HTTPException(status_code=409, detail="Activity is paused. Resume activity before scanning.")
    async with request.app.state.db() as session:
        libraries = {}
        for ids in batched(body.library_ids, QUERY_BATCH, strict=False):
            libraries.update(
                (library.id, library) for library in await session.scalars(select(Library).where(Library.id.in_(ids)))
            )
    outcomes = []
    for identifier in body.library_ids:
        library = libraries.get(identifier)
        if library is None:
            outcomes.append({"library_id": identifier, "status": "missing", "detail": "Library not found."})
        elif library.suspended:
            outcomes.append({"library_id": identifier, "status": "suspended", "detail": "Library is suspended."})
        else:
            result = request.app.state.scanner.queue_scan(identifier, "manual")
            outcomes.append({"library_id": identifier, "status": result})
            LOG.info("Scan request for library %s: %s", identifier, result)
    return {"outcomes": outcomes}


@router.patch("/{library_id}", summary="Update a library")
async def patch_library(library_id: int, body: LibraryPatch, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    fields = body.model_dump(exclude_unset=True)
    if fields.get("suspended") is False and len(fields) != 1:
        raise HTTPException(status_code=409, detail="resume a library separately from editing its settings")
    async with db() as session:
        library = await get_library(session, library_id)
        if library is not None and library.deleted:
            raise HTTPException(status_code=409, detail="Library is being deleted. Its settings cannot be changed.")
    async with request.app.state.scanner.library_mutation():
        async with db() as session:
            try:
                library = await update_library(session, library_id, fields)
                await session.commit()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        await request.app.state.probes.check_library_access()
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    if library.flow_id is not None:
        request.app.state.flow_enqueue.wake()
    request.app.state.probes.wake()
    request.app.state.healthchecks.wake()
    request.app.state.hub.wake()
    LOG.info("Updated library %s", library_id, extra={"operation": "library_update", "library_id": library_id})
    return _library_dict(library)


@router.get("/{library_id}", summary="Show a library")
async def read_library(library_id: int, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        library = await get_library(session, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    return _library_dict(library)


@router.delete("/{library_id}", status_code=status.HTTP_202_ACCEPTED, summary="Delete a library in the background")
async def delete_library(library_id: int, request: Request, response: Response) -> dict:
    if not request.app.state.services_running:
        raise HTTPException(status_code=409, detail="Server services are stopped. Try again after they resume.")
    try:
        result = await request.app.state.library_deletion.request(library_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="library not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["Location"] = f"/api/libraries/{library_id}"
    return result


@router.post("/{library_id}/scan", status_code=status.HTTP_202_ACCEPTED, summary="Scan a library now")
async def scan_library_now(library_id: int, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        library = await get_library(session, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    if library.suspended:
        raise HTTPException(
            status_code=409, detail="Library is suspended. Review its settings and resume it before scanning."
        )
    if request.app.state.scanner.paused:
        raise HTTPException(status_code=409, detail="Activity is paused. Resume activity before scanning.")
    result = request.app.state.scanner.queue_scan(library_id, "manual")
    if result == "stopped":
        raise HTTPException(status_code=409, detail="Scanner is stopped. Try again after server services resume.")
    LOG.info("Queued scan for library %s", library_id, extra={"operation": "library_scan", "library_id": library_id})
    return {"library_id": library_id, "status": result}


@router.post("/{library_id}/scan/cancel", summary="Cancel a library scan")
async def cancel_scan(library_id: int, request: Request) -> dict:
    async with request.app.state.db() as session:
        library = await get_library(session, library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    result = request.app.state.scanner.cancel_scan(library_id)
    request.app.state.probes.wake()
    progress = next(
        (state for state in await request.app.state.probes.progress_snapshot() if state["library_id"] == library_id),
        None,
    )
    if progress is None:
        raise HTTPException(status_code=404, detail="library not found")
    request.app.state.events.update_library_progress(library_id, probing=progress["probing"])
    return {"library_id": library_id, "status": result, "progress": progress}


class BatchDryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


@router.post("/{library_id}/dry-runs", status_code=202, summary="Preview all indexed library files")
async def start_dry_run(library_id: int, body: BatchDryRunBody, request: Request, principal: PrincipalDep) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.start(library_id, body.flow_id, body.expected_version, principal)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload()


@router.get("/{library_id}/dry-runs", summary="List retained library previews")
async def list_dry_runs(library_id: int, request: Request, principal: PrincipalDep) -> list[dict]:
    service: BatchDryRuns = request.app.state.dry_runs
    return await service.list_reports(library_id, principal)


@router.get("/{library_id}/dry-runs/{run_id}", summary="Read library preview progress and results")
async def read_dry_run(
    library_id: int,
    run_id: str,
    request: Request,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Annotated[list[BatchStatus] | None, Query()] = None,
) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(library_id, run_id, principal)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload(limit=limit, offset=offset, statuses=status)


@router.post("/{library_id}/dry-runs/{run_id}/cancel", summary="Cancel a library preview")
async def cancel_dry_run(library_id: int, run_id: str, request: Request, principal: PrincipalDep) -> dict:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(library_id, run_id, principal)
        await service.cancel(report)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return report.payload()


@router.get("/{library_id}/dry-runs/{run_id}/export.csv", summary="Export retained library preview results")
async def export_dry_run(library_id: int, run_id: str, request: Request, principal: PrincipalDep) -> Response:
    service: BatchDryRuns = request.app.state.dry_runs
    try:
        report = await service.get(library_id, run_id, principal)
        content = service.export(report)
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="library-dry-run-{report.id}.csv"',
            "Cache-Control": "no-store",
        },
    )
