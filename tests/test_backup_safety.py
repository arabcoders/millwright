from __future__ import annotations

import asyncio
import base64
import gc
import io
import json
import os
import threading
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
import zipfile

import httpx
import pytest
from sqlalchemy import select

from fastapi import Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from server import backup
from server.api import backups as backups_api
from server.api import plugins as plugins_api
from server.backup import (
    decrypt,
    encrypt,
    export_data,
    plugin_archive,
    reset_data,
    restore_config,
    restore_data,
    restore_plugin,
)
from server.core.auth import Principal
from server.core.config import load_config
from server.flows.definition import FlowDefinition
from server.flows.store import save_flow
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NotificationTarget,
    PluginState,
    TrashEntry,
)
from server.queue.store import create_library
from server.plugins import upload as plugin_upload
from server.plugins.host import PluginHost
from server.plugins.manifest import load_manifest
from server.plugins.upload import install_plugin
from tests.helpers import database_context, fd_path, graph_flow
from tests.test_backups import backup_env
from tests.test_plugin_upload_safety import ObservedUploadLock, contents, expected, package


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["libraries", "flows"])
@pytest.mark.parametrize("record_type", ["actions_pending", "prepared", "discard_prepared"])
async def test_restore_checks_recovery(backup_env, selected: str, record_type: str) -> None:
    client, config_path, db, _ = backup_env
    source = config_path.parent / "movie.mkv"
    source.write_bytes(b"original")
    async with db() as session:
        flow = await save_flow(session, "Original", FlowDefinition.model_validate(graph_flow("transcode.video")))
        library = await create_library(session, name="Original", roots=[str(source.parent)], flow_id=flow.id)
        media = MediaFile(
            library_id=library.id,
            path=str(source),
            size=8,
            mtime_ns=source.stat().st_mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(media)
        await session.flush()
        job = Job(library_id=library.id, media_file_id=media.id, status="failed", attempts=1)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed", result="failed")
        session.add(attempt)
        await session.flush()
        if record_type != "actions_pending":
            session.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=str(source),
                    destination_path=str(source),
                    partial_path=str(source) + ".partial",
                    trash_path=str(source) + ".discard",
                    state=record_type,
                )
            )
        else:
            session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type=record_type))
        await session.commit()
        library_id, roots, flow_id, version = library.id, library.roots, flow.id, flow.version
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [{"name": "Original", "definition": graph_flow(("transcode.video", {"quality": 29}))}],
        "libraries": [{"name": "Original", "roots": [str(source.parent / "changed")], "flow_name": None}],
        "notifications": [],
        "nodes": [],
        "plugin_states": [],
        "uploaded_plugins": {},
        "config": None,
        "settings": {},
    }
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
        data={
            "password": "test-password",
            "selection": json.dumps({"config": False, "replace": True, selected: ["Original"]}),
        },
    )
    async with db() as session:
        saved_library = await session.get(Library, library_id)
        saved_flow = await session.get(Flow, flow_id)
        assert saved_library is not None and saved_flow is not None
        assert response.status_code == 409, "restore replaced configuration that still owns recovery"
        assert saved_library.roots == roots and saved_flow.version == version
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_reset_retains_identity(tmp_path: Path) -> None:
    async with database_context("backup-identity") as db:
        async with db() as session:
            original = await create_library(session, name="Old", roots=[str(tmp_path / "old")])
            old_id = original.id
            entry = TrashEntry(
                id="0123456789abcdef",
                library_id=old_id,
                original_path=str(tmp_path / "old/movie.mkv"),
                library_root=str(tmp_path / "old"),
                suffix=".mkv",
                size_bytes=8,
                mtime_ns=1,
                purge_at=datetime(2027, 1, 1, tzinfo=UTC),
            )
            session.add(entry)
            await session.commit()
            await reset_data(session, set())
            await session.commit()
            await restore_data(
                session,
                {"libraries": [{"name": "Unrelated", "roots": [str(tmp_path / "new")]}]},
                {"libraries": ["Unrelated"]},
            )
            await session.commit()
            replacement = (await session.scalars(select(Library))).one()
            historical = await session.scalar(select(TrashEntry).where(TrashEntry.id == entry.id))
            assert historical is not None and historical.library_id == old_id
            assert replacement.id != old_id, "new library inherited an unrelated historical trash owner's ID"


