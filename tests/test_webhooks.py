from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
from base64 import b64encode
from pathlib import Path

import httpx
import pytest
from common.enums import WebhookType
from tests.helpers import create_test_app as create_app

from server.core.config import ServerConfig
from server.models import Library, MediaFile
from server.webhooks import PathReplacement, WebhookBody, convert_path, event_paths
from sqlalchemy import select
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_auth_isolation(tmp_path: Path) -> None:
    async with database_context("webhook-auth") as factory:
        app = create_app(
            factory,
            config=ServerConfig(
                api_key="instance-key",
                workspace_dir=str(tmp_path / "workspace"),
                trash_dir=str(tmp_path / "trash"),
            ),
        )
        async with factory() as session:
            _, token = await create_user_session(session, "operator", 1)
            await session.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/webhooks")).status_code == 401
            created = await client.put(
                "/api/webhooks/Sonarr", headers={"Authorization": "Bearer instance-key"}, json={"type": "sonarr"}
            )
            assert created.status_code == 200
            key = created.json()["api_key"]
            endpoint = "/api/webhooks/receive"
            basic = b64encode(f"anything:{key}".encode()).decode()
            for headers, params in [
                ({}, {}),
                ({"Authorization": "Bearer instance-key"}, {}),
                ({"Authorization": f"Bearer {token}"}, {}),
                ({}, {"api_key": "instance-key"}),
                ({"Authorization": "Basic !!!"}, {}),
                ({"Authorization": f"Basic {b64encode(b'missing-colon').decode()}"}, {}),
                ({"Authorization": f"Basic {b64encode(b'user:wrong').decode()}"}, {"api_key": key}),
                ({"Authorization": "Bearer instance-key"}, {"api_key": key}),
                ({"Authorization": "Digest unsupported"}, {"api_key": key}),
            ]:
                response = await client.post(endpoint, headers=headers, params=params, json={"eventType": "Test"})
                assert response.status_code == 401
            credentials = [
                ({}, {"api_key": key}),
                ({"Authorization": f"Bearer {key}"}, {}),
                ({"Authorization": f"Basic {basic}"}, {}),
            ]
            for headers, params in credentials:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    params=params,
                    json={"eventType": "Test", "series": {"path": "C:\\testpath"}},
                )
                assert response.status_code == 200 and response.json() == {"status": "test"}
            assert not app.state.scanner._scan_tasks
            ignored = await client.post(
                endpoint, params={"api_key": key}, json={"eventType": "Grab", "series": {"path": "/downloads"}}
            )
            assert ignored.status_code == 200 and ignored.json() == {"status": "ignored"}
            malformed = await client.post(
                endpoint,
                params={"api_key": key},
                json={"eventType": "Download", "episodeFile": {"sourcePath": "/downloads/file.mkv"}},
            )
            assert malformed.status_code == 422
            malformed_event = await client.post(endpoint, params={"api_key": key}, json={"eventType": []})
            assert malformed_event.status_code == 422
            assert not app.state.scanner._scan_tasks
            assert (await client.get("/api/webhooks", headers={"Authorization": f"Bearer {key}"})).status_code == 401
            headers = {"Authorization": "Bearer instance-key"}
            listed = await client.get("/api/webhooks", headers=headers)
            assert listed.json()[0]["api_key"] == key
            updated = await client.put("/api/webhooks/Sonarr", headers=headers, json={"type": "radarr"})
            assert updated.json()["api_key"] == key
            assert (await client.delete("/api/webhooks/Sonarr", headers=headers)).status_code == 204
            for headers, params in credentials:
                response = await client.post(endpoint, headers=headers, params=params, json={"eventType": "Test"})
                assert response.status_code == 401


