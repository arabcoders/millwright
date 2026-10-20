from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from server.api.auth import current_principal

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)


class PruneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_ids: list[Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=list, max_length=999)
    all_unused: bool = False

    @model_validator(mode="after")
    def selection(self) -> PruneBody:
        if bool(self.job_ids) == self.all_unused:
            raise ValueError("select job_ids or all_unused, not both")
        return self


@router.get("", summary="Inspect job workspaces")
async def list_workspaces(
    request: Request,
    query: str | None = None,
    eligibility: Literal["all", "unused", "protected"] = "all",
    sort_by: Literal[
        "workspace_id", "filename", "status", "node_name", "size_bytes", "last_activity_at"
    ] = "workspace_id",
    sort_dir: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=999)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    service = request.app.state.workspaces
    return await service.listing(
        query=query,
        eligibility=eligibility,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )


@router.post("/prune", summary="Remove unused workspace files without deleting history")
async def prune(body: PruneBody, request: Request) -> dict:
    service = request.app.state.workspaces
    if body.all_unused:
        snapshot = await service.sample()
        if snapshot["error"]:
            raise HTTPException(status_code=503, detail=snapshot["error"])
        ids = [item["workspace_id"] for item in snapshot["items"] if item["eligible"]]
    else:
        ids = list(dict.fromkeys(body.job_ids))
    items = []
    for job_id in ids:
        try:
            items.append(await service.staging.prune_job(job_id))
        except SQLAlchemyError:
            LOG.exception("workspace job check failed for %s", job_id)
            items.append({"workspace_id": job_id, "outcome": "error", "reason": "Could not check the workspace job."})
    snapshot = await service.sample()
    return {
        "items": items,
        "removed": sum(item["outcome"] == "removed" for item in items),
        "skipped": sum(item["outcome"] == "skipped" for item in items),
        "errors": sum(item["outcome"] == "error" for item in items),
        "snapshot": {key: snapshot[key] for key in ("summary", "sampled_at", "error")},
    }


@router.get("/{job_id}", summary="Inspect workspace file metadata")
async def workspace_detail(job_id: Annotated[int, Path(ge=0)], request: Request) -> dict:
    try:
        return await request.app.state.workspaces.detail(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found or root unavailable") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="workspace is unavailable or unsafe") from exc
