from __future__ import annotations

import asyncio
import fcntl
import json
import multiprocessing
import os
import socket
import struct
import sys
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import select

from server.finalization import finalize, recover
from common.plan import FFmpegPlan
from common.media.probe import ProbeData
from common.protocol import Heartbeat
from server.models import FinalizationJournal, Job, JobEvent
from server.nodehub.store import get_node
from server.staging import StagingService
from server.storage import storage_io
from server.trash import _try_library_lock, library_mutation_lock
from server.workspace import _remove_attempt, _stage_source
from tests.test_finalization import publication_db, raw
from tests.hub_helpers import FakeConn
from tests.test_hub import RAW, file_job, finished, hello, hub, materialize_offer, serve
from tests.test_hub import writer_env as writer_env
from tests.test_staging import add_job


def _gated_stage(address: tuple[str, int], *args, **kwargs) -> str:
    with socket.create_connection(address, timeout=10) as gate:
        gate.sendall(f"{os.getpid()}\n".encode())
        if gate.recv(1) != b"x":
            raise RuntimeError("Staging test gate closed.")
        return _stage_source(*args, **kwargs)


async def _cancel_turn(task: asyncio.Task) -> None:
    task.cancel()
    processed = asyncio.Event()
    asyncio.get_running_loop().call_soon(processed.set)
    await processed.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True], ids=["cancel", "shutdown"])
async def test_staging_process_finishes(tmp_path: Path, database_factory, monkeypatch, shutdown: bool) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    job_id = await add_job(database_factory, source)
    service = StagingService(database_factory, tmp_path / "workspace", min_free_gb=0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        monkeypatch.setattr("server.staging._stage_source", partial(_gated_stage, listener.getsockname()))
        assert service.schedule(job_id)
        staging = service.tasks[job_id]
        gate = None
        closing = None
        try:
            gate, _ = await asyncio.wait_for(asyncio.to_thread(listener.accept), 15)
            pid = int(await asyncio.wait_for(asyncio.to_thread(gate.recv, 100), 5))
            assert pid != os.getpid()
            finishing = staging
            if shutdown:
                close_started = asyncio.Event()
                close = service._close

                async def closing_service():
                    close_started.set()
                    await close()

                monkeypatch.setattr(service, "_close", closing_service)
                closing = asyncio.create_task(service.aclose())
                finishing = closing
                await asyncio.wait_for(close_started.wait(), 5)
            for _ in range(2):
                await _cancel_turn(finishing)
                assert not finishing.done() and job_id in service.active_jobs
            gate.sendall(b"x")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(finishing, 15)
            await asyncio.wait_for(service.wait_idle(), 5)
            assert (service.workspace_dir / f"jobs/{job_id}/source.mkv").read_bytes() == b"source"
            assert not service.active_jobs
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if gate is not None:
                gate.close()
            await asyncio.wait_for(service.aclose(), 15)
            await asyncio.gather(staging, *([closing] if closing is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["publish", "cancel-partial"])
async def test_finalization_process_finishes(tmp_path: Path, database_factory, cancel: bool) -> None:
    _, factory, identifier, workspace, trash, source = await publication_db(tmp_path, database_factory)
    calls = tmp_path / "probes.jsonl"
    binary = tmp_path / "ffprobe"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, os, socket, sys\n"
            "from pathlib import Path\n"
            "path = Path(sys.argv[-1])\n"
            f"with Path({str(calls)!r}).open('a') as log:\n"
            "    log.write(json.dumps([str(path), os.getpid(), os.getppid()]) + '\\n')\n"
            f"if {cancel!r} and path.suffix == '.part':\n"
            f"    with socket.create_connection({listener.getsockname()!r}, timeout=10) as gate:\n"
            "        gate.sendall(str(os.getppid()).encode())\n"
            "        assert gate.recv(1) == b'x'\n"
            f"print({json.dumps(raw())!r})\n"
        )
        binary.chmod(0o755)
        async with factory() as session:
            task = asyncio.create_task(
                finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin=binary)
            )
            gate = None
            try:
                if cancel:
                    gate, _ = await asyncio.wait_for(asyncio.to_thread(listener.accept), 15)
                    pid = int(await asyncio.wait_for(asyncio.to_thread(gate.recv, 100), 5))
                    for _ in range(2):
                        await _cancel_turn(task)
                        assert not task.done()
                    gate.sendall(b"x")
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 15)
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
                    journal = await session.scalar(select(FinalizationJournal))
                    assert journal is not None and journal.state == "discard_prepared"
                    assert source.read_bytes() == b"old"
                    await recover(session, journal, library_root=tmp_path)
                    assert journal.state == "discard_rolled_back" and not Path(journal.partial_path).exists()
                else:
                    await asyncio.wait_for(task, 15)
                    journal = await session.scalar(select(FinalizationJournal))
                    assert journal is not None and journal.state == "discard_completed"
                    assert source.read_bytes() == b"done"
                probes = [json.loads(line) for line in calls.read_text().splitlines()]
                assert len(probes) == 2
                assert all(Path(path).is_absolute() and parent != os.getpid() for path, _, parent in probes)
            finally:
                if gate is not None:
                    gate.close()
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)


