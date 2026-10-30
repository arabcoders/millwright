from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import current_principal
from server.models import NodeRow
from server.nodehub.store import list_nodes
from server.queue.store import NodeRemovalError, remove_node
from server.serialize import iso_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)


class NodePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1)
    max_transcodes: int | None = Field(default=None, ge=1)
    max_healthchecks: int | None = Field(default=None, ge=0)
    max_mkv_stats: int | None = Field(default=None, ge=0)
    paused: bool | None = None


def _node_dict(node: NodeRow, in_flight: dict[int, str] | None = None) -> dict:
    # token_hash is never exposed: it cannot verify anything the client would need.
    return {
        "id": node.id,
        "created_at": iso_utc(node.created_at),
        "updated_at": iso_utc(node.updated_at),
        "name": node.name,
        "display_name": node.display_name,
        "state": node.state,
        "paused": node.paused,
        "last_seen_at": iso_utc(node.last_seen_at),
        "capabilities": node.capabilities,
        "max_transcodes": node.transcode_limit,
        "max_healthchecks": node.healthcheck_limit,
        "max_mkv_stats": node.mkv_stats_limit,
        "usage": node.usage,
        "workspace_ready": node.workspace_ready,
        "workspace_free_bytes": node.workspace_free_bytes,
        "active_jobs": len(in_flight or {}),
        "active_transcodes": sum(kind == "transcode" for kind in (in_flight or {}).values()),
        "active_healthchecks": sum(kind == "healthcheck" for kind in (in_flight or {}).values()),
        "active_mkv_stats": sum(kind == "mkv_stats" for kind in (in_flight or {}).values()),
        "active_job_ids": sorted(in_flight or {}),
    }


@router.get("", summary="List nodes")
async def get_nodes(request: Request) -> list[dict]:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        return [
            _node_dict(node, request.app.state.hub.entries[node.name].in_flight)
            if node.name in request.app.state.hub.entries
            else _node_dict(node)
            for node in await list_nodes(session)
        ]


@router.get("/registration", summary="Read live node registration requests")
async def get_registration(request: Request) -> dict:
    return request.app.state.hub.registration_status()


@router.post("/pending/{request_id}/claim", status_code=status.HTTP_202_ACCEPTED, summary="Claim a pending node")
async def claim_node(request_id: str, request: Request) -> None:
    async with request.app.state.reset_lock:
        if not request.app.state.hub.claim_node(request_id):
            raise HTTPException(status_code=404, detail="Pending node is no longer connected.")
    LOG.info("Node registration request %s claimed.", request_id)


@router.patch("/{node_id}", summary="Update a node's workload limits, name, or paused state")
async def patch_node(node_id: int, body: NodePatchBody, request: Request) -> dict:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        node = await session.get(NodeRow, node_id, with_for_update=True, populate_existing=True)
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")
        if body.max_transcodes is not None:
            node.transcode_limit = body.max_transcodes
        if body.max_healthchecks is not None:
            node.healthcheck_limit = body.max_healthchecks
        if body.max_mkv_stats is not None:
            node.mkv_stats_limit = body.max_mkv_stats
        if body.display_name is not None:
            node.display_name = body.display_name.strip()
            if not node.display_name:
                raise HTTPException(status_code=422, detail="display_name must not be blank")
        if body.paused is not None:
            node.paused = body.paused
        await session.commit()
        if any(value is not None for value in (body.max_transcodes, body.max_healthchecks, body.max_mkv_stats)):
            await request.app.state.hub.set_limits(
                node_id,
                max_transcodes=node.transcode_limit,
                max_healthchecks=node.healthcheck_limit,
                max_mkv_stats=node.mkv_stats_limit,
            )
        if body.paused is not None:
            request.app.state.hub.set_paused(node_id, body.paused)
        LOG.info("node updated", extra={"operation": "node_update", "node_id": node_id})
        entry = request.app.state.hub.entries.get(node.name)
        return _node_dict(node, entry.in_flight if entry is not None else {})


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove an offline node")
async def delete_node(node_id: int, request: Request) -> None:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        try:
            removed = await remove_node(session, node_id)
            if not removed:
                raise HTTPException(status_code=404, detail="node not found")
            await session.commit()
        except NodeRemovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
