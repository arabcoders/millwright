from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import base64
import io
import json
import tomllib
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.helpers import create_test_app as create_app
from server.backup import (
    MAX_SIZE,
    decrypt,
    encrypt,
    export_config,
    export_data,
    plugin_archive,
    restore_config,
    restore_data,
    reset_data,
)
from server.core.config import ensure_config, load_config
from server.core.auth import authenticate_session
from server.core.config import AuthConfig, OidcConfig, ServerConfig
from server.flows.definition import FlowDefinition
from server.flows.store import save_flow
from server.scheduler.service import Scheduler
from server.settings import OperationalSettings, SETTINGS_KEY
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    NodeRow,
    NotificationDelivery,
    ProbeRow,
    PluginState,
    TrashEntry,
)
from server.nodehub.store import authenticate, hash_token
from server.queue.store import create_library
from server.plugins.host import PluginHost
from server.stats import build_snapshot
from tests.helpers import database_context, fd_path, graph_flow


def test_roundtrip() -> None:
    payload = b"backup contents"
    encrypted = encrypt(payload, "correct horse")
    assert encrypted != payload
    assert decrypt(encrypted, "correct horse") == payload
    assert encrypted != encrypt(payload, "correct horse")


def test_tamper_rejected() -> None:
    encrypted = bytearray(encrypt(b"backup", "password"))
    with pytest.raises(ValueError):
        decrypt(bytes(encrypted[:-1]), "password")
    encrypted[-1] ^= 1
    with pytest.raises(ValueError):
        decrypt(bytes(encrypted), "password")


def test_wrong_password() -> None:
    with pytest.raises(ValueError):
        decrypt(encrypt(b"backup", "right"), "wrong")


def test_size_rejected() -> None:
    with pytest.raises(ValueError):
        encrypt(b"x" * MAX_SIZE, "password")
    with pytest.raises(ValueError):
        decrypt(b"x" * MAX_SIZE, "password")


def test_restores_config_secrets(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    ensure_config(path)
    original = load_config(path)
    exported = export_config(path)
    assert exported is not None
    assert tomllib.loads(exported)["api_key"] == original.api_key
    assert tomllib.loads(exported)["node_enrollment_token"] == original.node_enrollment_token
    restore_config(
        path,
        'api_key = "other-key"\nnode_enrollment_token = "other-token"\n'
        '[auth.oidc]\nissuer = "https://sso.example.test"\nclient_id = "client"\n'
        'secret = "oidc-secret"\nredirect_uri = "https://example.test/cb"\n',
    )
    restored = load_config(path)
    assert restored.api_key == "other-key"
    assert restored.node_enrollment_token == "other-token"
    assert restored.auth.oidc is not None and restored.auth.oidc.secret == "oidc-secret"
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    references = [
        config["api_key"],
        config["node_enrollment_token"],
        config["auth"]["oidc"]["secret"],
        config["auth"]["oidc"]["client_id"],
    ]
    assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
    for reference in references:
        assert reference.startswith("file:secrets/")
        assert (tmp_path / reference.removeprefix("file:")).stat().st_mode & 0o777 == 0o600


def test_rejects_secret_symlink(tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    (tmp_path / "secrets").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError):
        restore_config(tmp_path / "config.toml", 'api_key = "from-backup"\n')
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("stage", ["secret", "config"])
def test_config_publish_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    path = tmp_path / "config.toml"
    restore_config(path, 'api_key = "old-key"\nnode_enrollment_token = "old-token"\n')
    before = path.read_bytes()
    files = {file.name: file.read_bytes() for file in (tmp_path / "secrets").iterdir()}

    def fail(*_args, **_kwargs) -> None:
        raise OSError("config publication failed")

    if stage == "config":
        monkeypatch.setattr("server.backup.os.replace", fail)
    else:
        from server.backup import os

        link = os.link
        calls = 0

        def fail_second(source: str | Path, destination: str | Path, **kwargs) -> None:
            nonlocal calls
            calls += 1
            assert fd_path(destination, kwargs.get("dst_dir_fd")).parent == tmp_path / "secrets"
            if calls == 2:
                raise OSError("secret publication failed")
            link(source, destination, **kwargs)

        monkeypatch.setattr("server.backup.os.link", fail_second)
    with pytest.raises(OSError):
        restore_config(path, 'api_key = "new-key"\nnode_enrollment_token = "new-token"\n')
    assert path.read_bytes() == before
    assert load_config(path).api_key == "old-key" and load_config(path).node_enrollment_token == "old-token"
    assert {file.name: file.read_bytes() for file in (tmp_path / "secrets").iterdir()} == files


@pytest.mark.parametrize("field", ["client_id", "secret"])
def test_unresolved_oidc_rejected(tmp_path: Path, field: str) -> None:
    content = (
        '[auth.oidc]\nissuer = "https://sso.example.test"\nclient_id = "client"\n'
        'secret = "oidc-secret"\nredirect_uri = "https://example.test/cb"\n'
    )
    value = "client" if field == "client_id" else "oidc-secret"
    content = content.replace(f'{field} = "{value}"', f'{field} = "file:missing"')
    with pytest.raises(ValueError):
        restore_config(tmp_path / "config.toml", content)
    assert not (tmp_path / "config.toml").exists()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context("backup_data") as factory:
        yield factory


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [None, 86400])
async def test_roundtrip_binds_flow(db: async_sessionmaker[AsyncSession], interval: int | None) -> None:
    async with db() as session:
        await restore_data(
            session,
            {
                "flows": [
                    {
                        "name": "film flow",
                        "definition": FlowDefinition.model_validate(
                            graph_flow(("transcode.video", {"quality": 21}))
                        ).model_dump(mode="json"),
                    }
                ],
                "libraries": [
                    {
                        "name": "films",
                        "roots": ["/media"],
                        "flow_name": "film flow",
                        "watch": True,
                        "healthcheck_enabled": True,
                        "scan_interval_s": interval,
                    }
                ],
            },
            {"flows": ["film flow"], "libraries": ["films"], "roots": {"films": ["/restored/media"]}},
        )
        await session.commit()
        library = (await session.scalars(select(Library))).one()
        assert library.flow_id is not None
        assert library.watch is False
        assert library.scan_interval_s == interval and library.suspended and not library.scan_on_start
        assert library.roots == ["/restored/media"]
        assert library.healthcheck_enabled is True
        await session.rollback()
        exported = await export_data(session)
        assert exported["libraries"][0]["healthcheck_enabled"] is True
        assert exported["libraries"][0]["scan_interval_s"] == interval


