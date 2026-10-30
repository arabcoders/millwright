from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import time
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from common.logging import setup_logging

SERVER_BIN = "millwright-server"
NODE_BIN = "millwright-node"
ROLES = ("server", "node", "all")
DEFAULT_ROLE = "all"
SERVER_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
HEALTH_INTERVAL_S = 0.25
ATTEMPT_TIMEOUT_S = 2.0
JOIN_TIMEOUT_S = 10.0
SUPERVISE_POLL_S = 0.5

Child = subprocess.Popen[Any]
LOG = logging.getLogger("run")
Sleeper = Callable[[float], None]
SignalHandler = Callable[[int, object], None]
SignalInstaller = Callable[[SignalHandler], None]
Urlopen = Callable[..., Any]


def parse_role(raw: str | None) -> str:
    role = (raw or DEFAULT_ROLE).strip().lower()
    if role not in ROLES:
        allowed = ", ".join(ROLES)
        msg = f"Role must be one of {allowed}. Got {raw!r} (--role or $MW_ROLE)"
        raise ValueError(msg)
    return role


def parse_port(raw: str) -> int:
    port = int(raw)
    if not 1 <= port <= 65535:
        msg = f"Port must be 1-65535. Got {raw!r}"
        raise ValueError(msg)
    return port


def parse_umask(raw: str) -> int:
    msg = "UMASK must be an octal file creation mask between 0000 and 0777."
    try:
        mask = int(raw, 8)
    except ValueError:
        raise ValueError(msg) from None
    if not 0 <= mask <= 0o777:
        raise ValueError(msg)
    return mask


def resolve_bin(name: str) -> str:
    if (path := shutil.which(name)) is None:
        msg = f"{name} not found on PATH"
        raise ValueError(msg)
    return path


def check_directory(path: Path) -> None:
    # Config and state subdirectories are created by their services on first startup.
    directory = path
    while not directory.exists():
        directory = directory.parent
    if os.access(directory, os.W_OK | os.X_OK):
        return
    owner = directory.stat()
    msg = (
        f"Cannot write to {path}. Running as {os.geteuid()}:{os.getegid()}. "
        f"Directory {directory} is owned by {owner.st_uid}:{owner.st_gid}. "
        f'Set Compose user: "{owner.st_uid}:{owner.st_gid}" to match the directory owner, '
        "or grant your chosen user access to the mounted directory."
    )
    raise ValueError(msg)


def wait_healthy(
    port: int,
    *,
    stopped: Callable[[], bool],
    urlopen: Urlopen = urllib.request.urlopen,
    sleep: Sleeper = time.sleep,
    interval_s: float = HEALTH_INTERVAL_S,
) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    attempt = 0
    # Recovery can outlast a fixed deadline. The launcher still owns stop signals and child exits.
    while not stopped():
        attempt += 1
        try:
            with urlopen(url, timeout=ATTEMPT_TIMEOUT_S):
                return not stopped()
        except OSError:
            LOG.debug("launcher.healthcheck.failed", extra={"event": "launcher.healthcheck.failed", "attempt": attempt})
        if stopped():
            return False
        sleep(interval_s)
    return False


def send_stop(children: tuple[Child, ...], signum: int) -> None:
    error: BaseException | None = None
    for child in children:
        try:
            if child.poll() is None:
                child.send_signal(signum)
        except ProcessLookupError as exc:
            if child.poll() is None and error is None:
                error = exc
        except BaseException as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error


def reap(child: Child, timeout: float | None) -> int:
    started = time.monotonic()
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        LOG.warning(
            "Killing child process pid %s after %.1fs",
            child.pid,
            time.monotonic() - started,
            extra={"event": "launcher.child.kill", "pid": child.pid, "duration_s": time.monotonic() - started},
        )
        error: BaseException | None = None
        try:
            child.kill()
        except ProcessLookupError as exc:
            if child.poll() is None:
                error = exc
        except BaseException as exc:
            error = exc
        try:
            code = child.wait(timeout=timeout)
        except BaseException as exc:
            if error is not None:
                raise error from exc
            raise
        if error is not None:
            raise error from expired
        LOG.warning(
            "Child process pid %s was killed. Return code %s",
            child.pid,
            code,
            extra={"event": "launcher.child.killed", "pid": child.pid, "returncode": code},
        )
        return code


