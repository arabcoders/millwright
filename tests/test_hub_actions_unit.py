from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from common.actions import Action, ActionOutcome, ExtractSubtitles
from common.media.probe import ProbeData
from common.protocol import NodeCapabilities
from server.events import EventBus
from server.nodehub.hub import NodeEntry, NodeHub
from tests.hub_helpers import Clock, FakeConn, NoDatabase


class Connection(FakeConn):
    callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    async def send_json(self, data: dict[str, Any]) -> None:
        await super().send_json(data)
        if self.callback is not None:
            await self.callback(data)


@dataclass(kw_only=True)
class Completion:
    job_id: int
    attempt_id: int
    node_id: int
    results: list[ActionOutcome]
    rejected: list[ActionOutcome]
    finished: bool
    accepted: bool
    prerequisite_error: str | None


@dataclass(kw_only=True)
class Case:
    hub: NodeHub
    entry: NodeEntry
    conn: Connection
    probe: ProbeData
    action: ExtractSubtitles
    completions: list[Completion] = field(default_factory=list)
    cleanups: list[tuple[int, int]] = field(default_factory=list)

    async def dispatch(
        self, path: str | None = "jobs/7/attempts/2/output.mkv", requires: list[str] | None = None
    ) -> None:
        await self.hub._dispatch_post_actions(self.entry, 7, 2, path, self.probe, [self.action], requires or [], [])

    def report(self, results: list[ActionOutcome]) -> None:
        self.hub._action_reports[(7, 2)].set_result(results)


@pytest_asyncio.fixture
async def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Case]:
    clock = Clock()
    bus = EventBus(min_interval_s=0, clock=lambda: clock().timestamp())
    hub = NodeHub(NoDatabase(), workspace_dir=tmp_path / "workspace", clock=clock, events=bus)
    conn = Connection()
    entry = NodeEntry(
        name="node-a",
        node_db_id=11,
        capabilities=NodeCapabilities(encoders=["libx264"]),
        workspace_ready=True,
        conn=conn,
    )
    hub.entries[entry.name] = entry
    case = Case(
        hub=hub,
        entry=entry,
        conn=conn,
        action=ExtractSubtitles(formats=["srt"], languages=["eng"]),
        probe=ProbeData(file_path="/library/movie.mkv", duration_s=60, size_bytes=6, streams=[], raw={"format": {}}),
    )

    async def prepare(job_id: int, attempt_id: int, actions: list[Action]) -> bool:
        return True

    async def complete(
        job_id: int,
        attempt_id: int,
        path: str | None,
        probe: ProbeData | None,
        results: list[ActionOutcome],
        rejected: list[ActionOutcome],
        *,
        finished: bool,
        accepted: bool,
        prerequisite_error: str | None,
        node_id: int,
    ) -> bool:
        case.completions.append(
            Completion(
                job_id=job_id,
                attempt_id=attempt_id,
                node_id=node_id,
                results=list(results),
                rejected=list(rejected),
                finished=finished,
                accepted=accepted,
                prerequisite_error=prerequisite_error,
            )
        )
        return finished

    async def cleanup(job_id: int, attempt_id: int) -> None:
        case.cleanups.append((job_id, attempt_id))

    monkeypatch.setattr(hub, "_prepare_post_actions", prepare)
    monkeypatch.setattr(hub, "_finish_post_actions", complete)
    monkeypatch.setattr(hub, "_cleanup_action_workspace", cleanup)
    try:
        yield case
    finally:
        await bus.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["send", "cancel", "cancel_error", "pending"])
@pytest.mark.parametrize("ok", [True, False])
async def test_report_overrides_delivery(case: Case, phase: str, ok: bool) -> None:
    cancelling = asyncio.Event()
    result = ActionOutcome(kind="extract_subtitles", ok=ok, error=None if ok else "worker extraction failed")

    async def send(data: dict[str, Any]) -> None:
        if data["type"] == "action_task":
            if phase == "send":
                case.report([result])
            raise OSError("task send failed after delivery")
        if data["type"] == "cancel":
            if phase != "pending":
                case.report([result])
            cancelling.set()
            if phase == "cancel_error":
                raise OSError("cancel send failed after report")

    case.conn.callback = send
    dispatch = asyncio.create_task(case.dispatch())
    try:
        if phase == "pending":
            await asyncio.wait_for(cancelling.wait(), 2)
            assert not dispatch.done() and not case.completions and not case.cleanups
            assert 7 in case.hub.staging.busy_jobs
            case.report([result])
        await asyncio.wait_for(dispatch, 2)
        assert len(case.completions) == 1
        outcome = case.completions[0]
        assert (outcome.job_id, outcome.attempt_id, outcome.node_id) == (7, 2, 11)
        assert outcome.accepted and outcome.finished
        assert outcome.results == [result] and outcome.rejected == [] and outcome.prerequisite_error is None
        task = case.conn.messages("action_task")[0]
        assert (task["job_id"], task["attempt_id"]) == (7, 2)
        assert task["probe"]["file_path"] == "jobs/7/attempts/2/output.mkv"
        assert task["probe"]["raw"] is None
        assert len(case.conn.messages("cancel")) == (0 if phase == "send" else 1)
        assert case.conn.closed is None
        assert case.cleanups == [(7, 2)]
        assert not case.hub._action_reports and not case.hub._action_nodes and not case.hub._action_jobs
        assert 7 not in case.hub.staging.busy_jobs
    finally:
        if not dispatch.done():
            case.report([result])
            dispatch.cancel()
        await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["workspace", "requirements", "path", "validation"])
async def test_prerequisites_block_delivery(case: Case, failure: str) -> None:
    path: str | None = "jobs/7/attempts/2/output.mkv"
    requires: list[str] = []
    if failure == "workspace":
        case.entry.workspace_ready = False
    elif failure == "requirements":
        requires = ["missing"]
    elif failure == "path":
        path = None
    else:
        path = "/outside/output.mkv"
    await asyncio.wait_for(case.dispatch(path, requires), 2)
    assert not case.conn.messages("action_task") and not case.conn.messages("cancel")
    outcome = case.completions[0]
    assert not outcome.accepted and outcome.finished and len(outcome.results) == 1
    result = outcome.results[0]
    assert not result.ok and result.kind == "extract_subtitles" and result.error
    assert case.cleanups == [(7, 2)] and not case.hub._action_reports


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["completion", "cleanup"])
async def test_cancellation_drains_handoff(case: Case, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    method = "_finish_post_actions" if boundary == "completion" else "_cleanup_action_workspace"
    original = getattr(case.hub, method)

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def send(data: dict[str, Any]) -> None:
        if data["type"] == "action_task":
            case.report([ActionOutcome(kind="extract_subtitles", ok=True)])

    monkeypatch.setattr(case.hub, method, blocked)
    case.conn.callback = send
    dispatch = asyncio.create_task(case.dispatch())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        dispatch.cancel()
        checkpoint = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
        await asyncio.wait_for(checkpoint, 2)
        assert not dispatch.done() and not case.cleanups
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(dispatch, 2)
        assert len(case.completions) == 1 and case.cleanups == [(7, 2)]
        assert not case.hub._action_jobs and not case.hub._action_reports and 7 not in case.hub.staging.busy_jobs
    finally:
        release.set()
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.wait_for(asyncio.gather(dispatch, return_exceptions=True), 2)