@pytest.mark.parametrize("role", ["partial", "replaced", "existing"])
def test_plugin_preserves_unexpected(tmp_path: Path, role: str) -> None:
    name = "filter.uploaded"
    destination = tmp_path / (name if role == "existing" else f".{name}.{role}")
    destination.mkdir()
    sentinel = destination / "personal.txt"
    sentinel.write_bytes(b"unrelated stored data")
    try:
        restore_plugin(tmp_path, name, package("updated"), replace=True)
    except OSError, ValueError:
        pass
    assert sentinel.is_file() and sentinel.read_bytes() == b"unrelated stored data", "restore deleted unexpected data"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["plain", "partial", "replaced"])
async def test_reset_preserves_unexpected(tmp_path: Path, role: str) -> None:
    root = tmp_path / "uploads"
    host = PluginHost([], uploaded_dir=root)
    destination = root / ("personal" if role == "plain" else f".filter.uploaded.{role}")
    destination.mkdir()
    sentinel = destination / "personal.txt"
    sentinel.write_bytes(b"unrelated stored data")
    try:
        try:
            await host.reset_uploaded()
        except OSError, ValueError:
            pass
        assert sentinel.is_file() and sentinel.read_bytes() == b"unrelated stored data", "reset deleted unexpected data"
    finally:
        await asyncio.wait_for(host.aclose(), 2)


