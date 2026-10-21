from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import structlog
from common.logging import THIRD_PARTY_LEVELS
from server.plugins.host import EvaluatedDecision
from server.plugins.runner import _inputs_model, _load_module, _Plugin, _respond
from server.scanner import scan

from tests.fakes.ffprobe import probe_output
from tests.helpers import (
    _schema_name,
    _schema_url,
    cleanup_test_run_root,
    cleanup_test_schemas,
    database_context,
    database_url_context,
    get_test_database_url,
    get_test_per_run_root,
    get_test_system_temp_root,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from server.plugins.host import _PluginState
    from sqlalchemy import URL
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def local_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.finalization import _verify_media, _verify_media_process
    from server.nodehub.hub import _inspect_artifact, _inspect_output
    from server.workspace import wait_workspace_io

    async def execute(operation, *args, **kwargs):
        if operation is _verify_media_process:
            return await _verify_media(*args, **kwargs)
        if operation is _inspect_artifact:
            return await _inspect_output(*args, **kwargs)
        return await wait_workspace_io(partial(operation, *args, **kwargs))

    # Existing journal tests inject filesystem failures in this process. The
    # spawned writer and tool lifecycle have separate real-process coverage.
    for module in ("staging", "finalization", "trash", "nodehub.hub"):
        monkeypatch.setattr(f"server.{module}.storage_io", execute)


@pytest.fixture
def without_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    which = scan.shutil.which
    monkeypatch.setattr(scan.shutil, "which", lambda name: None if name in {"fd", "fdfind"} else which(name))


@pytest.fixture
def database_url(request: pytest.FixtureRequest) -> Iterator[URL]:
    with database_url_context(request.node.name) as url:
        yield url


@pytest_asyncio.fixture
async def database_factory(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_context(request.node.name) as factory:
        yield factory


@pytest.fixture
def mock_ffprobe(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async def run(argv: list[str], _timeout_s: float, name: str) -> tuple[int, bytes, bytes]:
        assert name == "ffprobe"
        return 0, probe_output(Path(argv[-1])).encode(), b""

    # Mock the tool boundary, not probe parsing or filesystem source checks.
    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr("common.media.tools._run", mock)
    return mock


@pytest.fixture
def mock_plugin_runners(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    plugins: dict[Path, _Plugin] = {}
    monkeypatch.setitem(sys.modules, "ffmpeg_converter_plugin", ModuleType("ffmpeg_converter_plugin"))

    async def attempt(state: _PluginState, ctx: dict[str, Any], inputs: dict[str, Any]) -> tuple[str, Any]:
        if state.dir not in plugins:
            module = _load_module(state.dir / "plugin.py")
            plugins[state.dir] = _Plugin(manifest=state.manifest, module=module, inputs=_inputs_model(module))
        response = _respond(plugins[state.dir], json.dumps({"op": "decide", "ctx": ctx, "inputs": inputs}))
        if "error" in response:
            return "error", response["error"]
        return "ok", EvaluatedDecision.model_validate(response["decision"])

    # Keep plugin decisions, validation, and host lifecycle checks. Replace only runner IPC.
    mock = AsyncMock(side_effect=attempt)
    monkeypatch.setattr("server.plugins.host.PluginHost._attempt", mock)
    return mock


@pytest.fixture
def logging_state() -> Iterator[None]:
    root = logging.getLogger()
    handlers = root.handlers.copy()
    level = root.level
    configuration = structlog.get_config()
    dependencies = [(logging.getLogger(name), logging.getLogger(name).level) for name, _ in THIRD_PARTY_LEVELS]
    uvicorn = logging.getLogger("uvicorn.error")
    filters = uvicorn.filters.copy()
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in handlers:
                handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)
        structlog.configure(**configuration)
        uvicorn.filters[:] = filters
        for logger, dependency_level in dependencies:
            logger.setLevel(dependency_level)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    os.environ.setdefault("MW_TEST_DATABASE_URL", "postgresql+psycopg://mw_ci:mw_ci@127.0.0.1:55433/mw_ci")
    temp_root = get_test_system_temp_root()
    for env_name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[env_name] = str(temp_root)

    tempfile.tempdir = None

    run_root = get_test_per_run_root()
    # Workers keep tmp_path under their own run root, alongside database and state artifacts.
    if getattr(config.option, "basetemp", None) is None or hasattr(config, "workerinput"):
        config.option.basetemp = str(run_root / "pytest")

    os.environ["MW_CONFIG_PATH"] = str(run_root / "config")
    os.environ["MW_STATE_PATH"] = str(run_root / "state")
    os.environ["MW_CACHE_PATH"] = str(run_root / "cache")

    # Replace inherited application credentials before collecting tests or running CLI commands.
    cleanup = ExitStack()
    config.add_cleanup(cleanup_test_schemas)
    config.add_cleanup(cleanup.close)
    # An uninitialized test schema keeps inherited application credentials out of
    # pure tests without opening PostgreSQL. Database fixtures initialize their own schemas.
    url = _schema_url(get_test_database_url(), _schema_name("environment"))
    environment = cleanup.enter_context(pytest.MonkeyPatch.context())
    environment.setenv("MW_DATABASE_URL", url.render_as_string(hide_password=False))

    config._inicache["cache_dir"] = str(run_root / "pytest-cache")


def pytest_unconfigure(config) -> None:
    del config
    cleanup_test_run_root()
