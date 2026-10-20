from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from common.media.probe import ProbeData
from common.plan import FFmpegPlan
from common.plan.models import VideoTarget
from common.protocol import (
    ActionReport,
    AttemptMissing,
    Finished,
    Heartbeat,
    NodeCapabilities,
    NodeLog,
    NodeLogs,
    Progress,
)
from server.events import EventBus
from server.nodehub.hub import NodeEntry, NodeHub
from tests.hub_helpers import Clock, FakeConn, NoDatabase

PROBE = ProbeData(file_path="jobs/7/source.mkv", duration_s=60, size_bytes=6, streams=[])
PLAN = FFmpegPlan(video=VideoTarget(encoder="libx264"))


@pytest_asyncio.fixture
async def unit_hub(tmp_path: Path) -> AsyncIterator[NodeHub]:
    clock = Clock()
    bus = EventBus(min_interval_s=0, clock=lambda: clock().timestamp())
    hub = NodeHub(NoDatabase(), workspace_dir=tmp_path / "workspace", clock=clock, events=bus)
    try:
        yield hub
    finally:
        await asyncio.wait_for(hub.aclose(), 2)
        await bus.aclose()


def node_entry(
    name: str = "node-a",
    identifier: int = 1,
    *,
    encoders: tuple[str, ...] = ("libx264",),
    limit: int = 1,
) -> NodeEntry:
    return NodeEntry(
        name=name,
        node_db_id=identifier,
        capabilities=NodeCapabilities(encoders=list(encoders), max_transcodes=limit),
        workspace_ready=True,
        conn=FakeConn(),
    )


def test_node_logs_authenticated(caplog: pytest.LogCaptureFixture) -> None:
    entry = node_entry("node-fixed")
    msg = NodeLogs(entries=[NodeLog(logger="node.agent", level="INFO", message="ready", job_id=7)])
    with caplog.at_level(logging.INFO, logger="node.node-fixed"):
        NodeHub._handle_node_logs(entry, msg)
    record = caplog.records[-1]
    assert record.name == "node.node-fixed"
    assert getattr(record, "source_logger") == "node.agent"
    assert getattr(record, "node_id") == "node-fixed"
    assert getattr(record, "job_id") == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("greeting", [{"type": "unknown"}, {"v": 1, "type": "heartbeat"}])
async def test_hello_rejects_unlocked(unit_hub: NodeHub, greeting: dict) -> None:
    conn = FakeConn([greeting])
    async with unit_hub.dispatch_lock:
        await asyncio.wait_for(unit_hub.serve_connection(conn), 2)
    assert conn.messages("error")[0]["code"] == "handshake"
    assert conn.closed == 4400
    assert not conn.messages("workspace_challenge") and not unit_hub.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["receive", "parse", "timeout", "wrong_type"])
