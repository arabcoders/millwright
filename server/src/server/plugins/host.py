from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import watchfiles
from common.diagnostics import error_message
from common.fs.acquire import DIRECTORY_FLAGS, _check_entry, _check_parent, directory_fd
from common.process import create_subprocess_exec, drain
from pydantic import BaseModel, ValidationError

from server.plugins.manifest import PluginManifest, load_manifest
from server.plugins.migrations import find_manifest
from server.plugins.upload import (
    PluginSnapshot,
    directory_manifest,
    move_plugin_directory,
    plugin_snapshot,
    remove_plugin_directory,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

LOG = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]

PluginStateName = Literal["ready", "stopped", "disabled"]
PluginSource = Literal["bundled", "uploaded"]


class EvaluatedDecision(BaseModel):
    type: Literal["match", "veto", "skip", "plan", "actions", "error"]
    facets: dict[str, str | int | float | bool | list[str] | list[int]] | None = None
    reason: str | None = None
    plan: dict[str, Any] | None = None
    actions: list[dict[str, Any]] | None = None
    error: str | None = None


@dataclass(kw_only=True, slots=True)
class PluginInfo:
    manifest: PluginManifest
    state: PluginStateName
    source: PluginSource


@dataclass
class _PluginState:
    dir: Path
    manifest: PluginManifest
    runner: asyncio.subprocess.Process | None = None
    fields: list[dict[str, Any]] | None = None
    stopped: bool = False
    retiring: bool = False
    exit_unconfirmed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    etype = payload.get("type")
    message = payload.get("message")
    return f"{etype}: {message}" if etype else str(message)