def test_secret_directory_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    directory = tmp_path / "secrets"
    directory.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    open_file = os.open
    swapped = False

    def swap_open(file, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and fd_path(file, dir_fd).parent == directory:
            swapped = True
            directory.rename(tmp_path / "owned-secrets")
            directory.symlink_to(outside, target_is_directory=True)
        return open_file(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("server.backup.os.open", swap_open)
    try:
        restore_config(path, 'access_log = false\napi_key = "fixture-private-value"\n')
    except OSError, ValueError:
        pass
    assert list(outside.iterdir()) == [], "restore published credentials outside its checked secrets directory"


def test_config_directory_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "config"
    parent.mkdir()
    path = parent / "config.toml"
    path.write_bytes(b"original config")
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = outside / "config.toml"
    foreign.write_bytes(b"unrelated config")
    replace = os.replace

    def swap_replace(source, destination, **kwargs):
        parent.rename(tmp_path / "owned-config")
        parent.symlink_to(outside, target_is_directory=True)
        (outside / ".config.toml.partial").write_bytes(b"unrelated partial")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr("server.backup.os.replace", swap_replace)
    try:
        restore_config(path, "access_log = false\n")
    except OSError, ValueError:
        pass
    assert foreign.read_bytes() == b"unrelated config", "restore replaced an unrelated destination after a parent swap"
    assert (outside / ".config.toml.partial").read_bytes() == b"unrelated partial"


def test_config_partial_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    partial = tmp_path / ".config.toml.partial"
    replace = os.replace

    def publish_then_create(source, destination, **kwargs):
        result = replace(source, destination, **kwargs)
        partial.write_bytes(b"another writer's data")
        return result

    monkeypatch.setattr("server.backup.os.replace", publish_then_create)
    restore_config(path, "access_log = false\n")
    assert partial.is_file() and partial.read_bytes() == b"another writer's data", (
        "cleanup deleted an unexpected partial"
    )


def test_secret_publication_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    link = os.link
    foreign: list[Path] = []

    def replace_source(source, destination, **kwargs):
        partial = fd_path(source, kwargs.get("src_dir_fd"))
        partial.unlink()
        partial.write_bytes(b"another writer's data")
        foreign.append(partial)
        return link(source, destination, **kwargs)

    monkeypatch.setattr("server.backup.os.link", replace_source)
    try:
        restore_config(path, 'access_log = false\napi_key = "fixture-imported-value"\n')
    except OSError, ValueError:
        pass
    assert not path.exists() or load_config(path).api_key == "fixture-imported-value", (
        "restore published the wrong secret source"
    )
    assert foreign and foreign[0].is_file() and foreign[0].read_bytes() == b"another writer's data"


def test_config_secret_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    restore_config(path, 'access_log = false\napi_key = "original-key"\n')
    before = path.read_bytes()
    directory = tmp_path / "secrets"
    original_files = {file.name: file.read_bytes() for file in directory.iterdir()}
    displaced = tmp_path / "displaced-secrets"
    fsync = os.fsync
    swapped = False

    def swap_secrets(fd: int) -> None:
        nonlocal swapped
        if Path(os.readlink(f"/proc/self/fd/{fd}")).name == ".config.toml.partial":
            assert not swapped
            swapped = True
            directory.rename(displaced)
            directory.mkdir(mode=0o700)
            for name, data in original_files.items():
                (directory / name).write_bytes(data)
        fsync(fd)

    monkeypatch.setattr("server.backup.os.fsync", swap_secrets)
    try:
        restore_config(path, 'access_log = false\napi_key = "updated-key"\n')
    except OSError, ValueError:
        pass
    assert swapped
    assert path.read_bytes() == before, "config published references to a displaced secret generation"
    assert load_config(path).api_key == "original-key"
    assert {file.name: file.read_bytes() for file in directory.iterdir()} == original_files


@pytest.mark.parametrize("change", ["replacement", "hardlink"])
def test_published_secret_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    path = tmp_path / "config.toml"
    restore_config(path, 'access_log = false\napi_key = "old-key"\nnode_enrollment_token = "old-token"\n')
    before = path.read_bytes()
    directory = tmp_path / "secrets"
    original_files = {file.name: file.read_bytes() for file in directory.iterdir()}
    foreign = tmp_path / "retained-token"
    changed: Path | None = None
    fsync = os.fsync

    def change_published(fd: int) -> None:
        nonlocal changed
        partial = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if partial.name == ".config.toml.partial":
            assert changed is None
            staged = tomllib.loads(partial.read_text())
            changed = tmp_path / staged["node_enrollment_token"].removeprefix("file:")
            assert changed.read_bytes() == b"new-token\n"
            if change == "replacement":
                changed.rename(foreign)
                changed.write_bytes(b"another writer's data")
            else:
                os.link(changed, foreign)
        fsync(fd)

    monkeypatch.setattr("server.backup.os.fsync", change_published)
    with pytest.raises(ValueError):
        restore_config(path, 'access_log = false\napi_key = "new-key"\nnode_enrollment_token = "new-token"\n')
    assert changed is not None
    assert path.read_bytes() == before
    config = load_config(path)
    assert config.api_key == "old-key" and config.node_enrollment_token == "old-token"
    assert foreign.read_bytes() == b"new-token\n"
    retained = original_files.copy()
    if change == "replacement":
        assert changed.read_bytes() == b"another writer's data"
        retained[changed.name] = b"another writer's data"
    assert {file.name: file.read_bytes() for file in directory.iterdir()} == retained
    assert not (tmp_path / ".config.toml.partial").exists()


def test_plugin_publication_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, installed = install_plugin(package("original"), tmp_path, bundled_taken=lambda _: False)
    displaced = tmp_path / "original-installation"
    sentinel = installed / "personal.txt"
    copy = plugin_upload._copy_entry

    def copy_then_replace(archive, entry, target, written):
        result = copy(archive, entry, target, written)
        if entry.filename == "plugin.py":
            installed.rename(displaced)
            installed.mkdir()
            sentinel.write_bytes(b"unrelated old-bytes")
        return result

    monkeypatch.setattr(plugin_upload, "_copy_entry", copy_then_replace)
    try:
        restore_plugin(tmp_path, "filter.uploaded", package("updated"), replace=True)
    except OSError, ValueError:
        pass
    assert sentinel.is_file() and sentinel.read_bytes() == b"unrelated old-bytes", (
        "publication deleted a new unrelated file"
    )
    assert contents(displaced) == expected(package("original"))


def test_plugin_directory_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    outside = tmp_path / "outside"
    partial = outside / ".filter.uploaded.partial"
    partial.mkdir(parents=True)
    (partial / "plugin.toml").write_bytes(b"unrelated manifest")
    (partial / "personal.txt").write_bytes(b"unrelated personal data")
    before = {file.relative_to(outside): file.read_bytes() for file in outside.rglob("*") if file.is_file()}
    copy = plugin_upload._copy_entry
    swapped = False

    def swap_copy(archive, entry, target, written):
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(tmp_path / "owned-uploads")
            root.symlink_to(outside, target_is_directory=True)
        return copy(archive, entry, target, written)

    monkeypatch.setattr(plugin_upload, "_copy_entry", swap_copy)
    try:
        restore_plugin(root, "filter.uploaded", package("updated"), replace=True)
    except OSError, ValueError:
        pass
    after = {file.relative_to(outside): file.read_bytes() for file in outside.rglob("*") if file.is_file()}
    assert after == before, "plugin restore escaped its acquired upload directory and replaced unrelated files"


def test_plugin_manifest_collision(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package("original"))) as original, zipfile.ZipFile(buffer, "w") as archive:
        for item in original.infolist():
            archive.writestr(item.filename, original.read(item))
        archive.writestr("./plugin.toml", original.read("plugin.toml").replace(b"filter.uploaded", b"filter.shadow"))
    try:
        data = plugin_archive(
            "filter.uploaded", base64.b64encode(buffer.getvalue()).decode(), bundled_taken=lambda _: False
        )
        restore_plugin(tmp_path, "filter.uploaded", data, replace=False)
    except OSError, ValueError:
        pass
    installed = tmp_path / "filter.uploaded"
    assert not installed.exists() or load_manifest(installed).id == "filter.uploaded", (
        "a ZIP path alias replaced the validated manifest with a different plugin owner"
    )


@pytest.mark.asyncio
async def test_duplicate_plugin_states(backup_env) -> None:
    client, config_path, db, app = backup_env
    _, installed = install_plugin(package("original"), config_path.parent / "bundled", bundled_taken=lambda _: False)
    app.state.plugin_host._register(installed)
    async with db() as session:
        session.add(PluginState(plugin_id="filter.uploaded", enabled=False))
        await session.commit()
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [],
        "notifications": [],
        "plugin_states": [{"id": "filter.uploaded", "enabled": False}, {"id": "filter.uploaded", "enabled": True}],
        "uploaded_plugins": {},
        "config": None,
        "settings": {},
    }
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
        data={
            "password": "test-password",
            "selection": json.dumps({"config": False, "replace": True, "plugins": ["filter.uploaded"]}),
        },
    )
    async with db() as session:
        state = await session.scalar(select(PluginState).where(PluginState.plugin_id == "filter.uploaded"))
        assert state is not None
        assert response.status_code == 422 and not state.enabled, (
            "duplicate state records silently enabled a disabled plugin"
        )