@pytest.mark.asyncio
async def test_completion_probe_finishes(writer_env, tmp_path: Path, monkeypatch) -> None:
    env = writer_env
    job_id = await file_job(env, plan=FFmpegPlan())
    h, connection = hub(env), FakeConn([hello()])
    heartbeated = asyncio.Event()
    heartbeat = h._handle_heartbeat

    async def receive_heartbeat(entry, update):
        await heartbeat(entry, update)
        heartbeated.set()

    monkeypatch.setattr(h, "_handle_heartbeat", receive_heartbeat)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        binary = tmp_path / "ffprobe"
        marker = tmp_path / "probed"
        binary.write_text(
            f"#!{sys.executable}\n"
            "import os, socket\n"
            "from pathlib import Path\n"
            f"marker = Path({str(marker)!r})\n"
            "if not marker.exists():\n"
            "    marker.write_text('started')\n"
            f"    with socket.create_connection({listener.getsockname()!r}, timeout=10) as gate:\n"
            "        gate.sendall(str(os.getppid()).encode())\n"
            "        assert gate.recv(1) == b'x'\n"
            f"print({json.dumps(RAW)!r})\n"
        )
        binary.chmod(0o755)
        h.ffprobe_bin = str(binary)
        completion = None
        gate = None
        serving = await serve(h, connection)
        try:
            assert await h.dispatch_once() == 1
            offer = connection.messages("job")[0]["job"]
            artifact = materialize_offer(h, offer)
            artifact.write_bytes(b"oversized output")
            message = finished(
                job_id,
                offer["attempt_id"],
                offer["log_path"],
                "succeeded",
                output_path=offer["output_path"],
                output_probe=ProbeData.model_validate(offer["probe"]).model_copy(
                    update={"file_path": offer["output_path"]}
                ),
            )
            completion = asyncio.create_task(h._handle_finished(h.entries["node-a"], message))
            gate, _ = await asyncio.wait_for(asyncio.to_thread(listener.accept), 15)
            worker = int(await asyncio.wait_for(asyncio.to_thread(gate.recv, 100), 5))
            assert worker != os.getpid()
            with pytest.raises(BlockingIOError):
                _remove_attempt(env.workspace, job_id, 1)
            connection.push(Heartbeat(cpu_percent=47).model_dump(mode="json"))
            await asyncio.wait_for(heartbeated.wait(), 3)
            async with env.factory() as session:
                current = await get_node(session, "node-a")
                assert current is not None and current.usage is not None and current.usage["cpu_percent"] == 47
            for _ in range(2):
                await _cancel_turn(completion)
                assert not completion.done() and job_id in h.staging.busy_jobs
            gate.sendall(b"x")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(completion, 15)
            assert job_id not in h.staging.busy_jobs
            with pytest.raises(ProcessLookupError):
                os.kill(worker, 0)
            await asyncio.wait_for(h._handle_finished(h.entries["node-a"], message), 15)
            async with env.factory() as session:
                job = await session.get(Job, job_id)
                stage = await session.scalar(
                    select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.type == "stage")
                )
                assert job is not None and job.status == "failed"
                assert stage is not None and stage.payload["output_probe"]["size_bytes"] == len(b"oversized output")
                assert stage.payload["output_probe"]["file_path"] == offer["output_path"]
            assert (env.library / "movie.avi").read_bytes() == b"source"
            assert not (env.library / "movie.mkv").exists()
        finally:
            if gate is not None:
                gate.close()
            if completion is not None and not completion.done():
                completion.cancel()
            connection.disconnect()
            try:
                tasks = [serving, *([completion] if completion is not None else [])]
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
            finally:
                await asyncio.wait_for(h.aclose(), 5)


