from __future__ import annotations

import asyncio
import json
from pathlib import PurePath

import httpx
import pytest
from common.actions import ExternalNotificationConfig
from common.plugin import build_inputs
from sqlalchemy import select

from server.external_notifications import notify_external
from server.models import Job, JobEvent, NotificationDelivery
from server.notifications import NotificationService
from tests.helpers import database_context
from tests.test_notification_delivery import make_job


def notification(application: str = "radarr", *, unmonitor: bool = True) -> dict:
    return {
        "action": {
            "kind": "notify_external",
            "application": application,
            "url": "https://manager.test/base",
            "api_key": "private-api-key",
            "unmonitor_after_refresh": unmonitor,
        },
        "path": "/library/Series/Series.S01E01E02.mp4",
        "original_filename": "Series.S01E01E02.Original.mkv",
    }


@pytest.mark.parametrize(
    "options, error",
    [
        ({"application": "plex", "unmonitor_after_refresh": True}, "Unmonitor after refresh"),
        ({"application": "plex", "path_mappings": [{"destination": "/media"}]}, "source"),
        ({"application": "plex", "path_mappings": [{"source": "/library"}]}, "destination"),
        (
            {"application": "plex", "path_mappings": [{"source": "library", "destination": "/media"}]},
            "Use absolute paths",
        ),
        (
            {"application": "plex", "path_mappings": [{"source": "/library", "destination": "media"}]},
            "Use absolute paths",
        ),
        (
            {"application": "plex", "path_mappings": [{"source": "/library", "destination": "/media/../other"}]},
            "can't contain",
        ),
        (
            {
                "application": "plex",
                "path_mappings": [
                    {"source": "/library", "destination": "/media"},
                    {"source": "/library/", "destination": "/other"},
                ],
            },
            "appear only once",
        ),
    ],
)
def test_external_options_rejected(options, error) -> None:
    with pytest.raises(ValueError, match=error):
        build_inputs(ExternalNotificationConfig, {"url": "https://server.test", "api_key": "key", **options})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mappings, root, folder, prefix",
    [
        ([], "/library", "/library/Series", "/library/Ser"),
        (
            [
                {"source": "/library/Movies", "destination": "/movies"},
                {"source": "/library/", "destination": "/media/"},
            ],
            "/media",
            "/media/Series",
            "/media/Ser",
        ),
        (
            [
                {"source": "/library", "destination": "/wrong"},
                {"source": "/library/Series", "destination": "/media/Series"},
            ],
            "/media",
            "/media/Series",
            "/media/Ser",
        ),
        (
            [
                {"source": "/library/Series", "destination": "/media/Series"},
                {"source": "/library", "destination": "/wrong"},
            ],
            "/media",
            "/media/Series",
            "/media/Ser",
        ),
        (
            [{"source": "/library", "destination": "D:\\Media"}],
            "d:\\media",
            "D:\\Media\\Series",
            "D:\\Media\\Ser",
        ),
    ],
)
async def test_plex_folder_refresh(monkeypatch, mappings, root, folder, prefix) -> None:
    payload = notification("plex", unmonitor=False)
    payload["action"]["path_mappings"] = mappings
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["X-Plex-Token"] == "private-api-key"
        assert request.headers["Accept"] == "application/json"
        if request.url.path == "/base/library/sections":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {"key": "7", "Location": [{"path": root}, {"path": folder}]},
                            {"key": "8", "Location": [{"path": root}]},
                            {"key": "9", "Location": [{"path": prefix}]},
                        ]
                    }
                },
            )
        assert request.method == "GET"
        assert dict(request.url.params) == {"path": folder}
        return httpx.Response(200)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.plex.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert await notify_external(payload)
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/base/library/sections"),
        ("GET", "/base/library/sections/7/refresh"),
        ("GET", "/base/library/sections/8/refresh"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["mapping", "library"])
