from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import socket
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from common.capabilities import (
    CapabilitiesError,
    derive_worker_tags,
    probe_capabilities,
    resolve_mkv_tools,
    verify_encoders,
)
from common.config import apply_env_overrides
from common.dates import Dates
from common.diagnostics import error_message
from common.logging import setup_logging
from common.protocol import NodeCapabilities
from common.secrets import resolve_secret
from pydantic import TypeAdapter, ValidationError

from node.agent import Agent, AgentConfig, safe_endpoint
from node.capabilities import system_facts

if TYPE_CHECKING:
    from collections.abc import Sequence

LOG = logging.getLogger("node.cli")


APP_DIR = "millwright"
CONFIG_NAME = "node.toml"
ENV_CONFIG = "MW_NODE_CONFIG"
ENV_STATE = "MW_NODE_STATE_PATH"

ENV_OVERRIDES = {
    "enrollment_token": "MW_NODE_ENROLLMENT_TOKEN",
    "server_url": "MW_NODE_SERVER_URL",
    "workspace_dir": "MW_NODE_WORKSPACE_DIR",
    "ffmpeg_bin": "MW_NODE_FFMPEG_BIN",
    "ffprobe_bin": "MW_NODE_FFPROBE_BIN",
    "worker_tags": "MW_NODE_WORKER_TAGS",
    "min_free_gb": "MW_NODE_MIN_FREE_GB",
    "hard_size_ceiling": "MW_NODE_HARD_SIZE_CEILING",
    "stall_timeout_s": "MW_NODE_STALL_TIMEOUT_S",
    "heartbeat_s": "MW_NODE_HEARTBEAT_S",
    "backoff_base_s": "MW_NODE_BACKOFF_BASE_S",
    "backoff_max_s": "MW_NODE_BACKOFF_MAX_S",
}
_KNOWN_KEYS = {*AgentConfig.__dataclass_fields__, "worker_tags"} - {
    "node_id",
    "capabilities",
    "mkv_tools",
    "token",
    "credentials_path",
}

_NODE_ID_RE = re.compile(r"node-[0-9a-f]{16}\Z")

_CONFIG_ADAPTER = TypeAdapter(AgentConfig)


def _build_capabilities(config: AgentConfig, caps: Any, facts: Any, encoders: frozenset[str]) -> NodeCapabilities:
    return NodeCapabilities(
        max_transcodes=1,
        hostname=socket.gethostname(),
        ffmpeg_version=caps.ffmpeg_version,
        encoders=sorted(encoders),
        video_encoders=sorted(caps.video_encoders & encoders),
        audio_encoders=sorted(caps.audio_encoders & encoders),
        decoders=sorted(caps.decoders),
        audio_decoders=sorted(caps.audio_decoders),
        hwaccels=sorted(caps.hwaccels),
        worker_tags=sorted({*config.capabilities.worker_tags, *derive_worker_tags(encoders)}),
        cpu_model=facts.cpu_model,
        cpu_cores=facts.cpu_cores,
        memory_gb=facts.memory_gb,
        gpus=facts.gpus,
    )


def default_config_path() -> Path:
    if env := os.environ.get(ENV_CONFIG):
        return Path(env).expanduser()
    if override := os.environ.get("MW_CONFIG_PATH"):
        return Path(override).expanduser() / CONFIG_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / APP_DIR / CONFIG_NAME


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"invalid TOML in {path}: {exc}"
        raise ValueError(msg) from exc
    return data


def _credentials_path() -> Path:
    if override := os.environ.get(ENV_STATE) or os.environ.get("MW_STATE_PATH"):
        return Path(override).expanduser() / "node-credentials.json"
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base).expanduser() / APP_DIR / "node-credentials.json"


def _load_credentials() -> tuple[str, str] | None:
    path = _credentials_path()
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        node_id, token = data["node_id"], data["token"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        msg = "cannot access node credentials"
        raise ValueError(msg) from exc
    if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id) or not isinstance(token, str) or not token:
        msg = "invalid node credentials in node state file"
        raise ValueError(msg)
    return node_id, token


def _hub_url(value: str) -> str:
    setting = "server_url (MW_NODE_SERVER_URL)"
    value = value.strip()
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        msg = f"Invalid {setting}. Use a server URL such as http://server:8000 or https://server."
        raise ValueError(msg) from None
    schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    if url.scheme not in schemes:
        msg = f"{setting} must use http://, https://, ws://, or wss://."
        raise ValueError(msg)
    if not url.hostname or any(char.isspace() for char in value):
        msg = f"{setting} needs a valid server hostname or IP address, with no whitespace."
        raise ValueError(msg)
    if port == 0:
        msg = f"{setting} must use a port between 1 and 65535."
        raise ValueError(msg)
    if url.fragment:
        msg = f"{setting} must not contain a URL fragment."
        raise ValueError(msg)
    if url.path.rstrip("/") not in ("", "/api/hub"):
        msg = f"{setting} must be a bare server URL or end in /api/hub. Use https://server or wss://server/api/hub."
        raise ValueError(msg)
    return urlunsplit((schemes[url.scheme], url.netloc, "/api/hub", url.query, ""))