async def test_hello_failure_diagnostics(unit_hub: NodeHub, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    class HelloFailure(FakeConn):
        async def recv_json(self) -> dict:
            if failure == "receive":
                raise OSError("node socket disconnected before hello")
            if failure == "timeout":
                raise TimeoutError
            if failure == "wrong_type":
                return {"v": 1, "type": "heartbeat"}
            return {"v": 1, "type": "hello", "capabilities": {}}

    conn = HelloFailure()
    await asyncio.wait_for(unit_hub.serve_connection(conn), 2)
    assert conn.messages("error")[0]["code"] == "handshake"
    assert not unit_hub.entries
    records = [record for record in caplog.records if record.name == "server.nodehub.hub"]
    assert records and all(record.levelno == logging.WARNING for record in records)
    if failure != "wrong_type":
        assert any(getattr(record, "error", None) for record in records)


@pytest.mark.asyncio
async def test_pick_prefers_alternative(unit_hub: NodeHub) -> None:
    cpu = node_entry()
    gpu = node_entry("node-b", 2, encoders=("hevc_nvenc",))
    unit_hub.entries = {entry.name: entry for entry in (cpu, gpu)}
    plan = FFmpegPlan(video=VideoTarget(encoder="libx264", encoder_priority=["hevc_nvenc", "libx264"]))
    assert unit_hub._pick(plan, PROBE) == (gpu, None)
    assert unit_hub._pick(plan, PROBE, avoid_node_id=gpu.node_db_id) == (cpu, None)
    cpu.in_flight[7] = "transcode"
    assert unit_hub._pick(plan, PROBE, avoid_node_id=gpu.node_db_id) == (gpu, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["disconnected", "paused", "busy", "reserved"])
async def test_pick_skips_unavailable(unit_hub: NodeHub, blocked: str) -> None:
    first, second = node_entry(), node_entry("node-b", 2)
    unit_hub.entries = {entry.name: entry for entry in (first, second)}
    reserved = {}
    if blocked == "disconnected":
        first.disconnected.set()
    elif blocked == "paused":
        first.paused = True
    elif blocked == "busy":
        first.in_flight[7] = "transcode"
    else:
        reserved[first.node_db_id] = 1
    assert unit_hub._pick(PLAN, PROBE, reserved=reserved) == (second, None)


@pytest.mark.asyncio
async def test_completions_consume_slots(unit_hub: NodeHub) -> None:
    entry = node_entry(limit=2)
    unit_hub.entries[entry.name] = entry
    entry.in_flight = {7: "transcode", 10: "healthcheck"}
    release = asyncio.Event()

    async def complete() -> None:
        await release.wait()

    tasks = [asyncio.create_task(complete()) for _ in range(4)]
    unit_hub._completions = {
        (1, 7, 1, "finished", None): tasks[0],
        (1, 8, 2, "finished", None): tasks[1],
        (2, 9, 3, "finished", None): tasks[2],
        (1, 11, 4, "finished", None): tasks[3],
    }
    unit_hub._completion_kinds = {
        (1, 7, 1, "finished", None): "transcode",
        (1, 8, 2, "finished", None): "transcode",
        (2, 9, 3, "finished", None): "transcode",
        (1, 11, 4, "finished", None): "healthcheck",
    }
    try:
        assert unit_hub._node_load(entry) == 2
        assert not unit_hub._has_free_slot()
        assert unit_hub._pick(PLAN, PROBE)[0] is None
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert unit_hub._node_load(entry) == 1
        assert unit_hub._has_free_slot()
        assert unit_hub._pick(PLAN, PROBE) == (entry, None)
        entry.disconnected.set()
        assert not unit_hub._has_free_slot()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        unit_hub._completions.clear()
        unit_hub._completion_kinds.clear()


@pytest.mark.asyncio
async def test_reservations_follow_capabilities(unit_hub: NodeHub) -> None:
    first = node_entry()
    second = node_entry("node-b", 2, encoders=("libx265",))
    unit_hub.entries = {entry.name: entry for entry in (first, second)}
    release = asyncio.Event()

    async def stage() -> int:
        await release.wait()
        return 1

    tasks = [asyncio.create_task(stage()) for _ in range(2)]
    unit_hub.staging.tasks = dict(zip((7, 8), tasks, strict=True))
    unit_hub._staging_slots = {jid: (PLAN, PROBE, None, None, None) for jid in (7, 8, 9)}
    try:
        assert unit_hub._staging_reservations() == {first.node_db_id: 1}
        first.capabilities.encoders = ["libx265"]
        second.capabilities.encoders = ["libx264"]
        assert unit_hub._staging_reservations() == {second.node_db_id: 1}
        unit_hub.staging.tasks.clear()
        assert unit_hub._staging_reservations() == {}
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        unit_hub.staging.tasks.clear()
        unit_hub._staging_slots.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [1, 2])
async def test_release_follows_identity(unit_hub: NodeHub, identifier: int) -> None:
    old, current = node_entry(), node_entry(identifier=identifier)
    unit_hub.entries[current.name] = current
    for entry in (old, current):
        entry.in_flight[7] = "transcode"
        entry.offered_at[7] = unit_hub.clock()
        entry.last_progress_at[7] = unit_hub.clock()
    unit_hub._stall_cancelled.add(7)
    unit_hub._cancel_requested.add(7)
    bus = unit_hub.events
    assert bus is not None
    subscription = bus.subscribe()
    try:
        unit_hub._release_slot(old, 7)
        released = current if identifier == old.node_db_id else old
        retained = old if released is current else current
        assert not released.in_flight and not released.offered_at and not released.last_progress_at
        assert retained.in_flight == {7: "transcode"}
        assert not unit_hub._stall_cancelled and not unit_hub._cancel_requested
        assert unit_hub._wake.is_set()
        event = await asyncio.wait_for(subscription.next(), 2)
        assert event is not None and event.type == "node.status" and event.key == "node.status:node-a"
        assert event.payload == {
            "name": "node-a",
            "state": "online",
            "active_jobs": 0 if released is current else 1,
            "active_job_ids": [] if released is current else [7],
            "active_transcodes": 0 if released is current else 1,
            "active_healthchecks": 0,
            "active_mkv_stats": 0,
        }
    finally:
        bus.unsubscribe(subscription)


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [[], [9, 3]])
async def test_release_prunes_disconnected(unit_hub: NodeHub, remaining: list[int]) -> None:
    entry = node_entry()
    entry.in_flight = dict.fromkeys([7, *remaining], "transcode")
    entry.disconnected.set()
    unit_hub.entries[entry.name] = entry
    bus = unit_hub.events
    assert bus is not None
    subscription = bus.subscribe()
    try:
        unit_hub._release_slot(entry, 7)
        assert (entry.name in unit_hub.entries) is bool(remaining)
        assert unit_hub._entry_removed.is_set() is (not remaining)
        event = await asyncio.wait_for(subscription.next(), 2)
        assert event is not None and event.payload == {
            "name": entry.name,
            "state": "offline",
            "active_jobs": len(remaining),
            "active_job_ids": sorted(remaining),
            "active_transcodes": len(remaining),
            "active_healthchecks": 0,
            "active_mkv_stats": 0,
        }
    finally:
        bus.unsubscribe(subscription)


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [0, 1])
async def test_staged_sets_retry(unit_hub: NodeHub, staged: int) -> None:
    unit_hub._staging_slots[7] = (PLAN, PROBE, None, None, None)
    signal = unit_hub._signal_for("job:7")
    unit_hub._staged(7, staged)
    assert 7 not in unit_hub._staging_slots
    assert signal.is_set() and unit_hub._wake.is_set()
    assert unit_hub._staging_retry_at == (
        {} if staged else {7: unit_hub.clock() + timedelta(seconds=unit_hub.dispatch_interval_s)}
    )


