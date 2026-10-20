from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from common.enums import WebhookType
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from server.api.auth import current_principal
from server.models import Library, WebhookRegistration, utcnow
from server.serialize import iso_utc
from server.webhooks import WebhookBody, convert_path, event_paths

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

LOG = logging.getLogger(__name__)
_NAME = re.compile(r"^[\w][\w .-]{0,99}$")
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"], dependencies=[Depends(current_principal)])
receiver = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def registration_dict(row: WebhookRegistration) -> dict:
    return {
        "name": row.name,
        "type": row.type,
        "api_key": row.api_key,
        "path_replacements": row.path_replacements,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


@router.get("", summary="List inbound webhook registrations")
async def get_registrations(request: Request) -> list[dict]:
    async with request.app.state.db() as session:
        rows = await session.scalars(select(WebhookRegistration).order_by(WebhookRegistration.name))
        return [registration_dict(row) for row in rows]


@router.put("/{name}", summary="Create or update an inbound webhook registration")
async def put_registration(name: str, body: WebhookBody, request: Request) -> dict:
    if not _NAME.fullmatch(name):
        raise HTTPException(422, "Invalid webhook registration name")
    async with request.app.state.db() as session:
        statement = insert(WebhookRegistration).values(
            name=name, api_key=f"mwh_{secrets.token_urlsafe(12)}", **body.model_dump(mode="json")
        )
        row = (
            await session.scalars(
                statement.on_conflict_do_update(
                    index_elements=[WebhookRegistration.name],
                    set_={**body.model_dump(mode="json"), "updated_at": utcnow()},
                ).returning(WebhookRegistration)
            )
        ).one()
        await session.commit()
        return registration_dict(row)


@router.delete("/{name}", status_code=204, summary="Delete and revoke an inbound webhook registration")
async def delete_registration(name: str, request: Request) -> Response:
    async with request.app.state.db() as session:
        identifier = await session.scalar(
            delete(WebhookRegistration).where(WebhookRegistration.name == name).returning(WebhookRegistration.id)
        )
        if identifier is None:
            raise HTTPException(404, "Webhook registration not found")
        await session.commit()
    return Response(status_code=204)


@receiver.post("/receive", summary="Receive a Connect webhook using its registration key")
async def receive_webhook(
    request: Request,
    response: Response,
    basic: Annotated[HTTPBasicCredentials | None, Depends(HTTPBasic(auto_error=False))],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
    api_key: str = "",
) -> dict:
    # This router deliberately has no current_principal dependency. Neither a login
    # session nor the instance API key authorizes receipt, even with auth disabled.
    if basic is not None:
        api_key = basic.password
    elif bearer is not None:
        api_key = bearer.credentials
    elif "authorization" in request.headers:
        raise HTTPException(401, "Invalid webhook authorization")
    db: async_sessionmaker = request.app.state.db
    async with request.app.state.reset_lock, db() as session:
        registration = await session.scalar(
            select(WebhookRegistration).where(WebhookRegistration.api_key == api_key).with_for_update(read=True)
        )
        if registration is None:
            raise HTTPException(401, "Invalid webhook API key")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(422, "Expected a JSON object") from None
        if not isinstance(payload, dict):
            raise HTTPException(422, "Expected a JSON object")
        try:
            paths = event_paths(WebhookType(registration.type), payload)
            if paths is None:
                return {"status": "test" if payload.get("eventType") == "Test" else "ignored"}
            settings = WebhookBody(
                type=WebhookType(registration.type), path_replacements=registration.path_replacements
            )
            targets = {convert_path(value, settings.path_replacements) for value in paths}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        libraries = list((await session.scalars(select(Library).where(Library.deleted.is_(False)))).all())
        scopes: dict[int, set[Path]] = {}
        for target in targets:
            matches = [
                library for library in libraries if any(target.is_relative_to(Path(root)) for root in library.roots)
            ]
            if not matches:
                raise HTTPException(422, f"Webhook path is outside configured library roots: {target}")
            for library in matches:
                if library.suspended:
                    raise HTTPException(
                        409, f"Library {library.name} is suspended. Review and resume it before scanning."
                    )
                scopes.setdefault(library.id, set()).add(target)
        scanner = request.app.state.scanner
        if scanner.paused:
            raise HTTPException(409, "Activity is paused. Resume activity before scanning.")
        if not scanner.running:
            raise HTTPException(503, "Scanner is stopped. Try again after server services resume.")
        results = [
            {"library_id": identifier, "status": scanner.queue_scan(identifier, "webhook", target_paths=scope)}
            for identifier, scope in sorted(scopes.items())
        ]
        LOG.info("Webhook %s queued discovery for paths %s", registration.name, sorted(str(path) for path in targets))
    response.status_code = 202
    return {"status": "accepted", "libraries": results}