@pytest.mark.asyncio
async def test_restore_reports_commit(backup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, db, app = backup_env
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [],
        "notifications": [{"name": "Restored", "url": "json://example.test/hook"}],
        "plugin_states": [],
        "uploaded_plugins": {},
        "config": None,
        "settings": {},
    }
    monkeypatch.setattr(
        app.state.probes, "check_library_access", AsyncMock(side_effect=ValueError("library check failed"))
    )
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
        data={"password": "test-password", "selection": json.dumps({"config": False, "notifications": ["Restored"]})},
    )
    async with db() as session:
        assert await session.scalar(select(NotificationTarget).where(NotificationTarget.name == "Restored")) is not None
    assert response.status_code == 500, response.json()


@pytest.mark.asyncio
async def test_export_consistent_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_context("export-snapshot") as db:
        definition = FlowDefinition.model_validate(graph_flow("transcode.video"))
        async with db() as session:
            flow = await save_flow(session, "Original", definition)
            library = await create_library(session, name="Library", roots=[str(tmp_path)], flow_id=flow.id)
            await session.commit()
            library_id = library.id
        scalars = AsyncSession.scalars
        changed = False

        async def read_then_change(session, statement, *args, **kwargs):
            nonlocal changed
            result = await scalars(session, statement, *args, **kwargs)
            if not changed and statement.column_descriptions[0].get("entity") is Flow:
                changed = True
                async with db() as writer:
                    new_flow = await save_flow(writer, "Replacement", definition)
                    saved = await writer.get(Library, library_id)
                    assert saved is not None
                    saved.flow_id = new_flow.id
                    await writer.commit()
            return result

        monkeypatch.setattr(AsyncSession, "scalars", read_then_change)
        async with db() as session:
            exported = await export_data(session)
        assert changed
        assert exported["libraries"][0]["flow_name"] in {"Original", "Replacement"}, (
            "backup silently dropped a bound flow while its independent reads crossed a configuration edit"
        )
        assert exported["libraries"][0]["flow_name"] in {item["name"] for item in exported["flows"]}


