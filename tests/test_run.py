from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import run

pytestmark = pytest.mark.usefixtures("logging_state")


@pytest.fixture(autouse=True)
def local_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    for key in (
        "MW_ROLE",
        "MW_DISABLE_JOBS",
        "MW_PORT",
        "MW_HOST",
        "MW_NODE_CONFIG",
        "MW_NODE_SERVER_URL",
        "MW_NODE_WORKSPACE_DIR",
        "MW_NODE_ENROLLMENT_TOKEN",
        "MW_TZ",
        "TZ",
        "IN_CONTAINER",
        "UMASK",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text('node_enrollment_token = "configured-token"\n')

    yield path


class FakeProc:
    def __init__(self, name: str, exit_code: int | None = None) -> None:
        self.name = name
        self.returncode = exit_code
        self.signals: list[int] = []
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.returncode = 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd=self.name, timeout=timeout or 0.0)
        return self.returncode


def test_role_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        run.parse_role("both")


def test_parse_port_range() -> None:
    assert run.parse_port("8000") == 8000
    with pytest.raises(ValueError):
        run.parse_port("0")
    with pytest.raises(ValueError):
        run.parse_port("nope")


def test_wait_healthy_stopped() -> None:
    calls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> Any:
        calls.append(url)
        raise OSError(f"cannot reach {url} after {timeout}s")

    healthy = run.wait_healthy(8000, stopped=lambda: len(calls) == 3, urlopen=fake_urlopen, sleep=lambda _s: None)
    assert healthy is False
    assert len(calls) == 3


def test_wait_healthy_delayed() -> None:
    calls = 0
    elapsed = 0.0

    def probe(url: str, *, timeout: float) -> Any:
        nonlocal calls
        calls += 1
        assert url == "http://127.0.0.1:8000/health"
        assert timeout == run.ATTEMPT_TIMEOUT_S
        assert calls <= 141
        if elapsed < 35:
            raise OSError("Server startup is still running.")
        return contextlib.nullcontext()

    def sleep(delay: float) -> None:
        nonlocal elapsed
        elapsed += delay

    assert run.wait_healthy(8000, stopped=lambda: False, urlopen=probe, sleep=sleep)
    assert elapsed == 35
    assert calls == 141


@pytest.mark.parametrize("ready", [False, True])
def test_health_probe_stop(ready: bool) -> None:
    stopping = False
    sleeps: list[float] = []

    def probe(_url: str, *, timeout: float) -> Any:
        nonlocal stopping
        stopping = True
        if not ready:
            raise OSError("Server stopped during startup.")
        return contextlib.nullcontext()

    assert not run.wait_healthy(8000, stopped=lambda: stopping, urlopen=probe, sleep=sleeps.append)
    assert sleeps == []


def test_startup_server_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeProc("server")
    spawned: list[str] = []

    def spawn(argv: list[str]) -> Any:
        spawned.append(argv[0])
        assert len(spawned) == 1
        return server

    def probe(_url: str, *, timeout: float) -> Any:
        server.returncode = 7
        raise OSError("Server exited during startup.")

    wait_healthy = run.wait_healthy

    def wait(port: int, *, stopped: Callable[[], bool]) -> bool:
        return wait_healthy(port, stopped=stopped, urlopen=probe, sleep=lambda _s: pytest.fail("Server has exited."))

    monkeypatch.setattr(run, "resolve_bin", lambda name: name)
    monkeypatch.setattr(run.subprocess, "Popen", spawn)
    monkeypatch.setattr(run, "wait_healthy", wait)
    assert run.run_all() == 1
    assert spawned == [run.SERVER_BIN]
    assert server.returncode == 7


