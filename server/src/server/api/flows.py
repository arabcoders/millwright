from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common.facts import FACT_TYPES
from common.media.probe import ProbeData  # noqa: TC002 -- pydantic resolves annotations at runtime
from common.metadata import SidecarMetadata
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from server.api.auth import current_principal
from server.flows.definition import FlowDefinition
from server.flows.dryrun import DryRunResult, dry_run
from server.flows.engine import decision_entry
from server.flows.store import (
    FlowNameConflictError,
    FlowNotFoundError,
    FlowVersionError,
    delete_flow,
    get_flow,
    list_flows,
    rename_flow,
    save_flow,
)
from server.flows.validation import validate_definition
from server.models import Flow, Library
from server.nodehub.hub import probe_for_file
from server.plugins.migrations import migrate_definition
from server.queue.store import get_media_file
from server.serialize import iso_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from server.plugins.host import PluginHost

router = APIRouter(prefix="/api/flows", tags=["flows"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int
    flow: dict[str, Any]


class ProbeDryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow: dict[str, Any]
    probe: ProbeData
    sidecar_subtitles: list[str] = Field(default_factory=list)
    sidecar_metadata: SidecarMetadata = Field(default_factory=SidecarMetadata)


class RenameFlowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    definition: dict[str, Any]
    expected_version: int = Field(ge=1)


class SaveFlowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: dict[str, Any]
    expected_version: int | None = Field(default=None, ge=1)


def _flow_dict(flow: Flow) -> dict:
    return {
        "id": flow.id,
        "created_at": iso_utc(flow.created_at),
        "name": flow.name,
        "version": flow.version,
        "definition": json.loads(flow.definition),
        "updated_at": iso_utc(flow.updated_at),
    }


def _dry_run_dict(result: DryRunResult) -> dict:
    flow = result.flow
    return {
        "status": flow.status,
        "reason": flow.reason,
        "steps": [decision_entry(outcome) for outcome in flow.steps],
        "facets": flow.facets,
        "plan": flow.plan.model_dump(mode="json") if flow.plan is not None else None,
        "continuation": flow.continuation.model_dump(mode="json") if flow.continuation is not None else None,
        "actions": [action.model_dump(mode="json") for action in flow.actions],
        "post_actions": [action.model_dump(mode="json") for action in flow.post_actions],
        "requires": flow.requires,
        "requires_encoders": flow.requires_encoders,
        "argv": result.render.argv if result.render is not None else None,
        "stream_log": [
            {
                "stream_index": entry.stream_index,
                "codec": entry.codec,
                "language": entry.language,
                "title": entry.title,
                "outcome": entry.outcome,
                "detail": entry.detail,
            }
            for entry in (result.render.stream_log if result.render is not None else [])
        ],
    }


@router.get("", summary="List flows")
async def get_flows(request: Request) -> list[dict]:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        return [_flow_dict(flow) for flow in await list_flows(session)]


@router.get("/facts", summary="List media facts")
async def get_facts() -> list[dict[str, str]]:
    return [{"key": key, "type": FACT_TYPES[key]} for key in sorted(FACT_TYPES)]


@router.post("/normalize", summary="Migrate plugin configuration in a flow")
async def normalize_flow(definition: dict[str, Any], request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    try:
        migrated = FlowDefinition.model_validate(
            migrate_definition(definition, (info.manifest for info in host.plugins()))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return migrated.model_dump(mode="json")


PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"


@router.get("/presets", summary="List starter flow presets")
async def starter_presets() -> list[dict]:
    presets: list[dict] = []
    for path in sorted(PRESETS_DIR.glob("*.flow.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            presets.append(
                {
                    "id": path.stem.removesuffix(".flow"),
                    "name": envelope["name"],
                    "description": envelope["description"],
                    "route": envelope["route"],
                    "concepts": envelope["concepts"],
                    "definition": FlowDefinition.model_validate(envelope["definition"]).model_dump(mode="json"),
                }
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"starter preset '{path.name}' is invalid: {error}",
            ) from error
    return presets


@router.get("/export.zip", summary="Export all flows")
async def export_flows(request: Request) -> Response:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        flows = await list_flows(session)

    archive = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for flow in flows:
            base_name = re.sub(r"[^A-Za-z0-9._-]", "_", flow.name).strip(".") or "flow"
            entry_name = f"{base_name}.flow.json"
            suffix = 2
            while entry_name in used_names:
                entry_name = f"{base_name}_{suffix}.flow.json"
                suffix += 1
            used_names.add(entry_name)
            envelope = {
                "_type": "flow",
                "_version": 1,
                "name": flow.name,
                "definition": _flow_dict(flow)["definition"],
            }
            output.writestr(entry_name, json.dumps(envelope))

    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=flows.zip"},
    )


@router.get("/{name}", summary="Show a flow")
async def read_flow(name: str, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        flow = await get_flow(session, name)
    if flow is None:
        raise HTTPException(status_code=404, detail="flow not found")
    return _flow_dict(flow)


@router.put("/{name}", summary="Save a flow")
async def put_flow(name: str, body: SaveFlowBody, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    try:
        definition = FlowDefinition.model_validate(
            migrate_definition(body.definition, (info.manifest for info in host.plugins()))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    errors = await validate_definition(host, definition)
    if errors:
        LOG.warning(
            "Flow save rejected for %r",
            name,
            extra={"operation": "flow_save", "flow_name": name, "error": ". ".join(errors)},
        )
        raise HTTPException(status_code=422, detail=errors)
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        try:
            flow = await save_flow(session, name, definition, expected_version=body.expected_version)
        except FlowVersionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    LOG.info("flow saved", extra={"operation": "flow_save", "flow_id": flow.id})
    return _flow_dict(flow)


@router.patch("/{current_name}", summary="Rename a flow")
async def patch_flow(current_name: str, body: RenameFlowBody, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    try:
        definition = FlowDefinition.model_validate(
            migrate_definition(body.definition, (info.manifest for info in host.plugins()))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    errors = await validate_definition(host, definition)
    if errors:
        LOG.warning(
            "Flow rename rejected from %r to %r",
            current_name,
            body.name,
            extra={
                "operation": "flow_rename",
                "flow_name": current_name,
                "new_name": body.name,
                "error": ". ".join(errors),
            },
        )
        raise HTTPException(status_code=422, detail=errors)
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        try:
            flow = await rename_flow(
                session, current_name, body.name, definition, expected_version=body.expected_version
            )
        except FlowNotFoundError:
            raise HTTPException(status_code=404, detail="flow not found") from None
        except FlowNameConflictError:
            raise HTTPException(status_code=409, detail="flow name already exists") from None
        except FlowVersionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    LOG.info("flow renamed", extra={"operation": "flow_rename"})
    return _flow_dict(flow)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a flow")
async def remove_flow(name: str, request: Request, expected_version: int | None = None) -> Response:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        flow = await session.scalar(
            select(Flow).where(Flow.name == name).with_for_update().execution_options(populate_existing=True)
        )
        if flow is None:
            raise HTTPException(status_code=404, detail="flow not found")
        in_use = (
            await session.execute(select(func.count()).select_from(Library).where(Library.flow_id == flow.id))
        ).scalar_one()
        if in_use:
            raise HTTPException(status_code=409, detail=f"flow in use by {in_use} libraries")
        if expected_version is None:
            raise HTTPException(status_code=409, detail="flow version is required before deleting")
        try:
            await delete_flow(session, name, expected_version=expected_version)
        except FlowVersionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await session.commit()
    LOG.info("flow deleted", extra={"operation": "flow_delete"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/dry-run", summary="Dry-run a flow against a file")
async def dry_run_flow(body: DryRunBody | ProbeDryRunBody, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    try:
        definition = FlowDefinition.model_validate(
            migrate_definition(body.flow, (info.manifest for info in host.plugins()))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    errors = await validate_definition(host, definition)
    if errors:
        LOG.warning(
            "Flow dry-run rejected at entry %r",
            definition.entry,
            extra={
                "error": ". ".join(errors),
                "operation": "flow_dry_run",
                "flow_entry": definition.entry,
                **({"file_id": body.file_id} if isinstance(body, DryRunBody) else {"path": body.probe.file_path}),
            },
        )
        raise HTTPException(status_code=422, detail=errors)
    if isinstance(body, ProbeDryRunBody):
        result = await dry_run(
            definition,
            body.probe,
            host,
            body.probe.file_path,
            sidecars=body.sidecar_subtitles,
            metadata=body.sidecar_metadata,
        )
        return {**_dry_run_dict(result), "source": {"kind": "local", "path": body.probe.file_path}}
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        file = await get_media_file(session, body.file_id)
        if file is None:
            raise HTTPException(status_code=404, detail="file not found")
        library = await session.get(Library, file.library_id)
        probe = await probe_for_file(session, file)
    if probe is None:
        raise HTTPException(status_code=422, detail="file has no probe")
    result = await dry_run(definition, probe, host, file.path, library_name=library.name if library else "")
    return _dry_run_dict(result)
