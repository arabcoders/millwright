from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from common.diagnostics import error_message
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.responses import Response

from server.api.auth import current_principal
from server.backup import (
    MAX_SIZE,
    RESET_BUSY_STATUSES,
    _items,
    check_config_destination,
    decrypt,
    encrypt,
    export_config,
    export_data,
    export_plugins,
    plugin_archive,
    prepared_config,
    reset_data,
    restore_config,
    restore_data,
    restore_plugin,
)
from server.flows.batch import BatchError
from server.models import Job
from server.plugins.migrations import find_manifest, migrate_definition
from server.plugins.states import disabled_ids
from server.plugins.store_migrations import migrate_saved_plugins
from server.settings import OperationalSettings
from server.workspace import recovery_jobs, wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import async_sessionmaker


class PasswordBody(BaseModel):
    password: str = Field(min_length=8)


class ResetBody(BaseModel):
    keep: list[str] = Field(default_factory=list)


@dataclass(kw_only=True)
class _RestoreOutcome:
    committed: bool = False


@asynccontextmanager
async def _pause_dry_runs(request: Request) -> AsyncGenerator[None]:
    try:
        async with request.app.state.dry_runs.pause():
            yield
    except BatchError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


router = APIRouter(prefix="/api/backups", tags=["backups"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger("server.api.backups")


def _database_error(exc: SQLAlchemyError) -> str:
    original = getattr(exc, "orig", None)
    return error_message(original if isinstance(original, BaseException) else exc)


def _decode(data: bytes, password: str) -> dict[str, Any]:
    try:
        payload = json.loads(decrypt(data, password))
        return _validated(payload)
    except ValueError, UnicodeError, KeyError, TypeError:
        raise HTTPException(status_code=422, detail="invalid backup or password") from None


def _validated(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("_type") != "millwright-backup" or payload.get("_version") != 1:
        raise ValueError("unsupported backup format")
    for key in ("flows", "libraries", "notifications", "plugin_states", "webhooks"):
        if not isinstance(payload.get(key), list):
            raise ValueError("invalid backup contents")
    if not isinstance(payload.get("nodes", []), list):
        raise ValueError("invalid backup contents")
    if not isinstance(payload.get("uploaded_plugins"), dict):
        raise ValueError("invalid backup contents")
    if payload.get("config") is not None and not isinstance(payload["config"], str):
        raise ValueError("invalid backup contents")
    OperationalSettings.model_validate(payload["settings"])
    _preview(payload)
    return payload


async def _upload(file: UploadFile) -> bytes:
    data = await file.read(MAX_SIZE + 1)
    if len(data) >= MAX_SIZE:
        raise HTTPException(status_code=413, detail="backup is too large")
    return data


def _preview(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("flows", "libraries", "notifications", "plugin_states", "nodes", "webhooks"):
        _items(data, key)
    preview: dict[str, Any] = {
        "flows": [item["name"] for item in data["flows"]],
        "libraries": [
            {"name": item["name"], "flow_name": item.get("flow_name"), "roots": item["roots"]}
            for item in data["libraries"]
        ],
        "notifications": [item["name"] for item in data["notifications"]],
        "webhooks": [item["name"] for item in data["webhooks"]],
        "plugins": sorted({item["id"] for item in data["plugin_states"]} | set(data["uploaded_plugins"])),
        "nodes": [{"label": item["display_name"], "value": item["name"]} for item in data.get("nodes", [])],
        "config": data["config"] is not None,
        "settings": True,
    }
    if any(
        not isinstance(name, str) or not name
        for name in preview["flows"] + preview["notifications"] + preview["plugins"] + preview["webhooks"]
    ):
        raise ValueError("invalid backup contents")
    if any(
        not isinstance(item["label"], str)
        or not item["label"].strip()
        or not isinstance(item["value"], str)
        or not item["value"]
        for item in preview["nodes"]
    ):
        raise ValueError("invalid node in backup")
    if any(
        not isinstance(item["name"], str)
        or not isinstance(item["roots"], list)
        or not all(isinstance(root, str) for root in item["roots"])
        or (item["flow_name"] is not None and not isinstance(item["flow_name"], str))
        for item in preview["libraries"]
    ):
        raise ValueError("invalid backup contents")
    for key in ("flows", "libraries", "notifications", "plugins", "nodes", "webhooks"):
        names = [
            item["value" if key == "nodes" else "name"] if isinstance(item, dict) else item for item in preview[key]
        ]
        if len(set(names)) != len(names):
            raise ValueError("duplicate backup item")
    return preview


@router.post("/export", summary="Export encrypted configuration backup")
async def export_backup(body: PasswordBody, request: Request) -> Response:
    db: async_sessionmaker = request.app.state.db
    host = request.app.state.plugin_host
    async with request.app.state.reset_lock, host.upload_lock:
        async with db() as session:
            payload = await export_data(session)
        root = host.uploaded_dir
        archives = await wait_workspace_io(lambda: export_plugins(root)) if root is not None else {}
        config = await wait_workspace_io(lambda: export_config(request.app.state.config_path))
    known = {item["id"] for item in payload["plugin_states"]}
    payload["plugin_states"].extend({"id": name, "enabled": False} for name in archives if name not in known)
    payload.update(
        {
            "_type": "millwright-backup",
            "_version": 1,
            "config": config,
            "uploaded_plugins": archives,
        }
    )
    try:
        content = encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"), body.password)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    LOG.info(
        "Exported configuration backup with %s uploaded plugins",
        len(archives),
        extra={"operation": "backup_export", "plugin_count": len(archives)},
    )
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="millwright.mwbackup"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/preview", summary="Preview encrypted backup")
async def preview_backup(file: Annotated[UploadFile, File()], password: Annotated[str, Form()]) -> dict[str, Any]:
    LOG.debug("backup preview requested", extra={"operation": "backup_preview"})
    return _preview(_decode(await _upload(file), password))


def _selection(raw: str, data: dict[str, Any]) -> dict[str, Any]:
    selected = json.loads(raw)
    if not isinstance(selected, dict) or not isinstance(selected.get("config"), bool):
        raise ValueError("invalid selection")
    if not isinstance(selected.get("settings", False), bool):
        raise ValueError("invalid settings selection")
    available = _preview(data)
    for key in ("flows", "libraries", "notifications", "plugins", "nodes", "webhooks"):
        names = selected.get(key, [])
        choices = {
            item["value" if key == "nodes" else "name"] if isinstance(item, dict) else item for item in available[key]
        }
        if not isinstance(names, list) or any(not isinstance(name, str) or name not in choices for name in names):
            msg = f"invalid {key} selection"
            raise ValueError(msg)
    if selected["config"] and data["config"] is None:
        raise ValueError("backup has no config")
    return selected


async def _apply(request: Request, data: dict[str, Any], selection: str) -> dict[str, Any]:
    selected = _selection(selection, data)
    replace = selected.get("replace", False)
    if not isinstance(replace, bool):
        raise ValueError("replace must be a boolean")
    host = request.app.state.plugin_host
    root = host.uploaded_dir
    archives = data["uploaded_plugins"]
    data = deepcopy(data)
    selected = deepcopy(selected)
    manifests = [info.manifest for info in host.plugins()]
    for item in data["flows"]:
        if item["name"] in selected.get("flows", []):
            item["definition"] = migrate_definition(item["definition"], manifests)
    for item in data["plugin_states"]:
        old_id = item["id"]
        if old_id in selected.get("plugins", []) and old_id not in archives:
            manifest = find_manifest(old_id, manifests)
            if manifest is not None:
                item["id"] = manifest.id
                selected["plugins"] = [manifest.id if value == old_id else value for value in selected["plugins"]]
    bundled = {item.manifest.id for item in host.plugins() if item.source == "bundled"}
    plugin_data = await wait_workspace_io(
        lambda: {
            name: plugin_archive(name, archives[name], bundled_taken=bundled.__contains__)
            for name in selected.get("plugins", [])
            if name in archives
        }
    )
    if root is None and plugin_data:
        raise ValueError("uploaded plugin directory is unavailable")
    if root is not None and plugin_data:

        def verify_plugins() -> None:
            with tempfile.TemporaryDirectory(prefix=".backup-verify-", dir=root.parent) as stage:
                for name, archive in plugin_data.items():
                    restore_plugin(Path(stage), name, archive, replace=False)

        await wait_workspace_io(verify_plugins)
    if selected["config"]:
        await wait_workspace_io(lambda: prepared_config(request.app.state.config_path, data["config"]))
    outcome = _RestoreOutcome()

    async def apply() -> dict[str, Any]:
        failure: BaseException | None = None
        service_error: Exception | None = None
        try:
            async with request.app.state.scanner.library_mutation():
                try:
                    await request.app.state.stats.invalidate()
                    return await _apply_locked(request, data, selected, plugin_data, outcome)
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    try:
                        await request.app.state.probes.check_library_access()
                    except Exception as exc:
                        service_error = exc
                        if failure is None:
                            raise
                        LOG.error("backup service update failed", extra={"error": error_message(exc)})
        except BaseException as exc:
            first = failure or service_error or exc
            if outcome.committed and isinstance(first, Exception) and not isinstance(first, HTTPException):
                reason = error_message(first)
                LOG.error("backup post-restore service update failed", extra={"error": reason})
                raise HTTPException(
                    status_code=500,
                    detail=f"Database items were restored, but post-restore service update failed: {reason}",
                ) from None
            raise first from None

    async with request.app.state.reset_lock, request.app.state.stats.lock, host.upload_lock, _pause_dry_runs(request):
        applying = asyncio.create_task(apply())
        cancelled = False
        while not applying.done():
            try:
                await asyncio.shield(applying)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            applying.exception()
            raise asyncio.CancelledError
        return applying.result()


async def _apply_locked(
    request: Request,
    data: dict[str, Any],
    selected: dict[str, Any],
    plugin_data: dict[str, bytes],
    outcome: _RestoreOutcome,
) -> dict[str, Any]:
    replace = selected.get("replace", False)
    if selected["config"]:
        path = request.app.state.config_path
        check_config_destination(path)
        if path.exists() and not replace:
            raise ValueError("config already exists")
        await wait_workspace_io(lambda: prepared_config(path, data["config"]))
    host = request.app.state.plugin_host
    root = host.uploaded_dir
    archives = data["uploaded_plugins"]
    if any(name not in archives and host.describe(name) is None for name in selected.get("plugins", [])):
        raise ValueError("selected plugin is not installed on this server")
    if root is None and plugin_data:
        raise ValueError("uploaded plugin directory is unavailable")
    if root is not None and not replace and any((root / name).exists() for name in plugin_data):
        raise ValueError("uploaded plugin already exists")
    db: async_sessionmaker = request.app.state.db
    async with db() as session:
        await restore_data(session, data, selected)
        await session.commit()
        outcome.committed = True
        host.apply_disabled(await disabled_ids(session))
    if selected.get("settings", False):
        await request.app.state.settings.load()
    try:
        if root is not None:
            for name, archive in plugin_data.items():
                await wait_workspace_io(partial(restore_plugin, root, name, archive, replace=replace))
        if selected["config"]:
            await wait_workspace_io(lambda: restore_config(request.app.state.config_path, data["config"]))
    except (OSError, ValueError) as exc:
        LOG.error("backup file restore failed after database commit", extra={"error": error_message(exc)})
        raise HTTPException(
            status_code=500,
            detail=f"Database items were restored, but a file update failed: {error_message(exc)}",
        ) from None
    LOG.info(
        "Restored selected configuration and %s uploaded plugins",
        len(plugin_data),
        extra={"operation": "backup_restore", "plugin_count": len(plugin_data)},
    )
    if not plugin_data:
        await migrate_saved_plugins(host, db)
    return {"restored": True, "restart_required": bool(selected["config"] or plugin_data)}


@router.post("/restore", summary="Restore selected backup items")
async def restore_backup(
    request: Request,
    file: Annotated[UploadFile, File()],
    password: Annotated[str, Form()],
    selection: Annotated[str, Form()],
) -> dict[str, Any]:
    data = _decode(await _upload(file), password)
    try:
        return await _apply(request, data, selection)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            reason = _database_error(exc)
            LOG.info("backup restore conflict", extra={"error": reason})
            raise HTTPException(
                status_code=409, detail=f"selected item conflicts with existing configuration: {reason}"
            ) from None
        reason = _database_error(exc)
        LOG.error("backup database restore failed", extra={"error": reason})
        raise HTTPException(status_code=500, detail=f"backup database restore failed: {reason}") from None
    except SQLAlchemyError as exc:
        reason = _database_error(exc)
        LOG.error("backup database restore failed", extra={"error": reason})
        raise HTTPException(status_code=500, detail=f"backup database restore failed: {reason}") from None
    except (ValueError, KeyError, TypeError) as exc:
        if "work is active" in str(exc) or "already exists" in str(exc):
            LOG.warning("Backup restore refused", extra={"error": str(exc)})
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(
            status_code=422, detail=f"invalid backup selection or contents: {error_message(exc)}"
        ) from None


@router.post(
    "/reset",
    summary="Reset configuration and delete file records and job history without deleting library media",
)
async def reset_backup(body: ResetBody, request: Request) -> dict[str, bool]:
    allowed = {"flows", "libraries", "nodes", "notifications", "plugins", "webhooks", "settings"}
    keep = set(body.keep)
    if len(keep) != len(body.keep) or not keep <= allowed:
        raise HTTPException(status_code=422, detail="invalid reset category")
    if request.app.state.reset_lock.locked():
        raise HTTPException(status_code=409, detail="reset is already active")
    async with request.app.state.reset_lock, request.app.state.plugin_host.upload_lock, _pause_dry_runs(request):
        resetting = asyncio.create_task(_reset_locked(request, keep))
        cancelled = False
        while not resetting.done():
            try:
                await asyncio.shield(resetting)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            resetting.exception()
            raise asyncio.CancelledError
        resetting.result()
    LOG.info(
        "Reset server data and kept %s configuration categories",
        len(keep),
        extra={"operation": "backup_reset", "kept_count": len(keep)},
    )
    return {"reset": True}


async def _reset_locked(request: Request, keep: set[str]) -> None:
    state = request.app.state
    db: async_sessionmaker = state.db
    await _check_reset_jobs(db)
    closers = (
        state.scheduler.aclose,
        state.jobs.close,
        state.scanner.aclose,
        state.probes.close,
        state.flow_enqueue.close,
        state.healthchecks.close,
        state.staging.aclose,
        state.notifications.aclose,
    )
    committed = False
    failure: BaseException | None = None
    try:
        # Deletion waits for node work through the dispatch lock. Close it
        # before reset takes that lock, or the two operations wait on each other.
        if state.services_running:
            await state.library_deletion.close()
        async with state.hub.pause_for_reset():
            running = state.services_running
            try:
                if running:
                    for close in closers:
                        await close()
                async with state.stats.lock, state.workspaces.lock:
                    await _check_reset_jobs(db)
                    await state.stats.invalidate()
                    if "nodes" not in keep:
                        await state.hub.disconnect_for_reset()
                    async with db() as session:
                        await reset_data(session, keep)
                        await session.commit()
                        committed = True
                    await state.settings.load()
                try:
                    if "plugins" not in keep:
                        await state.plugin_host.reset_uploaded()
                except Exception as exc:
                    reason = error_message(exc)
                    LOG.error("reset uploaded plugin cleanup failed", extra={"error": reason})
                    raise HTTPException(
                        status_code=500, detail=f"Reset committed, but uploaded plugin cleanup failed: {reason}"
                    ) from None
                async with db() as session:
                    state.plugin_host.apply_disabled(await disabled_ids(session))
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if running and state.services_running:
                    try:
                        state.staging.start()
                        state.healthchecks.start()
                        state.scanner.start()
                        await state.probes.start()
                        await state.jobs.start()
                        if state.services_running:
                            await state.flow_enqueue.start()
                        if state.services_running:
                            state.scheduler.start()
                            state.notifications.start()
                            await state.library_deletion.start()
                    except (Exception, asyncio.CancelledError) as exc:
                        # A failed restart must not let partly restarted workers accept work.
                        LOG.error("reset service restart failed", extra={"error": error_message(exc)})
                        state.services_running = False
                        state.hub.begin_shutdown()
                        for close in (state.library_deletion.close, *closers):
                            try:
                                await close()
                            except (Exception, asyncio.CancelledError) as cleanup_error:
                                LOG.error("reset restart cleanup failed", extra={"error": error_message(cleanup_error)})
                        raise failure or exc from None
    except Exception as exc:
        if committed:
            if isinstance(exc, HTTPException):
                raise
            reason = error_message(exc)
            LOG.error("reset post-commit update failed", extra={"error": reason})
            raise HTTPException(
                status_code=500, detail=f"reset committed but service service update failed: {reason}"
            ) from None
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=409, detail=str(exc)) from None
        raise


async def _check_reset_jobs(db: async_sessionmaker) -> None:
    async with db() as session:
        if (
            await session.scalar(
                select(Job.id).where(or_(Job.status.in_(RESET_BUSY_STATUSES), Job.id.in_(recovery_jobs()))).limit(1)
            )
            is not None
        ):
            raise HTTPException(status_code=409, detail="cannot reset while work is active or recovery is pending")
