from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from typing import TYPE_CHECKING

from common.diagnostics import error_message
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from server.api.auth import PrincipalDep, current_principal
from server.plugins.states import disabled_ids, save_enabled
from server.plugins.upload import (
    MAX_UPLOAD_BYTES,
    UploadError,
    UploadSizeError,
    archive_manifest,
    install_plugin_pending,
)
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from server.plugins.host import PluginHost, PluginInfo
    from server.plugins.manifest import PluginManifest

router = APIRouter(prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(current_principal)])
LOG = logging.getLogger(__name__)


class PluginPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


async def sync_states(host: PluginHost, db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        host.apply_disabled(await disabled_ids(session))


async def _plugin_payload(host: PluginHost, info: PluginInfo) -> dict:
    fields = await host.describe_inputs(info.manifest.id)
    return {
        "manifest": info.manifest.model_dump(mode="json"),
        "state": info.state,
        "source": info.source,
        "inputs": fields or [],
    }


def _find(host: PluginHost, plugin_id: str) -> PluginInfo | None:
    return next((item for item in host.plugins() if item.manifest.id == plugin_id), None)


def _find_or_404(host: PluginHost, plugin_id: str) -> PluginInfo:
    info = _find(host, plugin_id)
    if info is None:
        raise HTTPException(status_code=404, detail="plugin not found")
    return info


@router.get("", summary="List plugins")
async def get_plugins(request: Request) -> list[dict]:
    host: PluginHost = request.app.state.plugin_host
    db: async_sessionmaker[AsyncSession] = request.app.state.db
    await sync_states(host, db)
    return [await _plugin_payload(host, info) for info in host.plugins()]


@router.get("/{plugin_id}", summary="Show a plugin")
async def get_plugin(plugin_id: str, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    db: async_sessionmaker[AsyncSession] = request.app.state.db
    await sync_states(host, db)
    return await _plugin_payload(host, _find_or_404(host, plugin_id))


@router.patch("/{plugin_id}", summary="Enable or disable a plugin")
async def patch_plugin(plugin_id: str, body: PluginPatch, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    db: async_sessionmaker[AsyncSession] = request.app.state.db
    await sync_states(host, db)
    _find_or_404(host, plugin_id)
    async with db() as session:
        await save_enabled(session, plugin_id, body.enabled)
        await session.commit()
    await host.set_enabled(plugin_id, body.enabled)
    LOG.info(
        "plugin state updated", extra={"operation": "plugin_update", "plugin_id": plugin_id, "enabled": body.enabled}
    )
    return await _plugin_payload(host, _find_or_404(host, plugin_id))


@router.post("/{plugin_id}/restart", summary="Restart a plugin runner")
async def restart_plugin(plugin_id: str, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    info = _find_or_404(host, plugin_id)
    if info.state == "disabled":
        raise HTTPException(status_code=409, detail=f"plugin disabled: {plugin_id}")
    await host.restart(plugin_id)
    LOG.info("plugin restarted", extra={"operation": "plugin_restart", "plugin_id": plugin_id})
    return await _plugin_payload(host, _find_or_404(host, plugin_id))


@router.post("/{plugin_id}/stop", summary="Stop a plugin runner")
async def stop_plugin(plugin_id: str, request: Request) -> dict:
    host: PluginHost = request.app.state.plugin_host
    info = _find_or_404(host, plugin_id)
    if info.state == "disabled":
        raise HTTPException(status_code=409, detail=f"plugin disabled: {plugin_id}")
    await host.stop(plugin_id)
    LOG.info("plugin stopped", extra={"operation": "plugin_stop", "plugin_id": plugin_id})
    return await _plugin_payload(host, _find_or_404(host, plugin_id))


@router.post("", summary="Install an uploaded plugin")
async def upload_plugin(request: Request, _principal: PrincipalDep, file: UploadFile) -> dict:
    host: PluginHost = request.app.state.plugin_host
    if host.uploaded_dir is None:
        raise HTTPException(status_code=503, detail="plugin upload directory is not configured")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"plugin archive exceeds {MAX_UPLOAD_BYTES} bytes")
    async with host.upload_lock:
        installing = asyncio.create_task(_install_uploaded(host, data))
        cancelled = False
        while not installing.done():
            try:
                await asyncio.shield(installing)
            except asyncio.CancelledError:
                cancelled = True
        result = installing.result()
        if cancelled:
            raise asyncio.CancelledError
        return result


async def _install_uploaded(host: PluginHost, data: bytes) -> dict:
    if host.uploaded_dir is None:
        raise HTTPException(status_code=503, detail="plugin upload directory is not configured")

    def inspect() -> PluginManifest:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive_manifest(archive)[1]

    try:
        manifest = await wait_workspace_io(inspect)
    except UploadSizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except (UploadError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if _bundled_taken(host, manifest.id):
        raise HTTPException(status_code=422, detail="plugin id is already used by a bundled plugin")

    previous = host._plugins.get(manifest.id)
    disabled = manifest.id in host._disabled
    host._disabled.add(manifest.id)
    if previous is not None:
        try:
            async with previous.lock:
                await host._stop_runner(previous)
        except Exception as exc:
            reason = error_message(exc)
            LOG.error("plugin runner stop failed for %s", manifest.id, extra={"error": reason})
            raise HTTPException(
                status_code=500,
                detail=f"Plugin runner exit is unconfirmed: {reason}. The plugin is disabled and the upload was not installed.",
            ) from None
    try:
        # The enclosing upload task owns this acquired result through all async
        # phases. A helper which discards its result on cancellation would leak it.
        installation = await asyncio.to_thread(
            install_plugin_pending,
            data,
            host.uploaded_dir,
            bundled_taken=lambda pid: _bundled_taken(host, pid),
        )
    except UploadSizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        reason = error_message(exc)
        LOG.error("plugin installation failed for %s", manifest.id, extra={"error": reason})
        raise HTTPException(
            status_code=500,
            detail=f"Plugin installation failed: {reason}. The plugin is disabled and retained files require inspection.",
        ) from None

    loaded_state = None
    failure_detail = "plugin failed to load after upload"

    async def load_payload() -> dict:
        nonlocal loaded_state, failure_detail
        loaded = await host.hot_load(installation.path)
        loaded_state = host._plugins.get(manifest.id)
        if loaded != manifest or loaded_state is None or loaded_state.dir != installation.path:
            raise HTTPException(status_code=500, detail=failure_detail)
        if not disabled:
            host._disabled.discard(manifest.id)
        info = _find_or_404(host, manifest.id)
        failure_detail = "plugin response preparation failed after upload"
        return await _plugin_payload(host, info)

    try:
        try:
            payload = await load_payload()
        except (asyncio.CancelledError, Exception) as failure:
            host._disabled.add(manifest.id)
            current = host._plugins.get(manifest.id)
            states = [current] if current is not None else []
            if loaded_state is not None and loaded_state is not current:
                states.append(loaded_state)
            try:
                for state in states:
                    if state.dir == installation.path:
                        # Wait for the current plugin call before touching its files.
                        async with state.lock:
                            await host._stop_runner(state)
            except Exception as exc:
                reason = error_message(exc)
                LOG.error("plugin runner cleanup failed for %s", manifest.id, extra={"error": reason})
                raise HTTPException(
                    status_code=500,
                    detail=f"{failure_detail.capitalize()}: {error_message(failure)}. Runner exit is unconfirmed: {reason}. The plugin is disabled and installation copies are retained.",
                ) from None
            try:
                await wait_workspace_io(installation.rollback)
                if installation.previous_fd is not None and previous is not None:
                    host._register(installation.path)
                    restored = host._plugins[manifest.id]
                    restored.stopped = previous.stopped
                elif current is not None and host._plugins.get(manifest.id) is current:
                    host._plugins.pop(manifest.id)
                host._invalid.pop(installation.path.name, None)
                if not disabled:
                    host._disabled.discard(manifest.id)
            except Exception as exc:
                reason = error_message(exc)
                LOG.error("plugin rollback failed for %s", manifest.id, extra={"error": reason})
                raise HTTPException(
                    status_code=500,
                    detail=f"{failure_detail.capitalize()}: {error_message(failure)}. Rollback is incomplete: {reason}. The plugin is disabled and retained files require inspection.",
                ) from None
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise HTTPException(
                status_code=500,
                detail=f"{failure_detail.capitalize()}: {error_message(failure)}. The installation was rolled back.",
            ) from None
        try:
            await wait_workspace_io(installation.commit)
        except Exception as exc:
            reason = error_message(exc)
            LOG.error("plugin previous installation cleanup failed for %s", manifest.id, extra={"error": reason})
            # Cleanup may already have removed part of the previous generation.
            # The published generation stays installed. It cannot safely roll back.
            raise HTTPException(
                status_code=500,
                detail=f"Plugin installed, but previous installation cleanup failed: {reason}. Retained files require inspection.",
            ) from None
        if host.on_change is not None:
            await host.on_change()
        return payload
    finally:
        installation.close()


def _bundled_taken(host: PluginHost, plugin_id: str) -> bool:
    info = _find(host, plugin_id)
    return info is not None and info.source == "bundled"