def _build_settings(data: dict[str, Any], path: Path) -> AgentConfig:
    for key in data:
        if key not in _KNOWN_KEYS:
            LOG.warning("ignoring unknown config key %s in %s", key, path)
    apply_env_overrides(data, ENV_OVERRIDES, json_fields={"worker_tags"})
    data.pop("node_id", None)
    data.pop("max_transcodes", None)
    data.pop("credentials_path", None)
    data.pop("token", None)
    if isinstance(data.get("enrollment_token"), str):
        data["enrollment_token"] = resolve_secret(data["enrollment_token"], base_dir=path.parent)
    credentials = _load_credentials()
    if credentials is not None:
        data["node_id"], data["token"] = credentials
    else:
        data["token"] = data.get("enrollment_token")
    missing = ", ".join(
        f"{key} (config key {key!r} or env {ENV_OVERRIDES[key]})"
        for key in ("enrollment_token", "server_url", "workspace_dir")
        if not data.get(key) and (key != "enrollment_token" or credentials is None)
    )
    if missing:
        msg = f"Node settings incomplete ({path}). Missing: {missing}"
        raise ValueError(msg)
    worker_tags = data.pop("worker_tags", [])
    # Only tags are configurable. Other capabilities are probed at startup.
    data["capabilities"] = {"worker_tags": worker_tags}
    data["credentials_path"] = _credentials_path()
    try:
        config = _CONFIG_ADAPTER.validate_python(data)
    except ValidationError as exc:
        details = ". ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']} [{error['type']}]"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        msg = f"invalid node config in {path}: {details}"
        raise ValueError(msg) from None
    config.server_url = _hub_url(config.server_url)
    root = Path(config.workspace_dir).resolve()
    for critical in (path, _credentials_path()):
        critical = critical.resolve()
        if critical.is_relative_to(root) or root.is_relative_to(critical):
            msg = f"node workspace must be separate from config and credentials files: {critical}"
            raise ValueError(msg)
    return config


def load_settings(path: Path, *, must_exist: bool = False) -> AgentConfig:
    if path.is_file():
        return _build_settings(_read_toml(path), path)
    if must_exist:
        msg = f"config file not found: {path}"
        raise ValueError(msg)
    return _build_settings({}, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m node", description="Start the transcode node agent", allow_abbrev=False
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Config file (default: ${ENV_CONFIG}, then $MW_CONFIG_PATH/{CONFIG_NAME} or ~/.config/{APP_DIR}/{CONFIG_NAME})",
    )
    return parser


async def _amain(args: argparse.Namespace, dates: Dates) -> int:
    path = args.config or default_config_path()
    explicit = args.config is not None or ENV_CONFIG in os.environ
    try:
        config = load_settings(path, must_exist=explicit)
    except ValueError as exc:
        LOG.error("Could not load node configuration from %s", path, extra={"error": error_message(exc)})
        return 2

    try:
        caps = await probe_capabilities(config.ffmpeg_bin)
    except (CapabilitiesError, OSError) as exc:
        LOG.error(
            "Could not inspect FFmpeg capabilities using %r", config.ffmpeg_bin, extra={"error": error_message(exc)}
        )
        return 2
    facts = await system_facts()
    encoders = await verify_encoders(config.ffmpeg_bin, caps.encoders)
    config.capabilities = _build_capabilities(config, caps, facts, encoders)
    config.mkv_tools = resolve_mkv_tools()

    agent = Agent(config, dates=dates)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handlers = {}
    run_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    error: BaseException | None = None
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            loop.add_signal_handler(sig, stop.set)
            handlers[sig] = previous
        LOG.info(
            "Starting node %s. Hub endpoint: %s. Worker tags: %s",
            config.node_id or "awaiting enrollment",
            safe_endpoint(config.server_url),
            ", ".join(config.capabilities.worker_tags) or "none",
        )
        run_task = asyncio.create_task(agent.run(), name="agent-run")
        stop_task = asyncio.create_task(stop.wait())
        await asyncio.wait((run_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if run_task.done():
            run_task.result()
    except BaseException as exc:
        error = exc
    finally:

        async def cleanup() -> None:
            close_error: BaseException | None = None
            try:
                await agent.aclose()
            except BaseException as exc:
                close_error = exc
            try:
                if run_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await run_task
            except BaseException as exc:
                if close_error is not None:
                    raise close_error from exc
                raise
            finally:
                if stop_task is not None:
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                for sig, previous in handlers.items():
                    loop.remove_signal_handler(sig)
                    signal.signal(sig, previous)
            if close_error is not None:
                raise close_error

        cleanup_task = asyncio.create_task(cleanup(), name="agent-cleanup")
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            cleanup_task.result()
        except BaseException as exc:
            if error is None:
                raise
            LOG.error("Could not finish node shutdown", exc_info=exc)
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    timezone = (os.environ.get("TZ") or "UTC").strip() or "UTC"
    dates = Dates(timezone)
    setup_logging(timezone=dates)
    try:
        result = asyncio.run(_amain(args, dates))
    except Exception:
        LOG.exception("Node stopped unexpectedly.")
        raise SystemExit(1) from None
    raise SystemExit(result)