@pytest.mark.asyncio
async def test_export_plugin_snapshot(backup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, app = backup_env
    host = app.state.plugin_host
    root = host.uploaded_dir
    assert root is not None
    original, updated = package("original"), package("updated")
    _, installed = install_plugin(original, root, bundled_taken=lambda _: False)
    host._register(installed)
    host.upload_lock = ObservedUploadLock()
    await host.upload_lock.acquire()
    owned = True
    exporting = asyncio.create_task(client.post("/api/backups/export", json={"password": "test-password"}))
    waiting = asyncio.create_task(host.upload_lock.waiting.wait())
    try:
        done, _ = await asyncio.wait([exporting, waiting], timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert done, "export neither waited for the installer nor completed"
        assert waiting in done and not exporting.done(), "export bypassed the plugin install lock"
        install_plugin(updated, root, bundled_taken=lambda _: False)
        owned = False
        host.upload_lock.release()
        response = await asyncio.wait_for(exporting, 2)
        assert response.status_code == 200
        data = json.loads(decrypt(response.content, "test-password"))
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(data["uploaded_plugins"]["filter.uploaded"]))) as archive:
            snapshot = (
                tomllib.loads(archive.read("plugin.toml").decode())["version"],
                archive.read("helper.py"),
                archive.read("plugin.py"),
            )
        assert snapshot in {expected(original), expected(updated)}, (
            "backup archived code from two installation generations"
        )
        assert snapshot == expected(updated)
    finally:
        if owned:
            owned = False
            host.upload_lock.release()
        waiting.cancel()
        await asyncio.wait_for(asyncio.gather(exporting, waiting, return_exceptions=True), 5)


def test_export_preserves_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, installed = install_plugin(package("original"), tmp_path, bundled_taken=lambda _: False)
    foreign = tmp_path / "personal.txt"
    foreign.write_bytes(b"unrelated personal data")
    open_member = zipfile.ZipFile.open
    swapped = False

    def swap_member(archive, name, mode="r", *args, **kwargs):
        nonlocal swapped
        if mode == "w" and name == "helper.py":
            swapped = True
            (installed / "helper.py").unlink()
            (installed / "helper.py").symlink_to(foreign)
        return open_member(archive, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", swap_member)
    with pytest.raises((OSError, ValueError)):
        backups_api.export_plugins(tmp_path)
    assert swapped
    assert foreign.read_bytes() == b"unrelated personal data"
    assert (installed / "helper.py").is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["database", "config"])