def test_prefix_conversion() -> None:
    replacements = WebhookBody(
        type=WebhookType.GENERIC,
        path_replacements=[
            PathReplacement(source="/remote", destination="/server"),
            PathReplacement(source="/remote/tv/", destination="/library/shows"),
            PathReplacement(source="/library", destination="/wrong"),
            PathReplacement(source="D:\\Media", destination="/windows"),
        ],
    ).path_replacements
    assert convert_path("/remote/tv/Show/file.mkv", replacements) == Path("/library/shows/Show/file.mkv")
    assert convert_path("/remote/tv", replacements) == Path("/library/shows")
    assert convert_path("/remote/tv-more/file.mkv", replacements) == Path("/server/tv-more/file.mkv")
    assert convert_path("/remotely/file.mkv", replacements) == Path("/remotely/file.mkv")
    assert convert_path("D:\\Media\\Show\\file.mkv", replacements) == Path("/windows/Show/file.mkv")
    with pytest.raises(ValueError):
        convert_path("/remote/tv/../../../escape.mkv", replacements)
    with pytest.raises(ValueError):
        WebhookBody(
            type=WebhookType.GENERIC,
            path_replacements=[
                PathReplacement(source="/remote/", destination="/one"),
                PathReplacement(source="/remote", destination="/two"),
            ],
        )


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        (
            WebhookType.SONARR,
            {
                "eventType": "Download",
                "episodeFile": {"path": "/tv/new.mkv", "sourcePath": "/downloads/input.mkv"},
                "deletedFiles": [{"path": "/tv/old.mkv", "recycleBinPath": "/trash/old.mkv"}],
            },
            {"/tv/new.mkv", "/tv/old.mkv"},
        ),
        (
            WebhookType.SONARR,
            {
                "eventType": "Download",
                "episodeFiles": [{"path": "/tv/one.mkv"}, {"path": "/tv/two.mkv"}],
                "sourcePath": "/downloads",
                "destinationPath": "/tv",
            },
            {"/tv/one.mkv", "/tv/two.mkv"},
        ),
        (
            WebhookType.RADARR,
            {
                "eventType": "Download",
                "movieFile": {"path": "/movies/new.mkv", "sourcePath": "/downloads/input.mkv"},
                "deletedFiles": [{"path": "/movies/old.mkv"}],
            },
            {"/movies/new.mkv", "/movies/old.mkv"},
        ),
        (
            WebhookType.RADARR,
            {"eventType": "MovieDelete", "movie": {"folderPath": "/movies/Deleted"}, "deletedFiles": True},
            {"/movies/Deleted"},
        ),
        (
            WebhookType.SONARR,
            {"eventType": "SeriesDelete", "series": {"path": "/tv/Deleted"}, "deletedFiles": False},
            {"/tv/Deleted"},
        ),
        (
            WebhookType.SONARR,
            {"eventType": "EpisodeFileDelete", "episodeFile": {"path": "/tv/file.mkv"}},
            {"/tv/file.mkv"},
        ),
        (
            WebhookType.RADARR,
            {"eventType": "MovieFileDelete", "movieFile": {"path": "/movies/file.mkv"}},
            {"/movies/file.mkv"},
        ),
        (
            WebhookType.SONARR,
            {"eventType": "Rename", "renamedEpisodeFiles": [{"path": "/tv/new.mkv", "previousPath": "/tv/old.mkv"}]},
            {"/tv/new.mkv", "/tv/old.mkv"},
        ),
        (
            WebhookType.RADARR,
            {
                "eventType": "Rename",
                "renamedMovieFiles": [{"path": "/movies/new.mkv", "previousPath": "/movies/old.mkv"}],
            },
            {"/movies/new.mkv", "/movies/old.mkv"},
        ),
        (WebhookType.RADARR, {"eventType": "Grab", "movie": {"folderPath": "/movies/not-imported"}}, None),
    ],
)
def test_native_paths(kind: WebhookType, payload: dict, expected: set[str] | None) -> None:
    assert event_paths(kind, payload) == expected