def _gated_write(address: tuple[str, int], destination: Path) -> None:
    with socket.create_connection(address, timeout=10) as gate:
        gate.sendall(str(os.getpid()).encode())
        if gate.recv(1) != b"x":
            raise RuntimeError("Storage test gate closed.")
        destination.write_bytes(b"finished")


def _storage_controller(address: tuple[str, int], root: Path, active: bool, workspace: bool) -> None:
    async def run() -> None:
        async with library_mutation_lock(root, wait=True, workspace=workspace):
            if active:
                await storage_io(_gated_write, address, root / "written")
            else:
                worker = await storage_io(os.getpid)
                with socket.create_connection(address, timeout=10) as gate:
                    gate.sendall(str(worker).encode())
                    await asyncio.Event().wait()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["writer", "workspace", "idle"])
async def test_parent_death_unlocks(tmp_path: Path, mode: str) -> None:
    active, workspace = mode != "idle", mode == "workspace"
    workspace_root = tmp_path / "workspace"
    root = workspace_root / "jobs/1/attempts/1" if workspace else tmp_path
    root.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        controller = multiprocessing.get_context("spawn").Process(
            target=_storage_controller, args=(listener.getsockname(), root, active, workspace)
        )
        controller.start()
        gate = None
        lock_fd = None
        acquiring = None
        try:
            gate, _ = await asyncio.wait_for(asyncio.to_thread(listener.accept), 15)
            worker = int(await asyncio.wait_for(asyncio.to_thread(gate.recv, 100), 5))
            assert _try_library_lock(root, workspace=workspace) is None
            controller.kill()
            await asyncio.to_thread(controller.join, 5)
            assert not controller.is_alive()
            if active:
                if workspace:
                    with pytest.raises(BlockingIOError):
                        _remove_attempt(workspace_root, 1, 1)
                else:
                    assert _try_library_lock(root) is None
                gate.sendall(b"x")
            lock_fd = os.open(root / ".lock" if workspace else root, os.O_RDWR if workspace else os.O_RDONLY)
            if workspace:
                lock = struct.pack("hhqqi4x", fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
                # Linux F_OFD_SETLKW. This Python build does not expose the name.
                acquiring = asyncio.create_task(asyncio.to_thread(fcntl.fcntl, lock_fd, 38, lock))
            else:
                acquiring = asyncio.create_task(asyncio.to_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX))
            await asyncio.wait_for(asyncio.shield(acquiring), 10)
            if active:
                assert (root / "written").read_bytes() == b"finished"
            if workspace:
                os.close(lock_fd)
                lock_fd = None
                assert _remove_attempt(workspace_root, 1, 1) and not root.exists()
        finally:
            if gate is not None:
                gate.close()
            if controller.is_alive():
                controller.kill()
            await asyncio.to_thread(controller.join, 5)
            controller.close()
            if acquiring is not None and not acquiring.done():
                os.kill(worker, 9)
                await asyncio.wait_for(acquiring, 5)
            if lock_fd is not None:
                os.close(lock_fd)