async def _kill_runner(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        msg = f"runner pid {proc.pid} exit is unconfirmed"
        raise RuntimeError(msg) from None


class PluginHost:
    def __init__(
        self,
        plugins_dirs: list[Path],
        *,
        uploaded_dir: Path | None = None,
        disabled: Iterable[str] = (),
        decide_timeout_s: float = 10.0,
    ) -> None:
        dirs = [Path(path) for path in plugins_dirs]
        self._uploaded_dir = Path(uploaded_dir) if uploaded_dir is not None else None
        if self._uploaded_dir is not None and self._uploaded_dir not in dirs:
            dirs.append(self._uploaded_dir)
        # Created eagerly so the watcher covers uploads into a fresh install.
        if self._uploaded_dir is not None:
            self._uploaded_dir.mkdir(parents=True, exist_ok=True)
        self._plugins_dirs = dirs
        self._disabled = set(disabled)
        self._decide_timeout_s = decide_timeout_s
        # Serializes plugin installs across concurrent upload requests.
        self.upload_lock = asyncio.Lock()
        self._plugins: dict[str, _PluginState] = {}
        self._invalid: dict[str, str] = {}
        self._closed = False
        self._watch_task: asyncio.Task[None] | None = None
        self.on_change: Callable[[], Awaitable[None]] | None = None
        self._close_task: asyncio.Task[BaseException | None] | None = None
        self._stop_watching: threading.Event | None = None
        self._discover()

    def _discover(self) -> None:
        for root in self._plugins_dirs:
            if not root.is_dir():
                LOG.info("Plugin directory does not exist: %s", root)
                continue
            for entry in sorted(root.iterdir()):
                # Dot-prefixed entries are upload staging (.partial/.replaced)
                # or reset leftovers, never plugin directories.
                if entry.name.startswith("."):
                    continue
                if (entry / "plugin.toml").is_file():
                    self._register(entry)

    def _register(self, plugin_dir: Path) -> None:
        previous = next((s for s in self._plugins.values() if s.dir == plugin_dir), None)
        try:
            manifest = load_manifest(plugin_dir)
        except Exception as exc:
            self._invalid[plugin_dir.name] = f"{type(exc).__name__}: {exc}"
            if previous is not None:
                self._plugins.pop(previous.manifest.id, None)
            LOG.warning("invalid plugin in %s", plugin_dir, extra={"error": str(exc)})
            return
        self._invalid.pop(plugin_dir.name, None)
        if previous is not None and previous.manifest.id != manifest.id:
            self._plugins.pop(previous.manifest.id, None)
        self._plugins[manifest.id] = _PluginState(dir=plugin_dir, manifest=manifest)

    @property
    def invalid_plugins(self) -> dict[str, str]:
        return dict(self._invalid)

    def describe(self, plugin_id: str) -> PluginManifest | None:
        state = self._plugins.get(plugin_id)
        return state.manifest if state is not None else None

    def plugins(self) -> list[PluginInfo]:
        return [
            PluginInfo(manifest=state.manifest, state=self._state_of(state), source=self._source_of(state))
            for state in self._plugins.values()
        ]

    def _state_of(self, state: _PluginState) -> PluginStateName:
        if state.manifest.id in self._disabled:
            return "disabled"
        if state.stopped:
            return "stopped"
        return "ready"

    def _source_of(self, state: _PluginState) -> PluginSource:
        if self._uploaded_dir is not None and state.dir.is_relative_to(self._uploaded_dir):
            return "uploaded"
        return "bundled"

    @property
    def uploaded_dir(self) -> Path | None:
        return self._uploaded_dir

    @property
    def unconfirmed_exit(self) -> bool:
        return any(state.exit_unconfirmed for state in self._plugins.values())

    def apply_disabled(self, plugin_ids: Iterable[str]) -> None:
        manifests = [state.manifest for state in self._plugins.values()]
        disabled: set[str] = set()
        for plugin_id in plugin_ids:
            manifest = find_manifest(plugin_id, manifests)
            disabled.add(manifest.id if manifest is not None else plugin_id)
        self._disabled = disabled

    async def reset_uploaded(self) -> None:
        root = self._uploaded_dir
        if root is None:
            return
        if not root.exists() and not root.is_symlink():
            return
        uploaded = [state for state in self._plugins.values() if state.dir.parent == root]
        with directory_fd(root) as root_fd:
            for state in uploaded:
                name = state.dir.name
                plugin_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
                try:
                    snapshot = plugin_snapshot(plugin_fd)
                    if directory_manifest(plugin_fd) != state.manifest or name != state.manifest.id:
                        raise ValueError("uploaded plugin registration changed before reset")
                    _check_entry(state.dir, root_fd, plugin_fd)
                    _check_parent(state.dir, root_fd)
                    await self._stop_runner(state)

                    def remove(
                        plugin_fd: int = plugin_fd, snapshot: PluginSnapshot = snapshot, name: str = name
                    ) -> None:
                        if plugin_snapshot(plugin_fd) != snapshot:
                            raise ValueError("uploaded plugin generation changed before reset")
                        replacement = f".{name}.reset"
                        move_plugin_directory(root, root_fd, name, replacement, plugin_fd)
                        remove_plugin_directory(root, root_fd, replacement, plugin_fd, snapshot)

                    async def reset(state: _PluginState = state, name: str = name) -> None:
                        await asyncio.to_thread(remove)
                        if self._plugins.get(state.manifest.id) is state:
                            self._plugins.pop(state.manifest.id)
                        self._invalid.pop(name, None)

                    await drain(asyncio.create_task(reset()))
                finally:
                    os.close(plugin_fd)

    async def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        state = self._plugins.get(plugin_id)
        if state is None:
            return
        if enabled:
            self._disabled.discard(plugin_id)
        else:
            self._disabled.add(plugin_id)
        await self._stop_runner(state)
        if enabled:
            self._register(state.dir)
            if self.on_change is not None:
                await self.on_change()

    async def stop(self, plugin_id: str) -> None:
        state = self._plugins.get(plugin_id)
        if state is None or plugin_id in self._disabled:
            return
        state.stopped = True
        await self._stop_runner(state)

    async def restart(self, plugin_id: str) -> None:
        state = self._plugins.get(plugin_id)
        if state is None:
            return
        await self._stop_runner(state)
        self._register(state.dir)
        if self.on_change is not None:
            await self.on_change()

    async def hot_load(self, plugin_dir: Path) -> PluginManifest | None:
        previous = next((s for s in self._plugins.values() if s.dir == plugin_dir), None)
        if previous is not None:
            await self._stop_runner(previous)
        self._register(plugin_dir)
        current = next((s for s in self._plugins.values() if s.dir == plugin_dir), None)
        if current is not None:
            return current.manifest
        return None

    def _lifecycle_block(self, plugin_id: str, state: _PluginState) -> str | None:
        # Waiters can't acquire runners on a superseded registration.
        if self._closed:
            return f"plugin host closed: {plugin_id}"
        if plugin_id in self._disabled:
            return f"plugin disabled: {plugin_id}"
        if state.stopped:
            return f"plugin stopped: {plugin_id}"
        if state.exit_unconfirmed:
            return f"plugin runner exit is unconfirmed: {plugin_id}"
        if state.retiring:
            return f"plugin runner is stopping: {plugin_id}"
        if self._plugins.get(plugin_id) is not state:
            return f"plugin reloaded: {plugin_id}"
        return None

    async def describe_inputs(self, plugin_id: str) -> list[dict[str, Any]] | None:
        state = self._plugins.get(plugin_id)
        if state is None or plugin_id in self._disabled:
            return None
        if state.fields is not None:
            return state.fields
        if state.stopped:
            # A stopped plugin stays stopped until restart. No runner spawn.
            return None
        async with state.lock:
            if self._lifecycle_block(plugin_id, state) is not None:
                return None
            if state.fields is None:
                state.fields = await self._describe_fields(state)
            return state.fields

    async def validate_inputs(self, plugin_id: str, inputs: dict[str, Any]) -> list[str]:
        state = self._plugins.get(plugin_id)
        if state is None:
            return [f"cannot validate inputs: unknown plugin: {plugin_id}"]
        async with state.lock:
            blocked = self._lifecycle_block(plugin_id, state)
            if blocked is not None:
                return [f"cannot validate inputs: {blocked}"]
            errors = await self._validate_inputs(state, inputs)
            if errors:
                LOG.warning("Plugin %s input validation failed", plugin_id, extra={"error": ". ".join(errors)})
            return errors

    async def _validate_inputs(self, state: _PluginState, inputs: dict[str, Any]) -> list[str]:
        runner = state.runner
        if runner is None or runner.returncode is not None:
            try:
                runner = await self._acquire_runner(state)
            except OSError as exc:
                return [f"cannot validate inputs: runner spawn failed: {exc}"]
            state.runner = runner
        try:
            line = await asyncio.wait_for(
                self._roundtrip(runner, {"op": "validate_inputs", "inputs": inputs}), self._decide_timeout_s
            )
        except asyncio.CancelledError:
            try:
                await self._stop_runner(state)
            except Exception:
                LOG.exception("could not stop cancelled plugin runner %s", state.manifest.id)
            raise
        except TimeoutError:
            await self._stop_runner(state)
            return [f"cannot validate inputs: timed out after {self._decide_timeout_s}s"]
        except (ConnectionError, ValueError) as exc:
            await self._stop_runner(state)
            return [f"cannot validate inputs: runner protocol read failed: {error_message(exc)}"]
        if not line:
            await self._stop_runner(state)
            return [f"cannot validate inputs: runner exited before responding. Exit code: {runner.returncode}."]
        try:
            response = json.loads(line)
        except ValueError as exc:
            await self._stop_runner(state)
            return [f"cannot validate inputs: runner sent invalid JSON: {error_message(exc)}"]
        if not isinstance(response, dict):
            await self._stop_runner(state)
            return ["cannot validate inputs: runner response must be a JSON object"]
        if "error" in response:
            return [_error_text(response["error"])]
        if response.get("valid") is not True:
            await self._stop_runner(state)
            return ["cannot validate inputs: runner response has neither valid nor error"]
        return []

    async def _describe_fields(self, state: _PluginState) -> list[dict[str, Any]] | None:
        runner = state.runner
        if runner is None or runner.returncode is not None:
            try:
                runner = await self._acquire_runner(state)
            except OSError as exc:
                LOG.warning(
                    "Plugin %s description runner could not start",
                    state.manifest.id,
                    extra={"error": error_message(exc)},
                )
                return None
            state.runner = runner
        try:
            line = await asyncio.wait_for(self._roundtrip(runner, {"op": "describe"}), self._decide_timeout_s)
        except asyncio.CancelledError:
            try:
                await self._stop_runner(state)
            except Exception:
                LOG.exception("could not stop cancelled plugin runner %s", state.manifest.id)
            raise
        except (TimeoutError, ConnectionError, ValueError) as exc:
            LOG.warning(
                "Plugin %s description request failed (timeout: %s seconds)",
                state.manifest.id,
                self._decide_timeout_s,
                extra={"error": error_message(exc)},
            )
            await self._stop_runner(state)
            return None
        if not line:
            await self._stop_runner(state)
            LOG.warning(
                "Plugin %s description runner exited without a response. Exit code: %s",
                state.manifest.id,
                runner.returncode,
            )
            return None
        try:
            response = json.loads(line)
        except ValueError as exc:
            await self._stop_runner(state)
            LOG.warning(
                "Plugin %s description contains invalid JSON", state.manifest.id, extra={"error": error_message(exc)}
            )
            return None
        fields = response.get("inputs") if isinstance(response, dict) else None
        if not isinstance(fields, list):
            reason = (
                _error_text(response["error"])
                if isinstance(response, dict) and "error" in response
                else "runner response has no inputs list"
            )
            LOG.warning("Plugin %s description failed", state.manifest.id, extra={"error": reason})
            # Unread output can belong to this request rather than the next one.
            await self._stop_runner(state)
            return None
        return fields

    async def evaluate(self, plugin_id: str, ctx: dict[str, Any], inputs: dict[str, Any]) -> EvaluatedDecision:
        state = self._plugins.get(plugin_id)
        if state is None:
            return EvaluatedDecision(type="error", error=f"unknown plugin: {plugin_id}")
        if plugin_id in self._disabled:
            return EvaluatedDecision(type="error", error=f"plugin disabled: {plugin_id}")
        if state.stopped:
            return EvaluatedDecision(type="error", error=f"plugin stopped: {plugin_id}")
        async with state.lock:
            blocked = self._lifecycle_block(plugin_id, state)
            if blocked is not None:
                return EvaluatedDecision(type="error", error=blocked)
            return await self._evaluate_once(state, ctx, inputs)

    async def _evaluate_once(
        self, state: _PluginState, ctx: dict[str, Any], inputs: dict[str, Any]
    ) -> EvaluatedDecision:
        outcome, payload = await self._attempt(state, ctx, inputs)
        if outcome != "ok":
            reason = _error_text(payload) if outcome == "error" else str(payload)
            LOG.warning(
                "Plugin %s evaluation failed with %s",
                state.manifest.id,
                outcome,
                extra={
                    "operation": "plugin_evaluation",
                    "outcome": outcome,
                    "plugin_id": state.manifest.id,
                    "error": reason,
                },
            )
        if outcome == "protocol":
            # Never reuse unread responses after losing protocol alignment.
            await self._stop_runner(state)
        if outcome == "ok":
            assert isinstance(payload, EvaluatedDecision)
            if payload.type == "error":
                payload.error = payload.error or "Plugin returned an error decision without a reason."
                LOG.warning("Plugin %s evaluation failed", state.manifest.id, extra={"error": payload.error})
            return payload
        return EvaluatedDecision(type="error", error=reason)

    async def _attempt(self, state: _PluginState, ctx: dict[str, Any], inputs: dict[str, Any]) -> tuple[str, Any]:
        runner = state.runner
        if runner is None or runner.returncode is not None:
            try:
                runner = await self._acquire_runner(state)
            except OSError as exc:
                return "crash", f"runner spawn failed: {exc}"
            state.runner = runner
        request = {"op": "decide", "ctx": ctx, "inputs": inputs}
        try:
            line = await asyncio.wait_for(self._roundtrip(runner, request), self._decide_timeout_s)
        except asyncio.CancelledError:
            try:
                await self._stop_runner(state)
            except Exception:
                LOG.exception("could not stop cancelled plugin runner %s", state.manifest.id)
            raise
        except TimeoutError:
            await self._stop_runner(state)
            return "timeout", f"decide timed out after {self._decide_timeout_s}s"
        except ConnectionError as exc:
            await self._stop_runner(state)
            return "crash", f"runner closed its stdin before responding: {error_message(exc)}"
        except ValueError as exc:
            await self._stop_runner(state)
            return "protocol", f"runner protocol read failed: {error_message(exc)}"
        if not line:
            await self._stop_runner(state)
            return "crash", f"runner exited before responding. Exit code: {runner.returncode}."
        try:
            response = json.loads(line)
        except ValueError as exc:
            return "protocol", f"runner sent invalid JSON: {error_message(exc)}"
        if not isinstance(response, dict):
            return "protocol", "runner response must be a JSON object"
        if "error" in response:
            return "error", response["error"]
        if "decision" not in response:
            return "protocol", "runner response has neither decision nor error"
        try:
            return "ok", EvaluatedDecision.model_validate(response["decision"])
        except ValidationError as exc:
            return "protocol", f"invalid decision payload: {error_message(exc)}"

    @staticmethod
    async def _roundtrip(proc: asyncio.subprocess.Process, request: dict[str, Any]) -> bytes:
        if proc.stdin is None or proc.stdout is None:
            msg = "runner pipes are closed"
            raise RuntimeError(msg)
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        return await proc.stdout.readline()

    async def _spawn(self, state: _PluginState) -> asyncio.subprocess.Process:
        return await create_subprocess_exec(
            sys.executable,
            "-m",
            "server.plugins.runner",
            str(state.dir),
            cwd=_REPO_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _acquire_runner(self, state: _PluginState) -> asyncio.subprocess.Process:
        spawning = asyncio.create_task(self._spawn(state))
        cancelled = False
        while not spawning.done():
            try:
                await asyncio.shield(spawning)
            except asyncio.CancelledError:
                cancelled = True
        runner = spawning.result()
        state.runner = runner
        blocked = self._lifecycle_block(state.manifest.id, state)
        if cancelled or blocked is not None:
            await drain(asyncio.create_task(self._stop_runner(state)))
            if cancelled:
                raise asyncio.CancelledError
            raise OSError(blocked)
        return runner

    async def _stop_runner(self, state: _PluginState) -> None:
        runner = state.runner
        if runner is not None:
            state.retiring = True

            async def stop() -> None:
                try:
                    await _kill_runner(runner)
                except BaseException:
                    if state.runner is runner:
                        state.exit_unconfirmed = True
                    raise
                if state.runner is runner:
                    state.runner = None
                    state.retiring = False
                    state.exit_unconfirmed = False

            await drain(asyncio.create_task(stop()))

    def start_watching(self) -> None:
        if self._closed or self._watch_task is not None:
            return
        LOG.info(
            "Plugin host loaded %d plugins. Directories: %s",
            len(self._plugins),
            ", ".join(str(path) for path in self._plugins_dirs),
        )
        self._stop_watching = threading.Event()
        self._watch_task = asyncio.get_running_loop().create_task(self._watch_loop(), name="plugin-host-watch")

    async def _watch_loop(self) -> None:
        stop = self._stop_watching
        if stop is None:
            return
            # awatch raises on missing paths. The optional user plugins directory may not exist.
        roots = [path for path in self._plugins_dirs if path.is_dir()]
        if not roots:
            return
        try:
            async for changes in watchfiles.awatch(*roots, stop_event=stop):
                await self._apply_changes({Path(path) for _, path in changes})
        except Exception:
            LOG.exception("plugin watcher stopped unexpectedly")

    async def _apply_changes(self, paths: set[Path]) -> None:
        candidates: dict[Path, None] = {}
        for path in paths:
            for root in self._plugins_dirs:
                if path.is_relative_to(root):
                    rel = path.relative_to(root)
                    # Skip upload staging and reset leftovers. See _discover.
                    if rel.parts and not rel.parts[0].startswith("."):
                        candidates[root / rel.parts[0]] = None
        # An upload can still roll back until its files are committed. Don't migrate
        # saved flows against that temporary installation from the watcher.
        async with self.upload_lock:
            for plugin_dir in candidates:
                previous = next((s for s in self._plugins.values() if s.dir == plugin_dir), None)
                if previous is not None:
                    await self._stop_runner(previous)
                if not (plugin_dir / "plugin.toml").is_file():
                    if previous is not None:
                        self._plugins.pop(previous.manifest.id, None)
                    self._invalid.pop(plugin_dir.name, None)
                    continue
                self._register(plugin_dir)
            if candidates and self.on_change is not None:
                await self.on_change()

    async def aclose(self) -> None:
        # Set before touching runners so queued callers observe the closure.
        self._closed = True
        if self._close_task is None:
            if self._stop_watching is not None:
                self._stop_watching.set()
            self._close_task = asyncio.create_task(self._close())
        closing = self._close_task
        cancelled = False
        while not closing.done():
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                cancelled = True
        error = closing.result()
        if error is not None:
            raise error
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self) -> BaseException | None:
        task = self._watch_task
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), 5)
            await asyncio.gather(task, return_exceptions=True)
            self._watch_task = None
        error: BaseException | None = None
        for state in list(self._plugins.values()):
            try:
                await self._stop_runner(state)
            except (asyncio.CancelledError, Exception) as exc:
                # A runner's cancellation is its failure, not permission to skip other running tasks.
                if error is None:
                    error = exc
        return error