@pytest.mark.asyncio
@pytest.mark.usefixtures("without_fd")
async def test_scoped_discovery(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    folder = root / "Show"
    folder.mkdir()
    media = folder / "episode.mkv"
    media.write_bytes(b"first")
    sibling = root / "unrelated.mkv"
    sibling.write_bytes(b"untouched")
    (folder / "ignored.txt").write_bytes(b"unsupported")
    async with database_context("webhook-scan") as factory:
        app = create_app(
            factory, config=ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(tmp_path / "trash"))
        )
        async with factory() as session:
            library = Library(name="TV", roots=[str(root)])
            session.add(library)
            await session.commit()
        scanner = app.state.scanner
        scanner.start()
        await asyncio.wait_for(scanner._startup_task, 3)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.put(
                    "/api/webhooks/TV",
                    headers={"Authorization": "Bearer test-key"},
                    json={"type": "generic", "path_replacements": [{"source": "/remote", "destination": str(root)}]},
                )
                key = response.json()["api_key"]

                async def send(path: str | list[str], *, payload: dict | None = None):
                    response = await client.post(
                        "/api/webhooks/receive",
                        params={"api_key": key},
                        json=payload if payload is not None else {"path": path},
                    )
                    assert response.status_code == 202, response.text
                    tasks = list(scanner._scan_tasks.values())
                    reports = await asyncio.wait_for(asyncio.gather(*tasks), 5)
                    assert len(reports) == 1 and reports[0] is not None
                    return reports[0]

                report = await send("/remote/Show")
                assert report.added == 1 and report.found == 1
                async with factory() as session:
                    row = (await session.scalars(select(MediaFile))).one()
                    assert row.path == str(media)
                    row.transcode_status = "not_needed"
                    row.transcode_decided = True
                    await session.commit()
                    identifier, timestamp = row.id, row.updated_at
                report = await send("/remote/Show/episode.mkv")
                assert report.unchanged == 1 and report.updated == 0
                async with factory() as session:
                    row = await session.get(MediaFile, identifier)
                    assert row is not None
                    assert (
                        row.updated_at == timestamp and row.transcode_decided and row.transcode_status == "not_needed"
                    )
                media.write_bytes(b"changed source")
                batch = root / "another.mkv"
                batch.write_bytes(b"new file")
                for invalid in (
                    [],
                    ["/remote/Show/episode.mkv", None],
                    ["/remote/Show/episode.mkv", ""],
                    ["/remote/Show/episode.mkv", str(tmp_path / "outside.mkv")],
                ):
                    response = await client.post(
                        "/api/webhooks/receive", params={"api_key": key}, json={"path": invalid}
                    )
                    assert response.status_code == 422
                    assert not scanner._scan_tasks
                report = await send(["/remote/Show/episode.mkv", "/remote/another.mkv"])
                assert report.updated == 1 and report.added == 1
                async with factory() as session:
                    paths = set(await session.scalars(select(MediaFile.path)))
                    assert paths == {str(media), str(batch)}
                    row = await session.get(MediaFile, identifier)
                    assert row is not None
                    assert not row.transcode_decided and row.transcode_status == "pending"
                    assert row.size == len(b"changed source")
                native = await client.put(
                    "/api/webhooks/TV",
                    headers={"Authorization": "Bearer test-key"},
                    json={"type": "sonarr", "path_replacements": [{"source": "/remote", "destination": str(root)}]},
                )
                assert native.json()["api_key"] == key
                report = await send(
                    "/remote/Show/episode.mkv",
                    payload={"eventType": "EpisodeFileDelete", "episodeFile": {"path": "/remote/Show/episode.mkv"}},
                )
                assert report.unchanged == 1 and report.pruned == 0
                assert media.read_bytes() == b"changed source"
                # Storage loss must not turn a missing webhook path into a deletion record.
                moved = tmp_path / "offline"
                root.rename(moved)
                deletion = {"eventType": "SeriesDelete", "series": {"path": "/remote/Show"}, "deletedFiles": True}
                report = await send("/remote/Show", payload=deletion)
                assert report.errors and report.pruned == 0
                async with factory() as session:
                    assert await session.get(MediaFile, identifier) is not None
                moved.rename(root)
                media.unlink()
                (folder / "ignored.txt").unlink()
                folder.rmdir()
                report = await send("/remote/Show", payload=deletion)
                assert report.pruned == 1
                async with factory() as session:
                    assert await session.get(MediaFile, identifier) is None
                assert sibling.read_bytes() == b"untouched"

                for path in (str(tmp_path / "outside.mkv"), "/remote/../outside.mkv", "/remotely/Show"):
                    response = await client.post(
                        "/api/webhooks/receive",
                        params={"api_key": key},
                        json={"eventType": "EpisodeFileDelete", "episodeFile": {"path": path}},
                    )
                    assert response.status_code == 422
                assert not scanner._scan_tasks
        finally:
            await asyncio.wait_for(scanner.aclose(), 5)
            await app.state.events.aclose()


@pytest.mark.asyncio
async def test_receipt_guards(tmp_path: Path) -> None:
    async with database_context("webhook-guards") as factory:
        app = create_app(
            factory, config=ServerConfig(workspace_dir=str(tmp_path / "workspace"), trash_dir=str(tmp_path / "trash"))
        )
        async with factory() as session:
            library = Library(name="media", roots=[str(tmp_path)], suspended=True)
            session.add(library)
            await session.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            key = (
                await client.put(
                    "/api/webhooks/local", headers={"Authorization": "Bearer test-key"}, json={"type": "generic"}
                )
            ).json()["api_key"]

            async def send():
                return await client.post(
                    "/api/webhooks/receive", params={"api_key": key}, json={"path": str(tmp_path / "file.mkv")}
                )

            assert (await send()).status_code == 409
            async with factory() as session:
                row = await session.get(Library, library.id)
                assert row is not None
                row.suspended = False
                await session.commit()
            assert (await send()).status_code == 503
            app.state.scanner.set_paused(paused=True)
            assert (await send()).status_code == 409
            app.state.recovering = True
            assert (await send()).status_code == 503
            app.state.recovering = False
            async with app.state.reset_lock:
                assert (await send()).status_code == 409
            assert not app.state.scanner._scan_tasks