@pytest.mark.asyncio
async def test_missing_selection_rejected(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        with pytest.raises(ValueError):
            await restore_data(session, {"flows": []}, {"flows": ["missing"]})


@pytest.mark.asyncio
async def test_empty_roots_rejected(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        with pytest.raises(ValueError):
            await restore_data(
                session,
                {"libraries": [{"name": "films", "roots": ["/backup/media"]}]},
                {"libraries": ["films"], "roots": {"films": []}},
            )
        assert not (await session.scalars(select(Library))).all()


@pytest.mark.asyncio
async def test_plugins_disabled(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        await restore_data(
            session,
            {"plugin_states": [{"id": "demo.plugin", "enabled": True}], "uploaded_plugins": {"demo.plugin": "archive"}},
            {"plugins": ["demo.plugin"]},
        )
        await session.commit()
        state = await session.scalar(select(PluginState).where(PluginState.plugin_id == "demo.plugin"))
        assert state is not None and state.enabled is False


@pytest.mark.asyncio
async def test_node_credentials_roundtrip(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        session.add(
            NodeRow(
                name="worker-a",
                display_name="worker-a",
                token_hash=hash_token("original"),
                state="offline",
                paused=True,
            )
        )
        await session.commit()
        data = await export_data(session)
        node = data["nodes"][0]
        assert node["token_hash"] == hash_token("original")
        await session.close()
        await restore_data(
            session,
            {
                "nodes": [
                    {
                        **node,
                        "display_name": "Main encoder",
                        "token_hash": hash_token("restored"),
                        "transcode_limit": 2,
                        "healthcheck_limit": 0,
                        "mkv_stats_limit": 3,
                    },
                    {
                        "name": "worker-b",
                        "display_name": "worker-b",
                        "token_hash": hash_token("second"),
                        "paused": False,
                        "transcode_limit": 1,
                        "healthcheck_limit": 1,
                        "mkv_stats_limit": 1,
                    },
                ]
            },
            {"nodes": ["worker-a", "worker-b"], "replace": True},
        )
        await session.commit()
        assert await authenticate(session, "worker-a", "original") is None
        restored = await authenticate(session, "worker-a", "restored")
        assert restored is not None and restored.paused is True and restored.transcode_limit == 2
        assert restored.healthcheck_limit == 0 and restored.mkv_stats_limit == 3
        assert restored.display_name == "Main encoder"
        second = await authenticate(session, "worker-b", "second")
        assert second is not None and second.state == "offline"


@pytest.mark.asyncio
async def test_invalid_node_rejected(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        with pytest.raises(ValueError):
            await restore_data(
                session,
                {"nodes": [{"name": "worker-a", "token_hash": "not-a-hash", "paused": False}]},
                {"nodes": ["worker-a"]},
            )


@pytest.mark.asyncio
async def test_reset_rejects_unknown_category(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        with pytest.raises(ValueError):
            await reset_data(session, {"jobs"})


@pytest.mark.asyncio
async def test_reset_retains_library_without_flow(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        flow = await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        await create_library(session, name="Movies", roots=["/media"], flow_id=flow.id)
        session.add(NodeRow(name="worker", display_name="worker", token_hash=hash_token("secret")))
        await session.commit()
        await reset_data(session, {"libraries", "nodes"})
        await session.commit()
        assert not (await session.scalars(select(Flow))).all()
        retained = (await session.scalars(select(Library))).one()
        assert retained.flow_id is None and retained.suspended
        assert (await session.scalars(select(NodeRow))).all()


@pytest.mark.asyncio
async def test_reset_keeps_physical_file(db: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"media")
    async with db() as session:
        flow = await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        library = await create_library(session, name="Movies", roots=[str(tmp_path)], flow_id=flow.id)
        session.add(
            MediaFile(
                library_id=library.id,
                path=str(path),
                size=5,
                mtime_ns=path.stat().st_mtime_ns,
                container="matroska",
                width=1,
                height=1,
                medium="video",
                last_scanned_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await reset_data(session, set())
        await session.commit()
        assert not (await session.scalars(select(MediaFile))).all()
    assert path.read_bytes() == b"media"


@pytest.mark.asyncio
async def test_reset_suspends_libraries(db: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    async with db() as session:
        flow = await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        await create_library(
            session, name="Movies", roots=[str(tmp_path)], flow_id=flow.id, watch=True, scan_on_start=True
        )
        await session.commit()
        await reset_data(session, {"libraries", "flows"})
        await session.commit()
        retained = (await session.scalars(select(Library))).one()
        assert retained.suspended and retained.flow_id == flow.id
        assert retained.watch and retained.scan_on_start and retained.roots == [str(tmp_path)]
        assert (await session.scalars(select(Flow))).one().id == flow.id


@pytest.mark.asyncio
async def test_reset_preserves_trash_metadata(db: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    trash_path = tmp_path / "trash.mkv"
    trash_path.write_bytes(b"trash")
    async with db() as session:
        session.add(
            TrashEntry(
                id="trash-entry",
                library_id=1,
                original_path=str(trash_path),
                library_root="/media",
                suffix=".mkv",
                size_bytes=5,
                mtime_ns=1,
                purge_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await reset_data(session, {"libraries", "flows", "nodes"})
        await session.commit()
        assert (await session.scalars(select(TrashEntry))).one().original_path == str(trash_path)
        assert not (await session.scalars(select(MediaFile))).all()
        assert not (await session.scalars(select(Job))).all()
    assert trash_path.read_bytes() == b"trash"


@pytest.mark.asyncio
async def test_reset_preserves_session(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session:
        _, token = await create_user_session(session, "tester", 1)
        await session.commit()
        await reset_data(session, set())
        await session.commit()
        assert await authenticate_session(session, token) is not None


@pytest.mark.asyncio
async def test_reset_clears_job_graph(db: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    media_path = tmp_path / "movie.mkv"
    media_path.write_bytes(b"media")
    async with db() as session:
        flow = await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        library = await create_library(session, name="Movies", roots=[str(tmp_path)], flow_id=flow.id)
        media = MediaFile(
            library_id=library.id,
            path=str(media_path),
            size=5,
            mtime_ns=media_path.stat().st_mtime_ns,
            container="matroska",
            medium="video",
            last_scanned_at=datetime.now(UTC),
        )
        session.add(media)
        await session.flush()
        probe = ProbeRow(media_file_id=media.id, raw_json="{}")
        session.add(probe)
        await session.flush()
        media.probe_id = probe.id
        job = Job(media_file_id=media.id, library_id=library.id, status="failed")
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status="failed")
        session.add(attempt)
        await session.flush()
        event = JobEvent(job_id=job.id, attempt_id=attempt.id, type="queued", payload={})
        session.add(event)
        await session.flush()
        session.add(NotificationDelivery(job_id=job.id, job_event_id=event.id, target_name="test", status="pending"))
        session.add(
            FinalizationJournal(
                attempt_id=attempt.id,
                source_path=str(media_path),
                destination_path=str(media_path),
                partial_path=str(media_path) + ".partial",
                trash_path=str(media_path) + ".trash",
                state="rolled_back",
            )
        )
        await session.commit()

        await reset_data(session, {"libraries"})
        await session.commit()
        assert (await session.scalars(select(Library))).one().flow_id is None
        assert not (await session.scalars(select(MediaFile))).all()
        assert not (await session.scalars(select(ProbeRow))).all()
        assert not (await session.scalars(select(Job))).all()
        assert not (await session.scalars(select(JobAttempt))).all()
        assert not (await session.scalars(select(JobEvent))).all()
        assert not (await session.scalars(select(NotificationDelivery))).all()
        assert not (await session.scalars(select(FinalizationJournal))).all()
        from server.search import search_files

        assert await search_files(session, "matroska") == []
    assert media_path.read_bytes() == b"media"


@pytest_asyncio.fixture
async def backup_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI]]:
    config_path = tmp_path / "config.toml"
    (tmp_path / "oidc.txt").write_text("secret-from-file\n", encoding="utf-8")
    (tmp_path / "client-id.txt").write_text("millwright\n", encoding="utf-8")
    (tmp_path / "api-key.txt").write_text("api-secret-from-file\n", encoding="utf-8")
    (tmp_path / "enrollment.txt").write_text("node-private-token\n", encoding="utf-8")
    config_path.write_text(
        '# My server settings\napi_key = "file:api-key.txt"\n'
        'node_enrollment_token = "file:enrollment.txt"\n'
        '[auth.oidc]\nissuer = "https://sso.example.test"\n'
        'client_id = "file:client-id.txt"\nsecret = "file:oidc.txt"\nredirect_uri = "https://example.test/cb"\n',
        encoding="utf-8",
    )
    oidc = OidcConfig(
        issuer="https://sso.example.test",
        client_id="millwright",
        secret="secret-from-file",
        redirect_uri="https://example.test/cb",
    )
    async with database_context("backups") as db:
        async with db() as session:
            _, token = await create_user_session(session, "tester", 1)
            await session.commit()
        host = PluginHost([], uploaded_dir=tmp_path / "plugins")
        app = create_app(
            db,
            config=ServerConfig(
                auth=AuthConfig(oidc=oidc),
            ),
            config_path=config_path,
            plugin_host=host,
            cache_root=tmp_path / "cache",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers={"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"},
            ) as client:
                yield client, config_path, db, app
        finally:
            await host.aclose()
            if app.state.auth.http_client is not None:
                await app.state.auth.http_client.aclose()


@pytest.mark.asyncio
async def test_backup_requires_auth(tmp_path: Path) -> None:
    async with database_context("backup_no_auth") as db:
        host = PluginHost([], uploaded_dir=tmp_path / "plugins")
        app = create_app(db, plugin_host=host, config_path=tmp_path / "config.toml")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (await client.post("/api/backups/export", json={"password": "long-password"})).status_code == 401
                assert (await client.post("/api/backups/export", json={})).status_code == 401
                assert (await client.post("/api/backups/reset", json={"keep": []})).status_code == 401
                for action in ("preview", "restore"):
                    response = await client.post(
                        f"/api/backups/{action}",
                        files={"file": ("backup.mwbackup", b"invalid")},
                        data={"password": "long-password", "selection": "{}"},
                    )
                    assert response.status_code == 401
        finally:
            await host.aclose()


@pytest.mark.asyncio
async def test_backup_reset(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, config_path, db, app = backup_env
    original_config = config_path.read_bytes()
    await app.state.settings.update({"max_probes": 2})
    await app.state.store.put("activity_paused", True)
    async with db() as session:
        await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        library = await create_library(session, name="Movies", roots=[str(config_path.parent)])
        media = MediaFile(
            library_id=library.id,
            path=str(config_path.parent / "movie.mkv"),
            size=5,
            mtime_ns=1,
            container="matroska",
            medium="video",
            last_scanned_at=datetime.now(UTC),
        )
        session.add(media)
        await session.flush()
        session.add(Job(media_file_id=media.id, library_id=library.id, status="queued"))
        await session.commit()
    response = await client.post("/api/backups/reset", json={"keep": []})
    assert response.status_code == 200, response.text
    assert response.json() == {"reset": True}
    assert config_path.read_bytes() == original_config
    assert (await client.get("/api/settings")).json() == OperationalSettings().model_dump(mode="json")
    assert await app.state.store.get(SETTINGS_KEY) is None
    assert await app.state.store.get("activity_paused") is True
    async with db() as session:
        assert await authenticate_session(session, client.headers["Cookie"].removeprefix("mw_session=")) is not None
        assert not (await session.scalars(select(Flow))).all()
        assert not (await session.scalars(select(Job))).all()


@pytest.mark.asyncio
async def test_backup_reset_busy(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, _, app = backup_env
    lock = app.state.reset_lock
    await lock.acquire()
    try:
        response = await client.post("/api/backups/reset", json={"keep": []})
    finally:
        lock.release()
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["actions_pending", "prepared", "discard_prepared"])
async def test_reset_preserves_recovery(backup_env, record_type: str) -> None:
    client, config_path, db, _ = backup_env
    source = config_path.parent / "movie.mkv"
    source.write_bytes(b"original")
    async with db() as session:
        library = await create_library(session, name="Recovery", roots=[str(config_path.parent)])
        media = MediaFile(
            library_id=library.id,
            path=str(source),
            size=source.stat().st_size,
            mtime_ns=source.stat().st_mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=datetime.now(UTC),
        )
        session.add(media)
        await session.flush()
        status = "succeeded" if record_type == "actions_pending" else "failed"
        job = Job(media_file_id=media.id, library_id=library.id, status=status, attempts=1)
        session.add(job)
        await session.flush()
        attempt = JobAttempt(job_id=job.id, attempt_number=1, status=status)
        session.add(attempt)
        await session.flush()
        if record_type != "actions_pending":
            session.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path=media.path,
                    destination_path=media.path,
                    partial_path=media.path + ".partial",
                    trash_path=media.path + ".discard",
                    state=record_type,
                )
            )
        else:
            session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type=record_type, payload={}))
        await session.commit()
        job_id = job.id
    response = await client.post("/api/backups/reset", json={"keep": ["libraries", "nodes", "plugins"]})
    async with db() as session:
        retained = await session.get(Job, job_id)
    assert response.status_code == 409 and retained is not None, (
        f"reset returned {response.status_code} and erased protected {record_type} records"
    )
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["journal", "interrupted", "unfinished", "unprepared", "actions"])
async def test_reset_terminal_history(backup_env, record_type: str) -> None:
    from tests.test_staging import add_job

    _, config_path, db, _ = backup_env
    source = config_path.parent / "movie.mkv"
    source.write_bytes(b"original")
    job_id = await add_job(db, source, status="failed")
    async with db() as session:
        job = await session.get(Job, job_id)
        assert job is not None
        media_id, updated_at = job.media_file_id, job.updated_at
        attempt = JobAttempt(
            job_id=job_id,
            attempt_number=1,
            status="running" if record_type == "unfinished" else "failed",
            result="interrupted" if record_type == "interrupted" else "failed",
        )
        session.add(attempt)
        await session.flush()
        journal = FinalizationJournal(
            attempt_id=attempt.id,
            source_path=str(source),
            destination_path=str(source),
            partial_path=str(source) + ".partial",
            trash_path=str(source) + ".discard",
            state="discard_prepared" if record_type == "journal" else "rolled_back",
        )
        event = JobEvent(
            job_id=job_id,
            attempt_id=attempt.id,
            type="finalization_unprepared"
            if record_type == "unprepared"
            else "actions_pending"
            if record_type == "actions"
            else "failed",
        )
        session.add_all([journal, event])
        await session.commit()
        attempt_id, journal_id, event_id = attempt.id, journal.id, event.id
        protected = record_type in {"journal", "actions"}
        if protected:
            with pytest.raises(ValueError):
                await reset_data(session, set())
        else:
            await reset_data(session, set())
        await session.commit()
    async with db() as session:
        saved = await session.get(Job, job_id)
        if protected:
            assert saved is not None and saved.updated_at == updated_at
        else:
            assert saved is None
        assert (await session.get(MediaFile, media_id) is not None) == protected
        assert (await session.get(JobAttempt, attempt_id) is not None) == protected
        assert (await session.get(FinalizationJournal, journal_id) is not None) == protected
        assert (await session.get(JobEvent, event_id) is not None) == protected
    assert source.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_reset_rechecks_recovery(backup_env, monkeypatch) -> None:
    from tests.test_staging import add_job

    client, _, db, _ = backup_env
    job_id = await add_job(db, None, status="failed")
    reset = reset_data

    async def protect_then_reset(session, keep):
        async with db() as writer:
            attempt = JobAttempt(job_id=job_id, attempt_number=1, status="failed")
            writer.add(attempt)
            await writer.flush()
            writer.add(
                FinalizationJournal(
                    attempt_id=attempt.id,
                    source_path="source",
                    destination_path="destination",
                    partial_path="partial",
                    trash_path="trash",
                    state="prepared",
                )
            )
            await writer.commit()
        await reset(session, keep)

    monkeypatch.setattr("server.api.backups.reset_data", protect_then_reset)
    response = await asyncio.wait_for(client.post("/api/backups/reset", json={"keep": ["nodes", "plugins"]}), 3)
    assert response.status_code == 409
    async with db() as session:
        assert await session.get(Job, job_id) is not None
        assert (
            await session.scalar(select(FinalizationJournal.id).where(FinalizationJournal.state == "prepared"))
            is not None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_reset_waits_maintenance(backup_env, monkeypatch, failure: bool) -> None:
    import threading

    from server.workspaces import scan_disk

    client, config_path, _, app = backup_env
    root = config_path.parent / "workspace"
    (root / "jobs" / "999").mkdir(parents=True)
    (root / "jobs" / "999" / "source.mkv").write_bytes(b"retained source")
    app.state.workspaces.root = root
    inspecting, inspected, scanner_closed = (asyncio.Event() for _ in range(3))
    scan_release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_scan(folder, stopped):
        loop.call_soon_threadsafe(inspecting.set)
        assert scan_release.wait(5)
        result = scan_disk(folder, stopped)
        loop.call_soon_threadsafe(inspected.set)
        return result

    monkeypatch.setattr("server.workspaces.scan_disk", blocked_scan)
    for name in ("jobs", "scanner", "probes", "flow_enqueue", "healthchecks", "staging", "notifications"):
        service = getattr(app.state, name)
        close = "close" if name in {"jobs", "probes", "flow_enqueue", "healthchecks"} else "aclose"
        monkeypatch.setattr(service, close, AsyncMock())
        start = AsyncMock() if name in {"jobs", "probes", "flow_enqueue"} else lambda: None
        monkeypatch.setattr(service, "start", start)
    started, closing, release, completed, restarted = (asyncio.Event() for _ in range(5))
    deletion_closed = asyncio.Event()

    async def close_deletion() -> None:
        async with app.state.hub.dispatch_lock:
            deletion_closed.set()

    monkeypatch.setattr(app.state.library_deletion, "close", close_deletion)
    monkeypatch.setattr(app.state.library_deletion, "start", AsyncMock())
    maintenance = Scheduler()
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            restarted.set()
            return
        started.set()
        await release.wait()
        completed.set()

    async def reset(_session, _keep) -> None:
        assert completed.is_set()
        assert inspected.is_set()
        assert maintenance._task is None
        if failure:
            raise ValueError("reset failed")

    async def close_scanner() -> None:
        assert deletion_closed.is_set()
        assert completed.is_set()
        assert maintenance._task is None
        scanner_closed.set()

    monkeypatch.setattr("server.api.backups.reset_data", reset)
    monkeypatch.setattr(app.state.scanner, "aclose", close_scanner)
    close = maintenance.aclose

    async def close_maintenance() -> None:
        closing.set()
        await close()

    monkeypatch.setattr(maintenance, "aclose", close_maintenance)
    maintenance.add(cleanup)
    app.state.scheduler = maintenance
    app.state.services_running = True
    maintenance.start()
    task = None
    sampling = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        sampling = asyncio.create_task(client.get("/api/workspaces"))
        await asyncio.wait_for(inspecting.wait(), 1)
        task = asyncio.create_task(
            client.post(
                "/api/backups/reset", json={"keep": ["flows", "libraries", "nodes", "notifications", "plugins"]}
            )
        )
        await asyncio.wait_for(closing.wait(), 1)
        assert not task.done() and not completed.is_set()
        release.set()
        await asyncio.wait_for(scanner_closed.wait(), 1)
        assert not task.done() and not sampling.done()
        scan_release.set()
        listed = await asyncio.wait_for(sampling, 2)
        assert listed.json()["summary"]["unused_size_bytes"] == len(b"retained source")
        response = await asyncio.wait_for(task, 2)
        assert response.status_code == (409 if failure else 200)
        await asyncio.wait_for(restarted.wait(), 1)
        assert calls == 2
        (root / "jobs" / "999" / "source.mkv").write_bytes(b"changed source")
        refreshed = (await client.get("/api/workspaces")).json()
        assert refreshed["error"] is None
        assert refreshed["items"][0]["source_bytes"] == len(b"changed source")
    finally:
        release.set()
        scan_release.set()
        if task is not None:
            await asyncio.wait_for(task, 2)
        if sampling is not None:
            await asyncio.wait_for(sampling, 2)
        await asyncio.wait_for(maintenance.aclose(), 1)
        await asyncio.wait_for(app.state.workspaces.aclose(), 2)
        app.state.services_running = False


@pytest.mark.asyncio
async def test_restore_rejects_active(backup_env) -> None:
    client, config_path, db, _ = backup_env
    async with db() as session:
        library = await create_library(session, name="busy", roots=[str(config_path.parent)], watch=True)
        media = MediaFile(
            library_id=library.id,
            path=str(config_path.parent / "fixture.mkv"),
            size=1,
            mtime_ns=1,
            container="mkv",
            medium="video",
            last_scanned_at=datetime.now(UTC),
        )
        session.add(media)
        await session.flush()
        session.add(Job(library_id=library.id, media_file_id=media.id, status="queued"))
        await session.commit()
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={
            "password": "long-password",
            "selection": json.dumps({"config": False, "libraries": ["busy"], "replace": True}),
        },
    )
    assert response.status_code == 409
    async with db() as session:
        saved = await session.get(Library, library.id)
        assert saved is not None and saved.watch and not saved.suspended


@pytest.mark.asyncio
async def test_reset_gates_mutations(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, _, app = backup_env
    lock = app.state.reset_lock
    await lock.acquire()
    try:
        blocked = await client.post("/api/libraries", json={})
    finally:
        lock.release()
    assert blocked.status_code == 409
    normal = await client.post("/api/libraries", json={})
    assert normal.status_code != 409


@pytest.mark.asyncio
async def test_restored_activation(backup_env) -> None:
    client, _, db, _ = backup_env
    async with db() as session:
        library = await create_library(session, name="review", roots=["/restored"])
        await session.commit()
    path = f"/api/libraries/{library.id}"
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    await client.patch(path, json={"roots": ["/obsolete"]})
    stale = (await client.get(path)).json()
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={
            "password": "long-password",
            "selection": json.dumps({"config": False, "libraries": ["review"], "replace": True}),
        },
    )
    assert response.status_code == 200
    restored = (await client.get(path)).json()
    assert restored["suspended"] and restored["roots"] == ["/restored"]
    mixed = await client.patch(path, json={"suspended": False, "roots": stale["roots"]})
    assert mixed.status_code == 409
    assert (await client.get(path)).json() == restored
    edited = await client.patch(path, json={"cooldown_s": 10})
    assert edited.status_code == 200
    assert edited.json()["suspended"] and edited.json()["roots"] == ["/restored"]
    resumed = await client.patch(path, json={"suspended": False})
    assert resumed.status_code == 200 and not resumed.json()["suspended"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reset", "restore"])
async def test_mutation_invalidates_stats(backup_env, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    client, config_path, db, app = backup_env
    async with db() as session:
        library = await create_library(session, name="stats", roots=[str(config_path.parent)])
        session.add(
            MediaFile(
                library_id=library.id,
                path=str(config_path.parent / "stats.mkv"),
                size=5,
                container="mkv",
                medium="video",
                mtime_ns=1,
                health_status="pending",
                last_scanned_at=datetime(2026, 10, 9, tzinfo=UTC),
            )
        )
        await session.commit()
    await app.state.stats.refresh()
    baseline = (await client.get("/api/stats")).json()
    assert baseline["backlog"]["remaining_files"] == 1
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    assert exported.status_code == 200
    entered, release, request_lock, mutating, mutation_release, reading = (asyncio.Event() for _ in range(6))
    calls = 0

    class RequestLock(asyncio.Lock):
        async def acquire(self) -> Literal[True]:
            if self.locked():
                reading.set()
            result = await super().acquire()
            request_lock.set()
            return result

    app.state.reset_lock = RequestLock()

    async def slow(session, events, now):
        nonlocal calls
        calls += 1
        data = await build_snapshot(session, events, now)
        if calls == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        return data

    original = reset_data if operation == "reset" else restore_data

    async def mutate(session, selection, *args):
        mutating.set()
        await asyncio.wait_for(mutation_release.wait(), 3)
        return await original(session, selection, *args)

    monkeypatch.setattr("server.stats.build_snapshot", slow)
    monkeypatch.setattr(f"server.api.backups.{operation}_data", mutate)
    refresh = asyncio.create_task(app.state.stats.refresh())
    request = None
    reader = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if operation == "reset":
            request = asyncio.create_task(client.post("/api/backups/reset", json={"keep": ["nodes", "plugins"]}))
        else:
            request = asyncio.create_task(
                client.post(
                    "/api/backups/restore",
                    files={"file": ("backup.mwbackup", exported.content)},
                    data={
                        "password": "long-password",
                        "selection": json.dumps({"config": False, "libraries": ["stats"], "replace": True}),
                    },
                )
            )
        await asyncio.wait_for(request_lock.wait(), 1)
        assert not request.done() and not mutating.is_set()
        release.set()
        await asyncio.wait_for(refresh, 2)
        await asyncio.wait_for(mutating.wait(), 2)
        reader = asyncio.create_task(client.get("/api/stats"))
        await asyncio.wait_for(reading.wait(), 1)
        assert not reader.done()
        mutation_release.set()
        response = await asyncio.wait_for(request, 3)
        assert response.status_code == 200, response.text
        assert (await asyncio.wait_for(reader, 2)).status_code == 503
        assert mutating.is_set()
        for endpoint in ("/api/stats", "/api/stats/overview", "/api/stats/savings"):
            assert (await client.get(endpoint)).status_code == 503
        assert await app.state.stats.read() is None
        await app.state.stats.refresh()
        summary = (await client.get("/api/stats")).json()
        assert summary["backlog"]["remaining_files"] == 0
    finally:
        release.set()
        mutation_release.set()
        await asyncio.wait_for(
            asyncio.gather(
                refresh,
                *([request] if request is not None else []),
                *([reader] if reader is not None else []),
                return_exceptions=True,
            ),
            3,
        )


@pytest.mark.asyncio
async def test_backup_selective_restore(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, config_path, db, app = backup_env
    await app.state.settings.update({"max_probes": 2, "scanner": {"ignore_dirs": ["Extras"]}})
    await app.state.store.put("activity_paused", True)
    original = FlowDefinition.model_validate(graph_flow(("transcode.video", {"quality": 21})))
    changed = FlowDefinition.model_validate(graph_flow(("transcode.video", {"quality": 27})))
    async with db() as session:
        flow = await save_flow(session, "Films", original)
        await create_library(session, name="Movies", roots=["/media/films"], flow_id=flow.id)
        await session.commit()

    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    assert exported.status_code == 200
    assert exported.content.startswith(b"MWBK")
    assert b"secret-from-file" not in exported.content
    payload = json.loads(decrypt(exported.content, "long-password"))
    assert payload["settings"]["max_probes"] == 2
    assert "activity_paused" not in payload["settings"]
    assert 'secret = "secret-from-file"' in payload["config"]
    assert 'client_id = "millwright"' in payload["config"]
    assert "# My server settings" in payload["config"]
    assert 'api_key = "api-secret-from-file"' in payload["config"]
    assert 'node_enrollment_token = "node-private-token"' in payload["config"]
    preview = await client.post(
        "/api/backups/preview",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password"},
    )
    assert preview.status_code == 200
    assert preview.json()["flows"] == ["Films"]
    assert preview.json()["settings"] is True
    assert preview.json()["libraries"] == [{"name": "Movies", "roots": ["/media/films"], "flow_name": "Films"}]
    assert "secret-from-file" not in preview.text

    async with db() as session:
        await save_flow(session, "Films", changed, expected_version=1)
        library = (await session.scalars(select(Library))).one()
        library.roots = ["/media/other"]
        await session.commit()
    config_path.write_text("", encoding="utf-8")
    await app.state.settings.update({"max_probes": 3})
    invalid = {**payload, "settings": {**payload["settings"], "max_probes": 0}}
    rejected = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", encrypt(json.dumps(invalid).encode(), "long-password"))},
        data={
            "password": "long-password",
            "selection": json.dumps({"config": False, "settings": True, "flows": ["Films"], "replace": True}),
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert (await client.get("/api/settings")).json()["max_probes"] == 3
    async with db() as session:
        unchanged = (await session.scalars(select(Flow))).one()
        assert FlowDefinition.model_validate_json(unchanged.definition) == changed
    selection = {
        "flows": ["Films"],
        "libraries": [],
        "notifications": [],
        "plugins": [],
        "config": True,
        "replace": True,
        "roots": {},
    }
    restored = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["restart_required"] is True
    assert (await client.get("/api/settings")).json()["max_probes"] == 3
    async with db() as session:
        flow = (await session.scalars(select(Flow))).one()
        library = (await session.scalars(select(Library))).one()
        assert FlowDefinition.model_validate_json(flow.definition) == original
        assert library.roots == ["/media/other"]
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["auth"]["oidc"]["secret"].startswith("file:secrets/")
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["auth"]["oidc"]["client_id"].startswith(
        "file:secrets/"
    )
    restored_config = load_config(config_path)
    assert restored_config.api_key == "api-secret-from-file"
    assert restored_config.node_enrollment_token == "node-private-token"
    assert restored_config.auth.oidc is not None and restored_config.auth.oidc.secret == "secret-from-file"
    assert config_path.stat().st_mode & 0o077 == 0
    restored = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps({"config": False, "settings": True})},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["restart_required"] is False
    assert (await client.get("/api/settings")).json() == payload["settings"]
    assert app.state.scanner.scanner.ignore_dirs == ["Extras"]
    response = await client.post("/api/backups/reset", json={"keep": ["settings"]})
    assert response.status_code == 200, response.text
    assert (await client.get("/api/settings")).json() == payload["settings"]
    assert await app.state.store.get("activity_paused") is True


@pytest.mark.asyncio
async def test_backup_restores_nodes(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, db, _ = backup_env
    async with db() as session:
        session.add(
            NodeRow(name="worker-a", display_name="worker-a", token_hash=hash_token("original"), state="offline")
        )
        await session.commit()
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    preview = await client.post(
        "/api/backups/preview",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password"},
    )
    assert preview.json()["nodes"] == [{"label": "worker-a", "value": "worker-a"}]
    assert hash_token("original") not in preview.text
    async with db() as session:
        node = (await session.scalars(select(NodeRow))).one()
        node.token_hash = hash_token("changed")
        await session.commit()
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={
            "password": "long-password",
            "selection": json.dumps({"nodes": ["worker-a"], "config": False, "replace": True}),
        },
    )
    assert response.status_code == 200, response.text
    async with db() as session:
        assert await authenticate(session, "worker-a", "original") is not None
        assert await authenticate(session, "worker-a", "changed") is None


@pytest.mark.asyncio
async def test_restores_webhooks(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, _, _ = backup_env
    body = {"type": "sonarr", "path_replacements": [{"source": "/remote", "destination": "/media"}]}
    created = await client.put("/api/webhooks/TV", json=body)
    assert created.status_code == 200
    key = created.json()["api_key"]
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    preview = await client.post(
        "/api/backups/preview",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password"},
    )
    assert preview.json()["webhooks"] == ["TV"]
    assert (await client.post("/api/backups/reset", json={"keep": ["webhooks"]})).status_code == 200
    assert (await client.get("/api/webhooks")).json()[0]["api_key"] == key
    assert (await client.post("/api/backups/reset", json={"keep": []})).status_code == 200
    assert (
        await client.post(
            "/api/webhooks/receive", headers={"Authorization": f"Bearer {key}"}, json={"eventType": "Test"}
        )
    ).status_code == 401
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps({"webhooks": ["TV"], "config": False})},
    )
    assert response.status_code == 200, response.text
    restored = (await client.get("/api/webhooks")).json()[0]
    assert {key: restored[key] for key in body} == body
    assert restored["api_key"] == key
    assert (
        await client.post(
            "/api/webhooks/receive", headers={"Authorization": f"Bearer {key}"}, json={"eventType": "Test"}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_backup_wrong_password(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, _, _ = backup_env
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    response = await client.post(
        "/api/backups/preview",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "wrong-password"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_backup_requires_login(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, _, _ = backup_env
    response = await client.post("/api/backups/export", json={"password": "long-password"}, headers={"Cookie": ""})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_backup_conflict(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, db, _ = backup_env
    async with db() as session:
        await save_flow(
            session, "Films", FlowDefinition.model_validate(graph_flow(("transcode.video", {"quality": 21})))
        )
        await session.commit()
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    selection = {
        "flows": ["Films"],
        "libraries": [],
        "notifications": [],
        "plugins": [],
        "config": False,
        "replace": False,
    }
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert response.status_code == 409

    selection["flows"] = []
    selection["config"] = True
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_library_requires_flow(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, _, db, _ = backup_env
    async with db() as session:
        flow = await save_flow(session, "Films", FlowDefinition.model_validate(graph_flow("transcode.video")))
        await create_library(session, name="Movies", roots=["/media/films"], flow_id=flow.id)
        await session.commit()
    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    selection = {"flows": [], "libraries": ["Movies"], "config": False, "replace": True}
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert response.status_code == 422

    selection["flows"] = ["Films"]
    response = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert response.status_code == 200, response.text
    async with db() as session:
        library = (await session.scalars(select(Library))).one()
        assert library.flow_id == flow.id
        assert library.watch is False


def test_rejects_escape() -> None:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(
            "plugin.toml",
            'id = "action.example"\nkind = "action"\nname = "Example"\ndescription = "Example"\nversion = "1"\nauthor = "Test"\nurl = "https://example.com/plugins"\n',
        )
        archive.writestr("../outside.py", "print('escaped')")
    encoded = base64.b64encode(content.getvalue()).decode("ascii")
    with pytest.raises(ValueError):
        plugin_archive("action.example", encoded, bundled_taken=lambda _: False)


@pytest.mark.asyncio
async def test_backup_restores_plugin(
    backup_env: tuple[httpx.AsyncClient, Path, async_sessionmaker[AsyncSession], FastAPI],
) -> None:
    client, config_path, db, _ = backup_env
    root = config_path.parent / "plugins"
    plugin = root / "action.example"
    plugin.mkdir()
    (plugin / "plugin.toml").write_text(
        'id = "action.example"\nkind = "action"\nname = "Example"\ndescription = "Example plugin"\nversion = "1"\nauthor = "Test"\nurl = "https://example.com/plugins"\n',
        encoding="utf-8",
    )
    (plugin / "plugin.py").write_text(
        "def decide(ctx, inputs):\n    return {'type': 'skip', 'reason': 'test'}\n", encoding="utf-8"
    )

    exported = await client.post("/api/backups/export", json={"password": "long-password"})
    assert exported.status_code == 200
    payload = json.loads(decrypt(exported.content, "long-password"))
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["uploaded_plugins"]["action.example"]))) as archive:
        assert set(archive.namelist()) == {"plugin.toml", "plugin.py"}
    preview = await client.post(
        "/api/backups/preview",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password"},
    )
    assert preview.json()["plugins"] == ["action.example"]
    (plugin / "plugin.toml").unlink()
    (plugin / "plugin.py").unlink()
    plugin.rmdir()

    selection = {
        "flows": [],
        "libraries": [],
        "notifications": [],
        "plugins": ["action.example"],
        "config": False,
        "replace": False,
        "roots": {},
    }
    restored = await client.post(
        "/api/backups/restore",
        files={"file": ("backup.mwbackup", exported.content)},
        data={"password": "long-password", "selection": json.dumps(selection)},
    )
    assert restored.status_code == 200, restored.text
    assert (plugin / "plugin.py").is_file()
    async with db() as session:
        state = await session.scalar(select(PluginState).where(PluginState.plugin_id == "action.example"))
        assert state is not None and state.enabled is False