async def test_plex_path_mismatch(monkeypatch, mismatch) -> None:
    payload = notification("plex", unmonitor=False)
    payload["path"] = "/library/SeriesOther/Episode.mp4"
    if mismatch == "mapping":
        payload["action"]["path_mappings"] = [
            {"source": "/library/Series", "destination": "/media/Series"},
            {"source": "/library/Movies", "destination": "/media/Movies"},
        ]
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/base/library/sections"
        return httpx.Response(
            200,
            json={"MediaContainer": {"Directory": [{"key": "7", "Location": [{"path": "/library/Series"}]}]}},
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.plex.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ValueError, match="No path mapping matches" if mismatch == "mapping" else "No Plex library"):
        await notify_external(payload)
    assert len(requests) == (0 if mismatch == "mapping" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "application, path, folder",
    [
        ("jellyfin", "/library/Series & Friends/Season 1/Episode.mp4", "/media/Series & Friends/Season 1"),
        ("emby", "/library/Movies/A Movie/Movie.mp4", "/movies/A Movie"),
    ],
)
async def test_media_folder_update(monkeypatch, application, path, folder) -> None:
    payload = notification(application, unmonitor=False)
    payload["path"] = path
    payload["action"]["path_mappings"] = [
        {"source": "/library/Movies", "destination": "/movies"},
        {"source": "/library", "destination": "/media"},
    ]
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/base/Library/Media/Updated"
        assert not request.url.params
        if application == "jellyfin":
            assert request.headers["Authorization"] == 'MediaBrowser Token="private-api-key"'
        else:
            assert request.headers["X-Emby-Token"] == "private-api-key"
        assert json.loads(request.content) == {"Updates": [{"Path": folder, "UpdateType": "Modified"}]}
        return httpx.Response(204 if application == "jellyfin" else 200)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.media_server.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    assert await notify_external(payload)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_plex_retry_persisted(monkeypatch, caplog) -> None:
    refreshes = []

    def respond(request):
        if request.url.path == "/base/library/sections":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [{"key": key, "Location": [{"path": "/library"}]} for key in ["7", "8"]]
                    }
                },
            )
        refreshes.append(request.url.path)
        if len(refreshes) == 2:
            raise httpx.ConnectError("connection refused private-api-key", request=request)
        return httpx.Response(200)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.plex.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    async with database_context("plex-retry") as factory:
        async with factory() as session:
            job_id = await make_job(session, notify=False)
            job = await session.get(Job, job_id)
            assert job is not None
            job.status = "succeeded"
            event = JobEvent(job_id=job_id, type="external_notification", payload=notification("plex", unmonitor=False))
            session.add(event)
            await session.flush()
            session.add(NotificationDelivery(job_id=job_id, job_event_id=event.id, target_name="external:plex"))
            await session.commit()
        await NotificationService(factory).deliver_pending()
        async with factory() as session:
            delivery = (await session.scalars(select(NotificationDelivery))).one()
            assert (delivery.status, delivery.attempts) == ("pending", 1)
            assert delivery.error and "connection refused" in delivery.error
            assert "private-api-key" not in delivery.error
        await NotificationService(factory).deliver_pending()
        async with factory() as session:
            delivery = (await session.scalars(select(NotificationDelivery))).one()
            assert (delivery.status, delivery.attempts, delivery.error) == ("sent", 2, None)
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "succeeded"
    assert refreshes == [
        "/base/library/sections/7/refresh",
        "/base/library/sections/8/refresh",
        "/base/library/sections/7/refresh",
        "/base/library/sections/8/refresh",
    ]
    assert all("private-api-key" not in str(record.__dict__) for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, "tmdb-42", "imdb-tt123"])
async def test_radarr_refresh_order(monkeypatch, identifier) -> None:
    payload = notification()
    if identifier:
        payload["path"] = f"/library/Movie {{{identifier}}}/Movie.mp4"
    payload["action"]["path_mappings"] = [
        {"source": str(PurePath(payload["path"]).parent), "destination": "/radarr/Movie"}
    ]
    requests = []
    movie = {"id": 7, "tmdbId": 42, "imdbId": "tt123"}

    def respond(request):
        requests.append(request)
        assert request.headers["X-Api-Key"] == "private-api-key"
        path = request.url.path
        if path.endswith("/parse"):
            assert request.url.params["title"] == payload["original_filename"]
            return httpx.Response(200, json={"movie": movie})
        if path.endswith("/movie"):
            assert dict(request.url.params) == ({"tmdbId": "42"} if identifier == "tmdb-42" else {})
            movies = [{"id": 8, "tmdbId": 43, "imdbId": "tt999"}, movie] if identifier == "imdb-tt123" else [movie]
            return httpx.Response(200, json=movies)
        if request.method == "POST":
            assert json.loads(request.content) == {"name": "RefreshMovie", "movieIds": [7]}
            return httpx.Response(201, json={"id": 23, "status": "started"})
        if path.endswith("/command/23"):
            return httpx.Response(200, json={"id": 23, "status": "completed"})
        assert json.loads(request.content) == {"movieIds": [7], "monitored": False}
        return httpx.Response(202, json=[])

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.arr.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def tick(_seconds):
        return None

    monkeypatch.setattr("server.external_notifications.arr.asyncio.sleep", tick)
    assert await notify_external(payload)
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/base/api/v3/movie" if identifier else "/base/api/v3/parse"),
        ("POST", "/base/api/v3/command"),
        ("GET", "/base/api/v3/command/23"),
        ("PUT", "/base/api/v3/movie/editor"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "lookup", "refresh", "unmonitor"])
