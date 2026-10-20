from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import event, text

from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.cache import JsonCache
from server.events import EventBus
from server.scheduler.service import Scheduler
from server.stats import STATS_RETENTION, StatsService, build_snapshot, overview, savings
from tests.helpers import database_context
from tests.test_maintenance import Clock
from tests.test_stats import add_finished_attempt, add_job, add_library, add_media

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_cache_scopes_timezone(tmp_path):
    async with database_context("stats-timezones") as db:
        cache = JsonCache(tmp_path, clock=lambda: NOW)
        utc_events, local_events = EventBus(), EventBus()
        local_events.timezone = ZoneInfo("Asia/Kuwait")
        utc = StatsService(db, cache, utc_events, address=("stats", "snapshot"), clock=lambda: NOW)
        local = StatsService(db, cache, local_events, address=("stats", "snapshot"), clock=lambda: NOW)
        await utc.refresh()
        assert await utc.read() is not None
        assert await local.read() is None
        await local.refresh()
        assert await local.read() is not None


@pytest.mark.asyncio
async def test_projects_weighted_ranges() -> None:
    async with database_context("stats-ranges") as db:
        async with db() as session:
            first = await add_library(session, "first")
            second = await add_library(session, "second")
            for identifier, age, durations in ((first, 0, [10, 10, None]), (second, 1, [100])):
                for index, seconds in enumerate(durations):
                    media = await add_media(session, identifier, f"{identifier}-{index}.mkv", "h264")
                    finished = NOW - timedelta(days=age)
                    job = await add_job(
                        session,
                        identifier,
                        media,
                        status="succeeded",
                        finished_at=finished,
                        started_at=finished - timedelta(seconds=seconds) if seconds is not None else None,
                    )
                    argv = ["-i", "source.mkv", "-c", "copy", "output.mkv"]
                    if not age and index == 0:
                        argv[-1:-1] = ["-c:v:0", "libx265"]
                    await add_finished_attempt(
                        session, job, source_size=100, output_size=120 if age else 80, argv=None if index == 2 else argv
                    )
            old_media = await add_media(session, first, "old.mkv", None)
            old_job = await add_job(
                session, first, old_media, status="succeeded", finished_at=NOW - timedelta(days=365)
            )
            await add_finished_attempt(
                session, old_job, source_size=100, output_size=50, argv=["-c", "copy", "-c:v:0", "libx265"]
            )
            await session.commit()
            snapshot = await build_snapshot(session, EventBus(), NOW)
        recent = overview(snapshot, 1)
        assert recent["jobs"]["completed"] == 3
        assert recent["jobs"]["avg_duration_s"] == 10.0
        longer = overview(snapshot, 2)
        assert longer["jobs"]["completed"] == 4
        assert longer["jobs"]["avg_duration_s"] == 40.0
        assert overview(snapshot, 365)["jobs"]["completed"] == 4
        selected = overview(snapshot, 365, second)
        assert selected["jobs"]["avg_duration_s"] == 100.0
        assert selected["libraries"] == longer["libraries"]
        assert selected["savings"]["totals"]["saved_bytes"] == -20
        assert selected["savings"]["totals"]["avg_reduction_pct"] == -20.0
        assert savings(snapshot, 2)["series"] == [
            {"date": "2026-10-08", "files": 1, "saved_bytes": -20},
            {"date": "2026-10-09", "files": 3, "saved_bytes": 60},
        ]
        assert savings(snapshot, 365)["totals"]["saved_bytes"] == 90
        split = longer["savings"]["by_kind"]
        assert split["transcode"] == {
            "series": [
                {"date": "2026-10-08", "files": 0, "saved_bytes": 0},
                {"date": "2026-10-09", "files": 1, "saved_bytes": 20},
            ],
            "totals": {"files": 2, "saved_bytes": 70, "source_bytes": 200, "avg_reduction_pct": 35.0},
        }
        assert split["remux"] == {
            "series": [
                {"date": "2026-10-08", "files": 1, "saved_bytes": -20},
                {"date": "2026-10-09", "files": 1, "saved_bytes": 20},
            ],
            "totals": {"files": 2, "saved_bytes": 0, "source_bytes": 200, "avg_reduction_pct": 0.0},
        }
        assert split["unclassified"]["totals"]["saved_bytes"] == 20
        assert selected["savings"]["by_kind"]["remux"]["totals"]["saved_bytes"] == -20
        assert selected["savings"]["by_kind"]["transcode"]["totals"]["files"] == 0
        libraries = {row["id"]: row for row in selected["libraries"]}
        assert libraries[first]["successful_transcodes"] == 2
        assert libraries[first]["successful_remuxes"] == 1
        assert libraries[first]["successful_unclassified"] == 1
        assert libraries[first]["transcode_saved_bytes"] == 70
        assert libraries[first]["remux_saved_bytes"] == 20
        assert libraries[first]["unclassified_saved_bytes"] == 20
        assert libraries[first]["saved_bytes"] == 110
        assert libraries[second]["successful_remuxes"] == 1
        assert libraries[second]["remux_saved_bytes"] == -20
        empty = overview(snapshot, 17, 999)
        assert empty["inventory"]["files"] == empty["jobs"]["completed"] == 0
        assert empty["savings"]["totals"]["files"] == 0
        assert len(empty["savings"]["series"]) == 17
        assert set(empty["savings"]["by_kind"]) == {"transcode", "remux", "unclassified"}
        for value in empty["savings"]["by_kind"].values():
            assert value["totals"] == {"files": 0, "saved_bytes": 0, "source_bytes": 0, "avg_reduction_pct": 0.0}
            assert value["series"] == empty["savings"]["series"]


