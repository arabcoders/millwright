from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from server.api.auth import current_principal
from server.settings import OperationalSettings, SettingsService

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(current_principal)])


@router.get("", summary="Read settings")
async def get_settings(request: Request) -> OperationalSettings:
    service: SettingsService = request.app.state.settings
    return service.current


@router.patch("", summary="Update settings")
async def patch_settings(body: dict[str, Any], request: Request) -> OperationalSettings:
    service: SettingsService = request.app.state.settings
    async with request.app.state.reset_lock:
        try:
            return await service.update(body)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
