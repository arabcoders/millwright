from __future__ import annotations

import platform
from typing import Annotated

from common.protocol import PROTOCOL_VERSION
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from common import version
from server.api.auth import current_principal
from server.stats import StatsService, overview, savings

router = APIRouter(prefix="/api", tags=["stats"], dependencies=[Depends(current_principal)])


async def snapshot(request: Request) -> dict:
    stats: StatsService = request.app.state.stats
    async with request.app.state.reset_lock:
        data = await stats.read()
    if data is None:
        raise HTTPException(status_code=503, detail="Stats are not available until the scheduled refresh completes.")
    return data


@router.get("/stats", summary="Show aggregate statistics")
async def read_stats(request: Request) -> dict:
    return (await snapshot(request))["summary"]


@router.get("/stats/savings", summary="Show space saved per day")
async def read_savings(
    request: Request,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    library_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    return savings(await snapshot(request), days, library_id)


@router.get("/stats/overview", summary="Show inventory and job statistics")
async def read_stats_overview(
    request: Request,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    library_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    return overview(await snapshot(request), days, library_id)


@router.get("/system", summary="Show server build information")
async def read_system(request: Request) -> dict:
    return {
        "version": version.APP_VERSION,
        "commit_sha": version.APP_COMMIT_SHA,
        "build_date": version.APP_BUILD_DATE,
        "branch": version.APP_BRANCH,
        "protocol_version": PROTOCOL_VERSION,
        "python": platform.python_version(),
        "timezone": request.app.state.timezone,
    }
