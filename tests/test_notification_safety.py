from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
import requests
from sqlalchemy import select
from sqlalchemy.sql.dml import Update
from starlette.requests import Request

from common.enums import JobStatus
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.events import EventBus
from server.models import Job, JobEvent, NotificationDelivery, NotificationTarget
from server.notifications import NotificationService, _apprise_send
from server.queue.store import finish
from server.retention import prune_jobs
from server.staging import StagingService
from tests.helpers import database_context, make_test_temp_dir
from tests.test_log_history import file_logs
from tests.test_notification_delivery import NOW, make_job
from tests.test_api import ApiEnv, delete_requested


@pytest.fixture
def io_draining(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    import server.notifications as notifications

    draining = asyncio.Event()
    owners = set()
    wait_io, wait = notifications.wait_workspace_io, asyncio.wait

    async def observe_io(operation):
        owner = asyncio.current_task()
        assert owner is not None
        owners.add(owner)
        try:
            return await wait_io(operation)
        finally:
            owners.remove(owner)

    async def observe_wait(futures, **kwargs):
        owner = asyncio.current_task()
        # Signal entry into the real thread drainer, not just a cancellation request.
        if owner is not None and owner in owners and owner.cancelling():
            draining.set()
        return await wait(futures, **kwargs)

    monkeypatch.setattr(notifications, "wait_workspace_io", observe_io)
    monkeypatch.setattr(asyncio, "wait", observe_wait)
    return draining


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["age", "single", "bulk", "missing"])
async def test_pending_request_retained(operation: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Concurrent sessions need separate connections and transactions.
    async with database_context("pending-notification") as factory:
        async with factory() as session:
            job_id = await make_job(session)
            await finish(session, job_id, JobStatus.FAILED, now=NOW)
            await session.commit()
        bus = EventBus()
        root = make_test_temp_dir("notification-deletion")
        app = create_app(
            factory,
            config=ServerConfig(workspace_dir=str(root / "workspace"), trash_dir=str(root / "trash")),
            events=bus,
            ffprobe_bin="unused",
            cache_root=root / "cache",
        )
        written = asyncio.Event()
        try:
            if operation == "age":
                assert await prune_jobs(factory, 1, now=NOW + timedelta(days=30)) == 0
            elif operation == "missing":
                from server.models import Library
                from server.scanner.scan import _prune_missing

                async with factory() as session:
                    job = await session.get(Job, job_id)
                    assert job is not None
                    library = await session.get(Library, job.library_id)
                    assert library is not None
                    library.scan_id = 1
                    await session.commit()
                    assert (
                        await _prune_missing(
                            session,
                            library,
                            1,
                            staging=StagingService(
                                factory, make_test_temp_dir("notification-workspace"), min_free_gb=1
                            ),
                            unavailable=set(),
                            excluded=(),
                        )
                        == 0
                    )
                    await session.commit()
            else:
                reading, reader_closed = asyncio.Event(), asyncio.Event()
                reader = None
                session_class = factory.class_
                execute, close = session_class.execute, session_class.close

                async def overlap(self, statement, *args, **kwargs):
                    nonlocal reader
                    result = await execute(self, statement, *args, **kwargs)
                    if asyncio.current_task() is app.state.jobs._loop and reader is None:
                        reader = self
                        reading.set()
                        await asyncio.wait_for(written.wait(), 5)
                    elif isinstance(statement, Update) and statement.table.name == "jobs":
                        written.set()
                        await asyncio.wait_for(reader_closed.wait(), 5)
                    return result

                async def close_reader(self):
                    await close(self)
                    if self is reader:
                        reader_closed.set()

                monkeypatch.setattr(session_class, "execute", overlap)
                monkeypatch.setattr(session_class, "close", close_reader)
                # Close the queue reader after DELETE writes, before its commit.
                # A shared connection would roll back the accepted deletion flag.
                await app.state.jobs.start()
                await asyncio.wait_for(reading.wait(), 5)
                async with api_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    env = ApiEnv(factory=factory, client=client, app=app, lib_dir=root)
                    response, outcomes = await delete_requested(env, [job_id], bulk=operation == "bulk")
                    assert response.status_code == 202 and outcomes[job_id]["outcome"] == "failed"
            async with factory() as session:
                assert await session.get(Job, job_id) is not None
                delivery = (await session.scalars(select(NotificationDelivery))).one()
                assert delivery.status == "pending"
                assert await session.get(JobEvent, delivery.job_event_id) is not None
        finally:
            written.set()
            try:
                await asyncio.wait_for(app.state.jobs.close(), 5)
            finally:
                await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "cancel", "timeout"])