def test_supervise_forwards_signal() -> None:
    server: Any = FakeProc("server")
    node: Any = FakeProc("node")
    handlers: list[Any] = []
    sleeps: list[int] = []

    def fake_sleep(_s: float) -> None:
        sleeps.append(1)
        if len(sleeps) == 1:
            handlers[0](signal.SIGTERM, None)

    code = run.supervise(server, node, install_signals=handlers.append, sleep=fake_sleep)
    assert code == 0
    assert server.signals == [signal.SIGTERM]
    assert node.signals == [signal.SIGTERM]
    assert not server.terminated
    assert not node.terminated


def test_supervise_stops_sibling() -> None:
    server: Any = FakeProc("server")
    node: Any = FakeProc("node", exit_code=1)
    sleeps: list[int] = []

    code = run.supervise(server, node, install_signals=lambda _h: None, sleep=lambda _s: sleeps.append(1))
    assert code == 1
    assert server.terminated
    assert node.signals == []
    assert sleeps == []


def _fake_exec(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    execs: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(run.os, "execvp", lambda path, argv: execs.append((path, argv)))
    monkeypatch.setattr(run, "resolve_bin", lambda name: f"/bin/{name}")
    return execs


def test_role_arg_precedes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_ROLE", "node")
    execs = _fake_exec(monkeypatch)
    run.main(["--role", "server", "--dev", "--database-url", "postgresql://localhost/test"])
    assert execs == [
        (
            "/bin/millwright-server",
            ["millwright-server", "--host", "0.0.0.0", "--database-url", "postgresql://localhost/test", "--dev"],
        )
    ]


@pytest.mark.parametrize("environment", [False, True])
def test_disabled_jobs_server(monkeypatch: pytest.MonkeyPatch, local_config: Path, environment: bool) -> None:
    if environment:
        monkeypatch.setenv("MW_DISABLE_JOBS", "true")
    else:
        local_config.write_text("disable_jobs = true\n")
    execs = _fake_exec(monkeypatch)
    run.main(["--role", "all", "--port", "9000"])
    assert execs == [("/bin/millwright-server", ["millwright-server", "--host", "0.0.0.0", "--port", "9000"])]


def test_no_migration_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MW_PORT", raising=False)
    execs = _fake_exec(monkeypatch)
    run.main(["--role", "server", "--no-auto-migration"])
    assert execs[0][1][-1] == "--no-auto-migration"

    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, env=None: FakeProc(argv[0]))
    monkeypatch.setattr(run, "wait_healthy", lambda *a, stopped=None: True)
    monkeypatch.setattr(run, "supervise", lambda *a, **k: 0)
    spawns: list[list[str]] = []
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, env=None: (spawns.append(argv), FakeProc(argv[0]))[1])
    assert run.main(["--role", "all", "--no-auto-migration"]) == 0
    assert "--no-auto-migration" in spawns[0]
    assert "--no-auto-migration" not in spawns[1]