def stop_children(children: tuple[Child, ...], timeout: float | None) -> list[int]:
    error: BaseException | None = None
    try:
        send_stop(children, signal.SIGTERM)
    except BaseException as exc:
        error = exc
    codes = []
    for child in children:
        try:
            codes.append(reap(child, timeout))
        except BaseException as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error
    return codes


def supervise(
    server: Child,
    node: Child,
    *,
    install_signals: SignalInstaller,
    sleep: Sleeper = time.sleep,
    join_timeout_s: float | None = JOIN_TIMEOUT_S,
    poll_s: float = SUPERVISE_POLL_S,
    stopped: Callable[[], bool] | None = None,
) -> int:
    stopping = False

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        LOG.info("Received %s. Forwarding to server and node.", signal.Signals(signum).name)
        send_stop((server, node), signum)

    expected = False
    try:
        install_signals(on_signal)
        while True:
            if stopping or (stopped is not None and stopped()):
                expected = True
                break
            for name, child in (("server", server), ("node", node)):
                if (code := child.poll()) is not None:
                    reason = f"signal {signal.Signals(-code).name} ({-code})" if code < 0 else f"exit code {code}"
                    LOG.error(
                        "The %s process exited unexpectedly with %s. Stopping the other.",
                        name,
                        reason,
                        extra={"event": "launcher.child.exit", "role": name, "returncode": code},
                    )
                    other = node if child is server else server
                    try:
                        if other.poll() is None:
                            other.terminate()
                    except ProcessLookupError:
                        if other.poll() is None:
                            raise
                    break
            else:
                sleep(poll_s)
                continue
            break
    finally:
        codes = stop_children((server, node), join_timeout_s)
    return max(codes) if expected else 1


def _install_signals(handler: SignalHandler) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def node_environment(config_path: Path, *, port: int) -> dict[str, str]:
    from node.cli import default_config_path
    from server.core.config import load_config
    from server.core.paths import workspace_dir

    config = load_config(config_path)
    path = default_config_path()
    settings = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    env = dict(os.environ)
    env["TZ"] = config.tz
    defaults = {
        "server_url": f"http://127.0.0.1:{port}",
        "workspace_dir": str(workspace_dir(config).resolve()),
    }
    for key, value in defaults.items():
        var = f"MW_NODE_{key.upper()}"
        if var not in env and key not in settings:
            env[var] = value
    return env


def healthcheck(command: Sequence[str], *, urlopen: Urlopen = urllib.request.urlopen) -> int:
    # A standalone node has no HTTP endpoint. Its PID 1 is the node process,
    # so reading that command confirms it is running without requiring registration.
    if any(Path(part).name == NODE_BIN for part in command[:2]):
        return 0
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--port", default=os.environ.get("MW_PORT", str(DEFAULT_PORT)))
    args, _ = parser.parse_known_args(command)
    port = parse_port(args.port)
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5):
            return 0
    except OSError as exc:
        LOG.error("Server liveness check failed on port %s. %s", port, exc)
        return 1