async def test_delivery_worker_drained(tmp_path, monkeypatch, io_draining: asyncio.Event, operation: str) -> None:
    import server.notifications as notifications

    started, release, finished = (Event() for _ in range(3))

    def send(url: str, body: str, title: str) -> bool:
        started.set()
        try:
            assert release.wait(5)
            return True
        finally:
            finished.set()

    monkeypatch.setattr(notifications, "_apprise_send_sync", send)

    async with database_context("notification-drain") as factory:
        async with factory() as session:
            job_id = await make_job(session)
            session.add(NotificationTarget(name="primary", url="json://example.test/notify"))
            await finish(session, job_id, JobStatus.FAILED, now=NOW)
            await session.commit()
        service = NotificationService(factory)
        task = None
        close = None
        send_task = None
        if operation == "timeout":
            # An already-expired deadline exercises the same cancellation handoff
            # without making the test depend on elapsed time.
            wait_for = asyncio.wait_for

            async def expired(awaitable, timeout):
                nonlocal send_task
                if timeout == notifications._SEND_TIMEOUT_S:
                    child = asyncio.ensure_future(awaitable)
                    send_task = child
                    try:
                        assert await asyncio.to_thread(started.wait, 3)
                        child.cancel()
                        await child
                    except asyncio.CancelledError:
                        raise TimeoutError from None
                return await wait_for(awaitable, timeout=timeout)

            monkeypatch.setattr(notifications.asyncio, "wait_for", expired)
        try:
            if operation == "close":
                task = asyncio.create_task(service._run())
                service._task = task
            else:
                task = asyncio.create_task(service.deliver_pending())
            assert await asyncio.to_thread(started.wait, 3)
            if operation == "close":
                close = asyncio.create_task(service.aclose())
            elif operation == "cancel":
                assert task is not None
                task.cancel()
            await asyncio.wait_for(io_draining.wait(), timeout=3)
            assert task is not None
            owner = send_task if operation == "timeout" else task
            assert owner is not None
            assert not owner.done(), "notification task finished while its delivery thread was still sending"
            if close is not None:
                assert not close.done(), "service close returned before its delivery worker drained"
        finally:
            release.set()
            if task is not None and operation != "timeout":
                task.cancel()
            await asyncio.wait_for(asyncio.gather(*(item for item in (task, close) if item), return_exceptions=True), 5)
            assert await asyncio.to_thread(finished.wait, 3)
            await asyncio.wait_for(service.aclose(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delivery", "manual", "validate"])
async def test_provider_password_confined(tmp_path, monkeypatch, caplog, operation: str) -> None:
    from server.core.log_history import query_logs
    from server.api.notifications import _validate_url
    from server.notifications import _apprise_test

    credential = "private-password"

    def refuse(*args, **kwargs):
        raise requests.RequestException("request failed at /notify?format=json using password " + credential)

    monkeypatch.setattr("apprise.plugins.custom_json.requests.request", refuse)
    with caplog.at_level(logging.DEBUG), file_logs(tmp_path):
        url = "json://user:" + credential + "@example.test/notify?format=json"
        if operation == "validate":
            _validate_url(url)
        elif operation == "manual":
            assert not (await _apprise_test(url, "body", "title", 60))[0]
        else:
            with pytest.raises(RuntimeError):
                await _apprise_send(url, "body", "title", 60)
    entries = query_logs(tmp_path)["entries"]
    assert entries
    assert credential not in json.dumps(entries), "provider diagnostics persisted the configured password"
    assert all(credential not in record.getMessage() for record in caplog.records)
    if operation != "validate":
        assert "/notify?format=json" in json.dumps(entries)


@pytest.mark.asyncio
async def test_malformed_delivery_isolated() -> None:
    calls = []

    async def send(url, body, title, timeout):
        calls.append(body)
        return True

    async with database_context("malformed-notification") as factory:
        async with factory() as session:
            job_id = await make_job(session)
            session.add(NotificationTarget(name="primary", url="json://example.test/notify"))
            await finish(session, job_id, JobStatus.FAILED, now=NOW)
            bad = (await session.scalars(select(JobEvent).where(JobEvent.type == "failed"))).one()
            bad.type = "succeeded"
            bad.payload = {"source_size": "invalid", "output_size": 1}
            healthy = JobEvent(job_id=job_id, type="failed", payload={"error": "ordinary failure"})
            session.add(healthy)
            await session.flush()
            session.add(NotificationDelivery(job_id=job_id, job_event_id=healthy.id, target_name="primary"))
            await session.commit()
            healthy_id = healthy.id
        await NotificationService(factory, send=send).deliver_pending()
        async with factory() as session:
            malformed = await session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.job_event_id == bad.id)
            )
            assert malformed is not None and malformed.status == "failed"
            delivery = await session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.job_event_id == healthy_id)
            )
            assert delivery is not None and delivery.status == "sent"
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_manual_worker_drained(monkeypatch, io_draining: asyncio.Event) -> None:
    import server.notifications as notifications

    started, release, finished = (Event() for _ in range(3))

    def send(url, body, title):
        started.set()
        try:
            assert release.wait(5)
            return True, None
        finally:
            finished.set()

    monkeypatch.setattr(notifications, "_apprise_test_sync", send)
    task = asyncio.create_task(notifications._apprise_test("json://example.test/notify", "body", "title", 60))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.wait_for(io_draining.wait(), 3)
        assert not task.done(), "manual test cancellation abandoned an active notification thread"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert await asyncio.to_thread(finished.wait, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "deadline", "deadline_cancel"])