def test_no_migration_node_rejected(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run.main(["--role", "node", "--no-auto-migration"]) == 2
    record = json.loads(capsys.readouterr().err)
    assert record["logger"] == "run" and record["level"] == "error"


@pytest.mark.parametrize("role", ["server", "node"])
def test_role_env_default(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    monkeypatch.setenv("MW_ROLE", role)
    execs = _fake_exec(monkeypatch)
    run.main([])
    expected = [f"millwright-{role}"]
    if role == "server":
        expected.extend(["--host", "0.0.0.0"])
    assert execs == [(f"/bin/millwright-{role}", expected)]


@pytest.mark.parametrize("mask,mode", [(None, 0o664), ("0077", 0o600)])
def test_container_file_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mask: str | None, mode: int
) -> None:
    monkeypatch.setenv("IN_CONTAINER", "1")
    if mask is not None:
        monkeypatch.setenv("UMASK", mask)
    monkeypatch.setattr(run, "resolve_bin", lambda name: name)
    output = tmp_path / "created-by-service"
    monkeypatch.setattr(run.os, "execvp", lambda *_args: output.touch())
    previous = os.umask(0o077)
    try:
        run.main(["--role", "server"])
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == mode


@pytest.mark.parametrize("role,blocked", [("server", "config"), ("server", "workspace"), ("node", "workspace")])
def test_container_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_config: Path,
    caplog: pytest.LogCaptureFixture,
    role: str,
    blocked: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    local_config.write_text(f'workspace_dir = "{workspace}"\n')
    (tmp_path / "node.toml").write_text(f'workspace_dir = "{workspace}"\n')
    monkeypatch.setenv("IN_CONTAINER", "1")
    monkeypatch.setenv("MW_NODE_SERVER_URL", "http://server:8000")
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "test-enrollment")
    directory = local_config.parent if blocked == "config" else workspace
    access = os.access
    # CI can run as root, so simulate the OS denial without depending on chmod bypasses.
    monkeypatch.setattr(run.os, "access", lambda path, mode: False if path == directory else access(path, mode))
    execs = _fake_exec(monkeypatch)
    previous = os.umask(0o077)
    try:
        assert run.main(["--role", role]) == 2
    finally:
        os.umask(previous)
    assert execs == []
    message = caplog.records[-1].getMessage()
    owner = directory.stat()
    assert str(directory) in message
    assert f"Running as {os.geteuid()}:{os.getegid()}" in message
    assert f"owned by {owner.st_uid}:{owner.st_gid}" in message
    assert f'Compose user: "{owner.st_uid}:{owner.st_gid}"' in message


def test_token_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MW_NODE_ENROLLMENT_TOKEN", raising=False)
    monkeypatch.delenv("MW_PORT", raising=False)
    monkeypatch.setattr(run, "resolve_bin", lambda name: f"/bin/{name}")
    envs: list[dict[str, str] | None] = []

    def fake_popen(argv: list[str], env: dict[str, str] | None = None) -> Any:
        envs.append(env)
        return FakeProc(argv[0])

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run, "wait_healthy", lambda *a, stopped=None: True)
    monkeypatch.setattr(run, "supervise", lambda *a, **k: 0)
    assert run.run_all() == 0
    assert envs[0] is None
    node_env = envs[1]
    assert node_env is not None and node_env["MW_NODE_ENROLLMENT_TOKEN"] == "configured-token"
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "tok")
    assert run.run_all() == 0
    node_env = envs[3]
    assert node_env is not None and node_env["MW_NODE_ENROLLMENT_TOKEN"] == "tok"


def test_local_enrollment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    monkeypatch.delenv("MW_NODE_ENROLLMENT_TOKEN", raising=False)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "node_enrollment_token").write_text("fleet-token\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text('node_enrollment_token = "file:secrets/node_enrollment_token"\n')
    monkeypatch.setattr(run, "resolve_bin", lambda name: f"/bin/{name}")
    envs = []

    def fake_popen(argv, env=None):
        envs.append(env)
        return FakeProc(argv[0])

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run, "wait_healthy", lambda *a, stopped=None: True)
    monkeypatch.setattr(run, "supervise", lambda *a, **k: 0)
    assert run.run_all() == 0
    assert envs[1]["MW_NODE_ENROLLMENT_TOKEN"] == "fleet-token"