@pytest.mark.asyncio
async def test_requests_use_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_context("stats-requests") as db:
        async with db() as session:
            await add_library(session, "cached")
            await session.commit()
        app = create_app(db, cache_root=tmp_path)
        app.state.stats.clock = lambda: NOW
        app.state.cache.clock = lambda: NOW
        await app.state.stats.refresh()
        async with db() as session:
            engine = session.get_bind()

        def inspect_query(_connection, _cursor, statement, _parameters, _context, _many) -> None:
            assert not statement.lstrip().upper().startswith("SELECT") or "sessions" in statement

        def unexpected(*_args, **_kwargs):
            pytest.fail("A request attempted to regenerate stats.")

        event.listen(engine, "before_cursor_execute", inspect_query)
        monkeypatch.setattr("server.stats.build_snapshot", unexpected)
        try:
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                for _ in range(2):
                    summary = await client.get("/api/stats")
                    assert summary.status_code == 200
                    for days, library_id in ((1, None), (17, 1), (365, 999)):
                        params = {"days": days}
                        if library_id is not None:
                            params["library_id"] = library_id
                        response = await client.get("/api/stats/overview", params=params)
                        assert response.status_code == 200 and response.json()["days"] == days
                        response = await client.get("/api/stats/savings", params=params)
                        assert response.status_code == 200 and len(response.json()["series"]) == days
                async with app.state.stats.lock:
                    await app.state.stats.invalidate()
                for endpoint in ("/api/stats", "/api/stats/overview", "/api/stats/savings"):
                    response = await client.get(endpoint)
                    assert response.status_code == 503
        finally:
            event.remove(engine, "before_cursor_execute", inspect_query)
            await app.state.events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["query", "write"])