async def test_cancel_drains_restore(backup_env, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    client, path, db, app = backup_env
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    worker_release, worker_completed = threading.Event(), threading.Event()
    app.state.reset_lock = ObservedUploadLock()
    loop = asyncio.get_running_loop()
    restore = restore_data
    fsync = os.fsync

    async def paused_restore(session, data, selection):
        entered.set()
        try:
            await release.wait()
            await restore(session, data, selection)
        finally:
            completed.set()

    def paused_fsync(fd: int) -> None:
        if Path(os.readlink(f"/proc/self/fd/{fd}")).name == ".config.toml.partial":
            loop.call_soon_threadsafe(entered.set)
            try:
                if not worker_release.wait(5):
                    raise TimeoutError("config writer was not released")
                fsync(fd)
            finally:
                worker_completed.set()
        else:
            fsync(fd)

    if boundary == "database":
        monkeypatch.setattr(backups_api, "restore_data", paused_restore)
    else:
        monkeypatch.setattr("server.backup.os.fsync", paused_fsync)
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [],
        "notifications": [{"name": "Restored", "url": "json://example.test/hook"}],
        "plugin_states": [],
        "uploaded_plugins": {},
        "config": "access_log = false\n" if boundary == "config" else None,
        "settings": {},
    }
    tasks = [
        asyncio.create_task(
            client.post(
                "/api/backups/restore",
                files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
                data={
                    "password": "test-password",
                    "selection": json.dumps(
                        {"config": boundary == "config", "replace": True, "notifications": ["Restored"]}
                    ),
                },
            )
        )
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        tasks.append(
            asyncio.create_task(
                backups_api.export_backup(
                    backups_api.PasswordBody(password="test-password"), Request({"type": "http", "app": app})
                )
            )
        )
        await asyncio.wait_for(app.state.reset_lock.waiting.wait(), 2)
        for _ in range(3):
            tasks[0].cancel()
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await asyncio.wait_for(checkpoint, 2)
            gc.collect()
            assert not tasks[0].done() and not tasks[1].done()
            assert app.state.reset_lock.locked() and app.state.plugin_host.upload_lock.locked()
            assert app.state.scanner._scan_lock.locked()
            assert not completed.is_set() and not worker_completed.is_set()
        release.set()
        worker_release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert isinstance(outcomes[1], Response) and outcomes[1].status_code == 200
        exported = json.loads(decrypt(bytes(outcomes[1].body), "test-password"))
        assert exported["notifications"] == data["notifications"]
        async with db() as session:
            assert (
                await session.scalar(select(NotificationTarget).where(NotificationTarget.name == "Restored"))
                is not None
            )
        if boundary == "config":
            assert await asyncio.to_thread(worker_completed.wait, 2)
            assert path.read_text() == data["config"]
        else:
            assert completed.is_set()
        assert not app.state.reset_lock.locked() and not app.state.plugin_host.upload_lock.locked()
    finally:
        release.set()
        worker_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        if boundary == "config" and entered.is_set():
            assert await asyncio.to_thread(worker_completed.wait, 2)
        gc.collect()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["restore", "reset"])