def test_all_role_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MW_PORT", raising=False)
    monkeypatch.setattr(run, "resolve_bin", lambda name: f"/bin/{name}")
    spawns: list[list[str]] = []

    def fake_popen(argv: list[str], env: dict[str, str] | None = None) -> Any:
        spawns.append(argv)
        return FakeProc(argv[0])

    monkeypatch.setattr(run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(run, "wait_healthy", lambda *a, stopped=None: True)
    monkeypatch.setattr(run, "supervise", lambda *a, **k: 0)
    assert run.main(["--role", "all", "--dev", "--no-auto-migration"]) == 0
    assert spawns[0] == [
        "/bin/millwright-server",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--dev",
        "--no-auto-migration",
    ]
    assert spawns[1] == ["/bin/millwright-node"]


def test_dev_drains_children(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path))
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "test-token")
    monkeypatch.setattr(run, "resolve_bin", lambda name: name)
    monkeypatch.setattr(run, "wait_healthy", lambda *args, **kwargs: True)
    children = []
    handlers = {}

    class SlowProc(FakeProc):
        pid = 1

        def send_signal(self, sig: int) -> None:
            self.signals.append(sig)

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                if timeout is not None:
                    raise subprocess.TimeoutExpired(cmd=self.name, timeout=timeout)
                self.returncode = 0
            return self.returncode

    def spawn(argv, env=None):
        child = SlowProc(argv[0])
        children.append(child)
        return child

    def stop(_delay):
        handler = handlers[signal.SIGINT]
        assert callable(handler)
        handler(signal.SIGINT, None)

    original_supervise = run.supervise

    def supervise(*args, **kwargs):
        return original_supervise(*args, sleep=stop, **kwargs)

    monkeypatch.setattr(run.subprocess, "Popen", spawn)
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(run, "supervise", supervise)

    assert run.run_all(["--dev"]) == 0
    assert children and all(child.returncode == 0 for child in children)


def test_node_inherits_server(local_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "shared"
    local_config.write_text(f'workspace_dir = "{root}"\nnode_enrollment_token = "custom-token"\n')
    env = run.node_environment(local_config, port=9000)
    assert env["MW_NODE_WORKSPACE_DIR"] == str(root)
    assert env["MW_NODE_ENROLLMENT_TOKEN"] == "custom-token"
    assert env["MW_NODE_SERVER_URL"] == "http://127.0.0.1:9000"
    monkeypatch.setenv("MW_WORKSPACE_DIR", str(tmp_path / "overridden"))
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "overridden-token")
    env = run.node_environment(local_config, port=9000)
    assert env["MW_NODE_WORKSPACE_DIR"] == str(tmp_path / "overridden")
    assert env["MW_NODE_ENROLLMENT_TOKEN"] == "overridden-token"


def test_node_inherits_timezone(local_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert run.node_environment(local_config, port=8000)["TZ"] == "Europe/Berlin"
    local_config.write_text('tz = "Asia/Kuwait"\n')
    assert run.node_environment(local_config, port=8000)["TZ"] == "Asia/Kuwait"
    monkeypatch.delenv("TZ")
    local_config.write_text("")
    assert run.node_environment(local_config, port=8000)["TZ"] == "UTC"


def test_node_overrides_preserved(local_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "node.toml").write_text(
        'workspace_dir = "/node/workspace"\nenrollment_token = "node-token"\nserver_url = "https://custom"\n'
    )
    env = run.node_environment(local_config, port=8000)
    assert all(key not in env for key in ("MW_NODE_ENROLLMENT_TOKEN", "MW_NODE_WORKSPACE_DIR", "MW_NODE_SERVER_URL"))
    monkeypatch.setenv("MW_NODE_WORKSPACE_DIR", "/explicit/workspace")
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "explicit-token")
    monkeypatch.setenv("MW_NODE_SERVER_URL", "https://explicit")
    env = run.node_environment(local_config, port=8000)
    assert env["MW_NODE_WORKSPACE_DIR"] == "/explicit/workspace"
    assert env["MW_NODE_ENROLLMENT_TOKEN"] == "explicit-token"
    assert env["MW_NODE_SERVER_URL"] == "https://explicit"


