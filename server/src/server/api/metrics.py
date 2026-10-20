from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from server.api.auth import current_principal

router = APIRouter(tags=["stats"], dependencies=[Depends(current_principal)])


@router.get("/metrics", summary="Prometheus metrics")
async def get_metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
