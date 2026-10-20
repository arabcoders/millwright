from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from common.diagnostics import error_message
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from server.api.auth import current_principal
from server.models import NotificationTarget, utcnow
from server.notifications import _SEND_TIMEOUT_S, _apprise_test, _url_secrets, notification_client
from server.serialize import iso_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(current_principal)])
_NAME = re.compile(r"^[\w][\w .-]{0,99}$")
LOG = logging.getLogger(__name__)


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)


def _target_dict(target: NotificationTarget) -> dict[str, str | None]:
    return {
        "name": target.name,
        "url": target.url,
        "created_at": iso_utc(target.created_at),
        "updated_at": iso_utc(target.updated_at),
    }


def _validate_url(url: str) -> None:
    if not url.strip():
        raise HTTPException(status_code=422, detail="invalid notification URL")
    if notification_client(url) is None:
        raise HTTPException(status_code=422, detail="invalid notification URL")


@router.get("", summary="List notification targets")
async def get_targets(request: Request) -> list[dict[str, str | None]]:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        targets = (await session.scalars(select(NotificationTarget).order_by(NotificationTarget.name))).all()
        return [_target_dict(target) for target in targets]


@router.put("/{name}", summary="Create or update a notification target")
async def put_target(name: str, body: TargetBody, request: Request) -> dict[str, str | None]:
    if not _NAME.fullmatch(name):
        raise HTTPException(status_code=422, detail="invalid notification target name")
    _validate_url(body.url)
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        statement = insert(NotificationTarget).values(name=name, url=body.url)
        target = (
            await session.scalars(
                statement.on_conflict_do_update(
                    index_elements=[NotificationTarget.name],
                    set_={"url": statement.excluded.url, "updated_at": utcnow()},
                ).returning(NotificationTarget)
            )
        ).one()
        await session.commit()
        LOG.info("Saved notification target %s", name, extra={"operation": "notification_save", "target_id": name})
        return _target_dict(target)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a notification target")
async def delete_target(name: str, request: Request) -> Response:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        identifier = await session.scalar(
            delete(NotificationTarget).where(NotificationTarget.name == name).returning(NotificationTarget.id)
        )
        if identifier is None:
            raise HTTPException(status_code=404, detail="notification target not found")
        await session.commit()
    LOG.info("Deleted notification target %s", name, extra={"operation": "notification_delete", "target_id": name})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/test", summary="Send a test notification")
async def test_target(name: str, request: Request) -> Response:
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        target = await session.scalar(select(NotificationTarget).where(NotificationTarget.name == name))
        if target is None:
            raise HTTPException(status_code=404, detail="notification target not found")
        url = target.url

    try:
        sent, detail = await asyncio.wait_for(
            _apprise_test(
                url, "This is a test notification from Millwright.", "Millwright notification test", _SEND_TIMEOUT_S
            ),
            timeout=_SEND_TIMEOUT_S,
        )
    except TimeoutError:
        detail = f"notification test timed out after {_SEND_TIMEOUT_S:g} seconds"
        LOG.warning("notification test target=%s: %s", name, detail)
        raise HTTPException(status_code=504, detail=detail) from None
    except Exception as exc:
        detail = error_message(exc, secrets=_url_secrets(url))
        LOG.warning(
            "Notification test failed for target %s",
            name,
            extra={
                "operation": "notification_test",
                "target_id": name,
                "error_class": type(exc).__name__,
                "error": detail,
            },
        )
        raise HTTPException(status_code=502, detail=detail) from None
    if not sent:
        safe_detail = detail or "notification provider rejected the notification"
        LOG.warning("Notification provider rejected test for target %s", name, extra={"error": safe_detail})
        raise HTTPException(status_code=502, detail=safe_detail)
    LOG.info("notification test sent", extra={"operation": "notification_test", "target_id": name})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