def test_explicit_node_config(local_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom-node.toml"
    path.write_text('enrollment_token = "custom-enrollment"\n')
    monkeypatch.setenv("MW_NODE_CONFIG", str(path))
    env = run.node_environment(local_config, port=8000)
    assert "MW_NODE_ENROLLMENT_TOKEN" not in env
    assert env["MW_NODE_SERVER_URL"] == "http://127.0.0.1:8000"


def test_shared_secret_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from node.cli import load_settings

    server_dir = tmp_path / "server"
    server_dir.mkdir()
    path = server_dir / "config.toml"
    path.write_text("")
    (server_dir / "enrollment").write_text("shared-secret\n")
    node_path = tmp_path / "node.toml"
    node_path.write_text('enrollment_token = "file:missing-secret"\n')
    monkeypatch.setenv("MW_NODE_CONFIG", str(node_path))
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "file:enrollment")
    env = run.node_environment(path, port=8000)
    assert env["MW_NODE_ENROLLMENT_TOKEN"] == "shared-secret"
    for key in ("MW_NODE_ENROLLMENT_TOKEN", "MW_NODE_SERVER_URL", "MW_NODE_WORKSPACE_DIR"):
        monkeypatch.setenv(key, env[key])
    settings = load_settings(node_path)
    assert settings.enrollment_token == "shared-secret"
    assert settings.server_url == "ws://127.0.0.1:8000/api/hub"


def test_aio_explicit_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "custom.toml"
    config.write_text(f'workspace_dir = "{tmp_path / "custom-workspace"}"\nnode_enrollment_token = "custom-token"\n')
    monkeypatch.setenv("MW_PORT", "9000")
    monkeypatch.setattr(run, "resolve_bin", lambda name: f"/bin/{name}")
    spawns = []
    monkeypatch.setattr(
        run.subprocess, "Popen", lambda argv, env=None: (spawns.append((argv, env)), FakeProc(argv[0]))[1]
    )
    health_ports = []
    monkeypatch.setattr(run, "wait_healthy", lambda port, **kwargs: (health_ports.append(port), True)[1])
    monkeypatch.setattr(run, "supervise", lambda *args, **kwargs: 0)
    assert run.run_all([f"--config={config}", "--port", "9100"]) == 0
    assert health_ports == [9100]
    assert spawns[1][1]["MW_NODE_SERVER_URL"] == "http://127.0.0.1:9100"
    assert spawns[1][1]["MW_NODE_WORKSPACE_DIR"] == str(tmp_path / "custom-workspace")
    assert spawns[1][1]["MW_NODE_ENROLLMENT_TOKEN"] == "custom-token"


@pytest.mark.parametrize(
    "command,port", [(["python", "/app/run.py"], 9000), (["millwright-server", "--port", "9100"], 9100)]
)
def test_http_liveness(command: list[str], port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_PORT", "9000")
    calls = []

    def probe(url, timeout):
        calls.append((url, timeout))
        return contextlib.nullcontext()

    assert run.healthcheck(command, urlopen=probe) == 0
    assert calls == [(f"http://127.0.0.1:{port}/health", 5)]


def test_node_liveness() -> None:
    def probe(*args, **kwargs):
        pytest.fail("Standalone node liveness must not depend on the server")

    assert run.healthcheck(["/app/.venv/bin/python", "/app/.venv/bin/millwright-node"], urlopen=probe) == 0


def test_liveness_reports_failure(caplog: pytest.LogCaptureFixture) -> None:
    def probe(*args, **kwargs):
        raise OSError("connection refused")

    assert run.healthcheck(["python", "/app/run.py"], urlopen=probe) == 1
    assert any(record.name == "run" and record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.parametrize(
    "args,host,port", [([], "0.0.0.0", 9000), (["--host", "127.0.0.1", "--port", "9100"], "127.0.0.1", 9100)]
)
def test_server_binding_overrides(args: list[str], host: str, port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from server import __main__ as server_main

    monkeypatch.setenv("MW_HOST", "0.0.0.0")
    monkeypatch.setenv("MW_PORT", "9000")
    calls = []
    monkeypatch.setattr(server_main, "start", lambda **kwargs: calls.append(kwargs))
    server_main.main(args)
    assert calls[0]["host"] == host
    assert calls[0]["port"] == port