def run_all(passthrough: Sequence[str] = ()) -> int:
    from server.core.paths import config_file

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", default=os.environ.get("MW_PORT", str(DEFAULT_PORT)))
    parser.add_argument("--host", default=os.environ.get("MW_HOST", SERVER_HOST))
    args, _ = parser.parse_known_args(passthrough)
    port = parse_port(args.port)
    LOG.info("Starting the server for the all role on %s:%s.", args.host, port)
    children: list[Child] = []
    stopping = False
    signal_error: BaseException | None = None
    # Killing the dev watcher would orphan its detached server during cleanup.
    join_timeout = None if "--dev" in passthrough else JOIN_TIMEOUT_S
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal stopping, signal_error
        stopping = True
        LOG.info("Received %s. Stopping acquired children.", signal.Signals(signum).name)
        try:
            send_stop(tuple(children), signum)
        except BaseException as exc:
            if signal_error is None:
                signal_error = exc

    try:
        _install_signals(on_signal)
        if stopping:
            return 1
        server_bin = resolve_bin(SERVER_BIN)
        if stopping:
            return 1
        server = subprocess.Popen([server_bin, "--host", args.host, "--port", str(port), *passthrough])
        children.append(server)
        if stopping:
            return 1
        if not wait_healthy(port, stopped=lambda: stopping or server.poll() is not None) or stopping:
            if stopping:
                return 1
            code = server.poll()
            if code is not None:
                reason = f"signal {signal.Signals(-code).name} ({-code})" if code < 0 else f"exit code {code}"
                LOG.error(
                    "Server exited before becoming healthy with %s.",
                    reason,
                    extra={"event": "launcher.child.exit", "role": "server", "returncode": code},
                )
            return 1
        env = node_environment(config_file(args.config), port=port)
        LOG.info("Server is healthy. Starting the node.")
        node_bin = resolve_bin(NODE_BIN)
        if stopping:
            return 1
        node = subprocess.Popen([node_bin], env=env)
        children.append(node)
        if stopping:
            return 1
        return supervise(
            server,
            node,
            install_signals=lambda _handler: None,
            stopped=lambda: stopping,
            join_timeout_s=join_timeout,
        )
    finally:
        try:
            try:
                stop_children(tuple(children), join_timeout)
            except BaseException as exc:
                if signal_error is not None:
                    raise signal_error from exc
                raise
            if signal_error is not None:
                raise signal_error
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Run the Millwright server, node, or both.",
        allow_abbrev=False,
    )
    parser.add_argument("--role", default=None, help="server, node, or all (default: $MW_ROLE, then all)")
    parser.add_argument("--healthcheck", action="store_true", help="Check container process or HTTP liveness")
    parser.add_argument("--dev", action="store_true", help="Usefel for development to reload on file changes")
    parser.add_argument(
        "--no-auto-migration", action="store_true", help="Require the database to be at the current migration"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, extra = build_parser().parse_known_args(argv)
    passthrough = [*extra]
    if args.dev:
        passthrough.append("--dev")
    try:
        role = parse_role(args.role or os.environ.get("MW_ROLE"))
        container_start = os.environ.get("IN_CONTAINER") == "1" and not args.healthcheck
        if container_start:
            os.umask(parse_umask(os.environ.get("UMASK", "0002")))
        timezone = (os.environ.get("TZ") or "UTC").strip() or "UTC"
        if role != "node":
            from server.core.config import ensure_config, load_config
            from server.core.paths import (
                cache_dir,
                config_file,
                state_dir,
                trash_dir,
                uploaded_plugins_dir,
                workspace_dir,
            )

            parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
            parser.add_argument("--config", type=Path)
            settings, _ = parser.parse_known_args(passthrough)
            path = config_file(settings.config)
            if container_start:
                check_directory(path.parent)
            ensure_config(path)
            config = load_config(path)
            if container_start:
                for directory in (
                    state_dir(),
                    cache_dir(),
                    workspace_dir(config),
                    trash_dir(config),
                    uploaded_plugins_dir(config),
                ):
                    check_directory(directory)
            timezone = config.tz
            if config.disable_jobs and role == "all":
                role = "server"
        elif container_start:
            from node.cli import default_config_path, load_settings

            parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
            parser.add_argument("--config", type=Path)
            settings, _ = parser.parse_known_args(passthrough)
            node_config = load_settings(
                settings.config or default_config_path(),
                must_exist=settings.config is not None or "MW_NODE_CONFIG" in os.environ,
            )
            if node_config.credentials_path is not None:
                check_directory(node_config.credentials_path.parent)
            check_directory(Path(node_config.workspace_dir))
        setup_logging(timezone=timezone)
        if args.healthcheck:
            command = Path("/proc/1/cmdline").read_text().split("\0")
            return healthcheck(command)
        if args.no_auto_migration:
            if role == "node":
                LOG.error("--no-auto-migration is a server-only option. The node role cannot use it.")
                return 2
            passthrough.append("--no-auto-migration")
        if role == "all":
            return run_all(passthrough)
        name = SERVER_BIN if role == "server" else NODE_BIN
        path = resolve_bin(name)
        if role == "server":
            passthrough = ["--host", os.environ.get("MW_HOST", SERVER_HOST), *passthrough]
        LOG.info("Starting %s for the %s role.", name, role)
        os.execvp(path, [name, *passthrough])  # noqa: S606
    except ValueError as exc:
        LOG.error("%s", exc)
    except Exception:
        LOG.exception("Launcher failed.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