async def test_backup_waits_upload(backup_env, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    client, _, _, app = backup_env
    host = app.state.plugin_host
    root = host.uploaded_dir
    assert root is not None
    _, installed = install_plugin(package("original"), root, bundled_taken=lambda _: False)
    host._register(installed)
    host.upload_lock = ObservedUploadLock()
    entered, mutated = asyncio.Event(), asyncio.Event()
    release, completed = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    copy = plugin_upload._copy_entry

    def paused_copy(
        archive: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, written: int
    ) -> tuple[int, tuple[int, ...]]:
        if (
            target.parent.resolve() == root / ".filter.uploaded.partial"
            and entry.filename == "plugin.py"
            and b"alpha" in archive.read("helper.py")
        ):
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(5):
                    raise TimeoutError("upload was not released")
                return copy(archive, entry, target, written)
            finally:
                completed.set()
        return copy(archive, entry, target, written)

    async def hot_load(path: Path):
        host._register(path)
        return load_manifest(path)

    async def payload(_host, _info):
        return {"installed": True}

    monkeypatch.setattr(plugin_upload, "_copy_entry", paused_copy)
    monkeypatch.setattr(host, "hot_load", hot_load)
    monkeypatch.setattr(plugins_api, "_plugin_payload", payload)
    if action == "restore":
        restore = restore_plugin

        def observed_restore(directory, name, data, *, replace):
            if directory == root:
                loop.call_soon_threadsafe(mutated.set)
            return restore(directory, name, data, replace=replace)

        monkeypatch.setattr("server.api.backups.restore_plugin", observed_restore)
    else:
        reset = host.reset_uploaded

        async def observed_reset():
            mutated.set()
            await reset()

        monkeypatch.setattr(host, "reset_uploaded", observed_reset)
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [],
        "notifications": [],
        "plugin_states": [{"id": "filter.uploaded", "enabled": False}],
        "uploaded_plugins": {"filter.uploaded": base64.b64encode(package("beta")).decode()},
        "config": None,
        "settings": {},
    }
    tasks = []
    probes = []
    with io.BytesIO(package("alpha")) as stream:
        upload_file = UploadFile(filename="plugin.zip", file=stream)
        tasks.append(
            asyncio.create_task(
                plugins_api.upload_plugin(
                    Request({"type": "http", "app": app}),
                    Principal(user_id=1, username="operator"),
                    upload_file,
                )
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if action == "restore":
                tasks.append(
                    asyncio.create_task(
                        client.post(
                            "/api/backups/restore",
                            files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
                            data={
                                "password": "test-password",
                                "selection": json.dumps(
                                    {"config": False, "plugins": ["filter.uploaded"], "replace": True}
                                ),
                            },
                        )
                    )
                )
            else:
                tasks.append(asyncio.create_task(client.post("/api/backups/reset", json={"keep": ["nodes"]})))
            probes = [asyncio.create_task(mutated.wait()), asyncio.create_task(host.upload_lock.waiting.wait())]
            done, _ = await asyncio.wait(probes, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            assert done, "backup neither acquired the upload lock nor attempted publication"
            overlapped = mutated.is_set()
            release.set()
            outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            assert await asyncio.to_thread(completed.wait, 2)
            assert not overlapped, "backup mutated the installer-owned tree before its worker completed"
            assert isinstance(outcomes[0], dict)
            assert isinstance(outcomes[1], httpx.Response)
            assert outcomes[1].status_code == 200
            if action == "restore":
                assert contents(installed) == expected(package("beta"))
            else:
                assert not installed.exists()
        finally:
            release.set()
            for probe in probes:
                probe.cancel()
            await asyncio.wait_for(asyncio.gather(*probes, return_exceptions=True), 2)
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            if entered.is_set():
                assert await asyncio.to_thread(completed.wait, 2)


@pytest.mark.asyncio
async def test_restore_conflict_reported(backup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, db, _ = backup_env
    execute = AsyncSession.execute
    inserted = False
    incoming = "json://example.test/notify"

    async def insert_before_check(session: AsyncSession, statement, *args, **kwargs):
        nonlocal inserted
        if (
            not inserted
            and statement.is_select
            and statement.column_descriptions[0].get("entity") is NotificationTarget
            and statement._for_update_arg is not None
        ):
            inserted = True
            async with db() as writer:
                writer.add(NotificationTarget(name="Concurrent", url="json://example.test/existing"))
                await writer.commit()
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", insert_before_check)
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [],
        "notifications": [{"name": "Concurrent", "url": incoming}],
        "plugin_states": [],
        "uploaded_plugins": {},
        "config": None,
        "settings": {},
    }
    response = await asyncio.wait_for(
        client.post(
            "/api/backups/restore",
            files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
            data={
                "password": "test-password",
                "selection": json.dumps({"config": False, "notifications": ["Concurrent"]}),
            },
        ),
        timeout=5,
    )
    async with db() as session:
        saved = await session.scalar(select(NotificationTarget).where(NotificationTarget.name == "Concurrent"))
        assert saved is not None and saved.url == "json://example.test/existing"
    assert response.status_code == 409
    assert inserted


@pytest.mark.asyncio
async def test_reset_reports_commit(backup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, db, app = backup_env
    async with db() as session:
        library = await create_library(session, name="Cleared", roots=["/fixture/media"])
        await session.commit()
        library_id = library.id
    for name in (
        "scheduler",
        "jobs",
        "scanner",
        "probes",
        "flow_enqueue",
        "healthchecks",
        "staging",
        "notifications",
    ):
        service = getattr(app.state, name)
        close = "close" if name in {"jobs", "probes", "flow_enqueue", "healthchecks"} else "aclose"
        monkeypatch.setattr(service, close, AsyncMock())
        monkeypatch.setattr(
            service, "start", AsyncMock() if name in {"jobs", "probes", "flow_enqueue"} else lambda: None
        )
    monkeypatch.setattr(app.state.probes, "start", AsyncMock(side_effect=ValueError("restart failed")))
    app.state.services_running = True
    try:
        response = await asyncio.wait_for(client.post("/api/backups/reset", json={"keep": ["nodes", "plugins"]}), 2)
        async with db() as session:
            assert await session.get(Library, library_id) is None
        assert response.status_code == 500, response.json()
    finally:
        app.state.services_running = False


@pytest.mark.asyncio
async def test_reset_waits_restore(backup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, config_path, db, app = backup_env
    entered, release, mutated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    app.state.scanner._scan_lock = ObservedUploadLock()
    restore = restore_data
    reset = reset_data

    async def paused_restore(session, data, selection):
        entered.set()
        await release.wait()
        await restore(session, data, selection)

    async def observed_reset(session, keep):
        mutated.set()
        await reset(session, keep)

    monkeypatch.setattr("server.api.backups.restore_data", paused_restore)
    monkeypatch.setattr("server.api.backups.reset_data", observed_reset)
    data = {
        "_type": "millwright-backup",
        "_version": 1,
        "webhooks": [],
        "flows": [],
        "libraries": [{"name": "Restored", "roots": [str(config_path.parent / "media")], "flow_name": None}],
        "notifications": [],
        "plugin_states": [],
        "uploaded_plugins": {},
        "config": None,
        "settings": {},
    }
    tasks = [
        asyncio.create_task(
            client.post(
                "/api/backups/restore",
                files={"file": ("backup.mwbackup", encrypt(json.dumps(data).encode(), "test-password"))},
                data={
                    "password": "test-password",
                    "selection": json.dumps({"config": False, "libraries": ["Restored"]}),
                },
            )
        )
    ]
    probe = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        tasks.append(asyncio.create_task(client.post("/api/backups/reset", json={"keep": ["nodes", "plugins"]})))
        probe = asyncio.create_task(app.state.scanner._scan_lock.waiting.wait())
        done, _ = await asyncio.wait([tasks[1], probe], timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert done, "reset neither waited for the running restore nor responded"
        overlapped = mutated.is_set()
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert not overlapped, "reset cleared data before the running restore finished its database changes"
        assert isinstance(outcomes[0], httpx.Response) and isinstance(outcomes[1], httpx.Response)
        assert outcomes[0].status_code == 200
        assert outcomes[1].status_code in {200, 409}
        if outcomes[1].status_code == 200:
            async with db() as session:
                assert not (await session.scalars(select(Library))).all()
    finally:
        release.set()
        if probe is not None:
            probe.cancel()
            await asyncio.wait_for(asyncio.gather(probe, return_exceptions=True), 2)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
