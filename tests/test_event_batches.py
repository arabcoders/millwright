from __future__ import annotations

import asyncio

import pytest

from server.events import Event, EventBus, Subscription


async def next_event(sub: Subscription) -> Event | None:
    return await asyncio.wait_for(sub.next(), timeout=1)


@pytest.mark.asyncio
async def test_pending_frames_paced() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        for job_id in range(300):
            bus.publish("job.progress", {"job_id": job_id, "progress": {"percent": 1}})
        timer = bus._bulk_timer
        assert timer is not None
        assert sub.backlog == 0
        for remaining in (200, 100, 0):
            bus.flush_bulk()
            assert sub.backlog == 1
            event = await next_event(sub)
            assert event is not None and len(event.payload["events"]) == 100
            assert len(bus._bulk) == remaining
            if remaining:
                assert bus._bulk_timer is not None
                assert bus._bulk_timer is not timer
                timer = bus._bulk_timer
                bus.publish("job.progress", {"job_id": 299, "progress": {"percent": 2}})
                assert bus._bulk_timer is timer
            else:
                assert bus._bulk_timer is None
    finally:
        await asyncio.wait_for(bus.aclose(), 1)


@pytest.mark.asyncio
async def test_merged_history_bounded() -> None:
    bus = EventBus()
    bus.subscribe()
    try:
        for job_id in range(1000):
            bus.publish("job.status", {"job_id": job_id, "status": "running", "updated_at": "2026-01-01T00:00:00Z"})
            bus.publish("job.progress", {"job_id": job_id, "progress": {}, "updated_at": "2026-01-01T00:00:01Z"})
        assert len(bus._bulk) <= 512
        assert len(bus._latest_progress) <= 512
    finally:
        await asyncio.wait_for(bus.aclose(), 1)


@pytest.mark.asyncio
async def test_latest_coalesces() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        bus.publish("job.progress", {"job_id": 1, "progress": {"percent": 10}})
        bus.publish("job.progress", {"job_id": 1, "progress": {"percent": 20}})
        bus.flush_bulk()
        event = await next_event(sub)
        assert event is not None
        assert event.payload["events"] == [
            {"type": "job.progress", "payload": {"job_id": 1, "progress": {"percent": 20}}}
        ]
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_terminal_retry_progress() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        bus.publish("job.status", {"job_id": 1, "status": "succeeded"}, key="job.status:1")
        bus.publish("job.progress", {"job_id": 1, "progress": {"percent": 99}}, key="job.progress:1")
        bus.flush_bulk()
        terminal = await next_event(sub)
        assert terminal is not None
        assert all(item["type"] != "job.progress" for item in terminal.payload["events"])
        bus.publish("job.status", {"job_id": 1, "status": "queued"}, key="job.status:1")
        bus.publish("job.progress", {"job_id": 1, "progress": {"percent": 1}}, key="job.progress:1")
        bus.flush_bulk()
        retry = await next_event(sub)
        assert retry is not None
        assert {item["type"] for item in retry.payload["events"]} == {"job.status", "job.progress"}
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_progress_merges_older_status() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        bus.publish(
            "job.progress",
            {"job_id": 1, "updated_at": "2026-01-01T00:00:02Z", "progress": {"percent": 2}},
            key="job.progress:1",
        )
        bus.publish(
            "job.status", {"job_id": 1, "status": "running", "updated_at": "2026-01-01T00:00:01Z"}, key="job.status:1"
        )
        bus.flush_bulk()
        frame = await next_event(sub)
        assert frame is not None
        assert len(frame.payload["events"]) == 1
        assert [item["type"] for item in frame.payload["events"]] == ["job.status"]
        assert frame.payload["events"][0]["payload"]["progress"] == {"percent": 2}
        bus.publish(
            "job.status", {"job_id": 1, "status": "running", "updated_at": "2026-01-01T00:00:01Z"}, key="job.status:1"
        )
        bus.flush_bulk()
        later = await next_event(sub)
        assert later is not None and later.payload["events"][0]["payload"]["progress"] == {"percent": 2}
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_delete_purges_progress() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        bus.publish("job.status", {"job_id": 1, "status": "queued"})
        bus.publish("job.progress", {"job_id": 1, "progress": {"percent": 1}})
        bus.publish("job.deleted", {"job_id": 1})
        event = await next_event(sub)
        assert event is not None and event.type == "job.deleted"
        bus.flush_bulk()
        assert bus._bulk == {}
        assert sub.backlog == 0
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_overflow_resyncs() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    try:
        for index in range(513):
            bus.publish("job.progress", {"job_id": index}, key=f"job.progress:{index}")
        assert sub.backlog == 0
        bus.flush_bulk()
        event = await next_event(sub)
        assert event is not None and event.type == "events.resync"
        assert len(bus._bulk) == 0
    finally:
        await bus.aclose()


@pytest.mark.asyncio
async def test_close_cancels_timer() -> None:
    bus = EventBus()
    bus.subscribe()
    bus.publish("job.progress", {"job_id": 1})
    timer = bus._bulk_timer
    assert timer is not None
    await bus.aclose()
    assert timer.cancelled()