async def test_refresh_retains_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async with database_context("stats-failure") as db:
        cache = JsonCache(tmp_path, clock=lambda: NOW)
        bus = EventBus(min_interval_s=0)
        service = StatsService(db, cache, bus, address=("stats", "snapshot"), clock=lambda: NOW)
        subscription = bus.subscribe()
        try:
            await service.refresh()
            before = await cache.read(*service.address)
            assert before is not None and isinstance(before.payload, dict)
            assert before.generated_at == NOW and before.expires_at == NOW + STATS_RETENTION
            published = await asyncio.wait_for(subscription.next(), 1)
            assert published is not None and published.payload == before.payload["summary"]

            async def fail(*_args, **_kwargs) -> None:
                raise OSError("stats unavailable")

            if failure == "query":
                monkeypatch.setattr("server.stats.build_snapshot", fail)
            else:
                monkeypatch.setattr(cache, "write", fail)
            service.clock = lambda: NOW + timedelta(minutes=1)
            await service.refresh()
            assert await cache.read(*service.address) == before
            assert subscription.backlog == 0
            cache.clock = lambda: NOW + STATS_RETENTION
            assert await service.read() is None
        finally:
            bus.unsubscribe(subscription)
            await bus.aclose()


@pytest.mark.asyncio
async def test_refresh_closes_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_context("stats-close") as db:
        cache = JsonCache(tmp_path, clock=lambda: NOW)
        service = StatsService(db, cache, EventBus(), address=("stats", "snapshot"), clock=lambda: NOW)
        sessions = []
        write = cache.write

        async def capture(session, events, now):
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"
            sessions.append(session)
            return await build_snapshot(session, events, now)

        async def check_write(namespace, key, entry):
            assert not sessions[0].in_transaction()
            await write(namespace, key, entry)

        monkeypatch.setattr("server.stats.build_snapshot", capture)
        monkeypatch.setattr(cache, "write", check_write)
        await service.refresh()
        assert await service.read() is not None


@pytest.mark.asyncio
async def test_startup_populates_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_context("stats-startup") as db:
        app = create_app(db, cache_root=tmp_path)
        app.state.stats.clock = lambda: NOW
        app.state.cache.clock = lambda: NOW
        calls = 0
        ready = asyncio.Event()
        write = app.state.cache.write

        async def published(namespace, key, entry):
            await write(namespace, key, entry)
            ready.set()

        async def capture(session, events, now):
            nonlocal calls
            calls += 1
            return await build_snapshot(session, events, now)

        monkeypatch.setattr("server.stats.build_snapshot", capture)
        monkeypatch.setattr(app.state.cache, "write", published)
        async with asyncio.timeout(5):
            async with app.router.lifespan_context(app):
                await ready.wait()
                async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    response = await client.get("/api/stats")
                assert response.status_code == 200
                assert calls == 1
                entry = await app.state.cache.read(*app.state.stats.address)
                assert entry is not None and entry.generated_at == NOW


@pytest.mark.asyncio
async def test_slow_refresh_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_context("stats-slow") as db:
        app = create_app(db, cache_root=tmp_path)
        app.state.stats.clock = lambda: NOW
        app.state.cache.clock = lambda: NOW
        await app.state.stats.refresh()
        baseline = await app.state.stats.read()
        assert baseline is not None
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def slow(session, events, now):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
            return await build_snapshot(session, events, now)

        monkeypatch.setattr("server.stats.build_snapshot", slow)
        clock = Clock()
        scheduler = Scheduler(clock=clock, sleep=clock.sleep)
        runs: asyncio.Queue[None] = asyncio.Queue()

        async def maintain() -> None:
            runs.put_nowait(None)

        scheduler.add(app.state.stats.refresh, every=60)
        scheduler.add(maintain, every=60)
        scheduler.start()
        try:
            await clock.ready()
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(runs.get(), 1)
            async with db() as session:
                identifier = await add_library(session, "processing")
                await session.commit()
            await clock.advance(60)
            await asyncio.wait_for(runs.get(), 1)
            assert calls == 1
            async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                cached = await asyncio.wait_for(client.get("/api/stats"), 1)
                assert cached.status_code == 200 and cached.json() == baseline["summary"]
                response = await asyncio.wait_for(
                    client.patch(f"/api/libraries/{identifier}", json={"suspended": True}), 1
                )
                assert response.status_code == 200
        finally:
            release.set()
            await asyncio.wait_for(scheduler.aclose(), 3)
            await app.state.events.aclose()