@pytest.mark.asyncio
async def test_receive_routes_messages(unit_hub: NodeHub, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = node_entry()
    unit_hub.entries[entry.name] = entry
    messages = [
        Heartbeat(cpu_percent=25),
        Progress(job_id=7, attempt_id=11, percent=50),
        AttemptMissing(job_id=7, attempt_id=11, log_path="jobs/7/attempts/1/ffmpeg.log"),
        Finished(job_id=7, attempt_id=11, result="failed", log_path="jobs/7/attempts/1/ffmpeg.log"),
        ActionReport(job_id=7, attempt_id=11, results=[]),
    ]
    conn = FakeConn([{"type": "invalid"}, *(message.model_dump(mode="json") for message in messages), None])
    entry.conn = conn
    routed: list[tuple[str, object]] = []

    async def heartbeat(owner: NodeEntry, msg: Heartbeat) -> None:
        assert owner is entry
        routed.append(("heartbeat", msg))

    async def progress(owner: NodeEntry, msg: Progress) -> None:
        assert owner is entry
        routed.append(("progress", msg))

    async def journals(node_id: int, *, wait: bool = True) -> None:
        assert node_id == entry.node_db_id and not wait
        routed.append(("journals", node_id))

    async def missing(owner: NodeEntry, msg: AttemptMissing) -> None:
        assert owner is entry
        routed.append(("missing", msg))

    def completion(node_id: int, msg: Finished | ActionReport) -> tuple[None, bool]:
        assert node_id == entry.node_db_id
        routed.append(("completion", msg))
        return None, False

    monkeypatch.setattr(unit_hub, "_handle_heartbeat", heartbeat)
    monkeypatch.setattr(unit_hub, "_handle_progress", progress)
    monkeypatch.setattr(unit_hub, "read_journals", journals)
    monkeypatch.setattr(unit_hub, "_finish_missing", missing)
    monkeypatch.setattr(unit_hub, "_launch_completion", completion)
    await asyncio.wait_for(unit_hub._receive_loop(entry), 2)
    assert conn.messages("error")[0]["code"] == "protocol"
    assert routed == [
        ("heartbeat", messages[0]),
        ("progress", messages[1]),
        ("journals", entry.node_db_id),
        ("missing", messages[2]),
        ("completion", messages[3]),
        ("completion", messages[4]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["replacement", "disconnected"])
async def test_receive_checks_entries(unit_hub: NodeHub, caplog: pytest.LogCaptureFixture, stale: str) -> None:
    entry = node_entry()
    log = NodeLogs(entries=[NodeLog(logger="node.agent", level="INFO", message="stale worker log")])
    entry.conn = FakeConn([log.model_dump(mode="json"), None])
    unit_hub.entries[entry.name] = entry
    if stale == "replacement":
        unit_hub.entries[entry.name] = node_entry()
    else:
        entry.disconnected.set()
    with caplog.at_level(logging.INFO, logger="node.node-a"):
        await asyncio.wait_for(unit_hub._receive_loop(entry), 2)
    assert not [record for record in caplog.records if record.name == "node.node-a"]