async def test_manual_route_drained(monkeypatch, io_draining: asyncio.Event, operation: str) -> None:
    from server.api.notifications import test_target
    import server.notifications as notifications
    from fastapi import HTTPException

    started, release, finished = (Event() for _ in range(3))
    armed = asyncio.Event()
    loop = asyncio.get_running_loop()
    call_at = loop.call_at
    route = None
    deadline = None
    expire = None

    def arm(when, callback, *args, context=None):
        nonlocal deadline, expire
        handle = call_at(when, callback, *args, context=context)
        if asyncio.current_task() is route and isinstance(getattr(callback, "__self__", None), asyncio.Timeout):
            deadline, expire = handle, callback
            armed.set()
        return handle

    def send(url, body, title):
        started.set()
        try:
            assert release.wait(5)
            return True, None
        finally:
            finished.set()

    monkeypatch.setattr(notifications, "_apprise_test_sync", send)
    monkeypatch.setattr(loop, "call_at", arm)
    async with database_context("manual-route-drain") as factory:
        async with factory() as session:
            session.add(NotificationTarget(name="primary", url="json://example.test/notify"))
            await session.commit()
        request = Request({"type": "http", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(db=factory))})
        route = asyncio.create_task(test_target("primary", request))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.wait_for(armed.wait(), 3)
            assert deadline is not None and expire is not None
            if operation.startswith("deadline"):
                # Fire the real wait_for deadline callback after worker acquisition.
                deadline.cancel()
                expire()
                await asyncio.wait_for(io_draining.wait(), 3)
                assert not route.done(), "the notification route finished before its worker exited"
            if operation != "deadline":
                for _ in range(3):
                    io_draining.clear()
                    route.cancel()
                    await asyncio.wait_for(io_draining.wait(), 3)
                    assert not route.done(), "the notification route finished before its worker exited"
            release.set()
            outcome = (await asyncio.wait_for(asyncio.gather(route, return_exceptions=True), 5))[0]
            if operation == "deadline":
                assert isinstance(outcome, HTTPException) and outcome.status_code == 504
            else:
                assert isinstance(outcome, asyncio.CancelledError)
            assert await asyncio.to_thread(finished.wait, 3)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(route, return_exceptions=True), 5)
            assert await asyncio.to_thread(finished.wait, 3)