async def test_sonarr_delivery_persisted(monkeypatch, caplog, failure) -> None:
    payload = notification("sonarr")
    payload["action"]["path_mappings"] = [
        {"source": "/other", "destination": "/unused"},
        {"source": "/library", "destination": "/sonarr"},
    ]
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/parse"):
            assert dict(request.url.params) == {
                "title": payload["original_filename"],
                "path": "/sonarr/Series/Series.S01E01E02.mp4",
            }
            return httpx.Response(
                200,
                json={
                    "series": {"id": 10},
                    "episodes": [] if failure == "lookup" else [{"id": 11, "seriesId": 10}, {"id": 12, "seriesId": 10}],
                },
            )
        if request.method == "POST":
            assert json.loads(request.content) == {"name": "RefreshSeries", "seriesIds": [10]}
            return httpx.Response(201, json={"id": 23, "status": "failed" if failure == "refresh" else "completed"})
        if request.url.path.endswith("/command/23"):
            return httpx.Response(200, json={"id": 23, "status": "failed" if failure == "refresh" else "completed"})
        assert request.url.path == "/base/api/v3/episode/monitor"
        assert json.loads(request.content) == {"episodeIds": [11, 12], "monitored": False}
        if failure == "unmonitor":
            raise httpx.ConnectError("monitor refused private-api-key", request=request)
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.arr.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    async with database_context("external-delivery") as factory:
        async with factory() as session:
            job_id = await make_job(session, notify=False)
            job = await session.get(Job, job_id)
            assert job is not None
            job.status = "succeeded"
            event = JobEvent(job_id=job_id, type="external_notification", payload=payload)
            session.add(event)
            await session.flush()
            session.add(NotificationDelivery(job_id=job_id, job_event_id=event.id, target_name="external:sonarr"))
            await session.commit()
        for _ in range(3 if failure else 1):
            # Recreate the worker to prove retry context is durable.
            await NotificationService(factory).deliver_pending()
        async with factory() as session:
            delivery = (await session.scalars(select(NotificationDelivery))).one()
            assert delivery.status == ("failed" if failure else "sent")
            assert delivery.attempts == (3 if failure else 1)
            assert (delivery.error is not None) is bool(failure)
            assert "private-api-key" not in (delivery.error or "")
            event = await session.get(JobEvent, delivery.job_event_id)
            assert event is not None and event.payload["path"] == "/library/Series/Series.S01E01E02.mp4"
            job = await session.get(Job, job_id)
            assert job is not None and job.status == "succeeded"
    if failure in {"lookup", "refresh"}:
        assert not any(request.method == "PUT" for request in requests)
    assert sum(request.method == "POST" for request in requests) == (0 if failure == "lookup" else 1)
    assert all("private-api-key" not in str(record.__dict__) for record in caplog.records)


@pytest.mark.asyncio
async def test_optional_monitoring_unchanged(monkeypatch) -> None:
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"movie": {"id": 7}})
        assert request.method == "POST"
        return httpx.Response(201, json={"id": 23, "status": "completed"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.arr.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    payload = notification(unmonitor=False)
    del payload["action"]["unmonitor_after_refresh"]
    assert await notify_external(payload)
    assert calls == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_delayed_refresh_resumes(monkeypatch, interruption) -> None:
    polling = asyncio.Event()
    retry = False
    updates = []
    commands = []

    async def respond(request):
        if request.url.path.endswith("/parse"):
            return httpx.Response(200, json={"movie": {"id": 7}})
        if request.method == "POST":
            commands.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 23, "status": "started"})
        if request.method == "PUT":
            updates.append(json.loads(request.content))
            return httpx.Response(200, json=[])
        if not retry:
            polling.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"id": 23, "status": "completed"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "server.external_notifications.arr.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def tick(_seconds):
        return None

    monkeypatch.setattr("server.external_notifications.arr.asyncio.sleep", tick)
    if interruption == "timeout":
        wait_for = asyncio.wait_for

        async def deadline(awaitable, timeout):
            if timeout == 60 and not retry:
                child = asyncio.ensure_future(awaitable)
                try:
                    await wait_for(polling.wait(), 3)
                finally:
                    child.cancel()
                    await wait_for(asyncio.gather(child, return_exceptions=True), 3)
                raise TimeoutError
            return await wait_for(awaitable, timeout)

        monkeypatch.setattr("server.notifications.asyncio.wait_for", deadline)
    async with database_context("cancelled-refresh") as factory:
        async with factory() as session:
            job_id = await make_job(session, notify=False)
            event = JobEvent(job_id=job_id, type="external_notification", payload=notification())
            session.add(event)
            await session.flush()
            session.add(NotificationDelivery(job_id=job_id, job_event_id=event.id, target_name="external:radarr"))
            await session.commit()
        task = asyncio.create_task(NotificationService(factory).deliver_pending())
        try:
            await asyncio.wait_for(polling.wait(), 3)
            if interruption == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            else:
                await asyncio.wait_for(task, 3)
            assert not updates
            async with factory() as session:
                delivery = (await session.scalars(select(NotificationDelivery))).one()
                assert (delivery.status, delivery.attempts) == ("pending", 1)
                event = await session.get(JobEvent, delivery.job_event_id)
                assert event is not None and event.payload["progress"] == {"command_id": 23, "ids": [7]}
            retry = True
            await NotificationService(factory).deliver_pending()
            async with factory() as session:
                delivery = (await session.scalars(select(NotificationDelivery))).one()
                assert (delivery.status, delivery.attempts) == ("sent", 2)
            assert updates == [{"movieIds": [7], "monitored": False}]
            assert commands == [{"name": "RefreshMovie", "movieIds": [7]}]
        finally:
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
