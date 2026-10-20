from __future__ import annotations

from pathlib import Path
import traceback

import pytest

from node.cli import load_settings
from server.api.app import create_app
from server.core.config import ServerConfig, load_config
from server.core.paths import healthcheck_dir
from server.trash import trash_path
from tests.helpers import database_context


def test_validation_hides_secrets(tmp_path: Path) -> None:
    marker = "audit-secret-marker"
    (tmp_path / "oidc-secret").write_text(marker, encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text('[auth.oidc]\nsecret = "file:oidc-secret"\n', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(path)
    leaked = marker in str(error.value)
    # Don't include the diagnostic or marker in failure output.
    assert leaked is False
    traceback_leaked = marker in "".join(traceback.format_exception(error.value))
    assert traceback_leaked is False


@pytest.mark.parametrize(
    ("variable", "value", "node"),
    [
        ("MW_AUTH_OIDC_SCOPES", '["audit-secret-marker"', False),
        ("MW_AUTH_OIDC_SCOPES", '{"audit-secret-marker": 1}', False),
        ("MW_ACCESS_LOG", "audit-secret-marker", False),
        ("MW_AUTH_OIDC_ISSUER", "audit-secret-marker", False),
        ("MW_NODE_WORKER_TAGS", '["audit-secret-marker"', True),
        ("MW_NODE_WORKER_TAGS", '{"audit-secret-marker": 1}', True),
        ("MW_NODE_HEARTBEAT_S", "audit-secret-marker", True),
    ],
)
def test_env_validation_privacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, value: str, node: bool
) -> None:
    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("MW_API_KEY", "audit-secret-marker")
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "audit-secret-marker")
    monkeypatch.setenv("MW_NODE_STATE_PATH", str(tmp_path / "state"))
    path = tmp_path / "config.toml"
    path.write_text(
        f'enrollment_token = "enroll-secret"\nserver_url = "ws://hub"\nworkspace_dir = "{tmp_path / "workspace"}"\n'
        if node
        else "",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as error:
        if node:
            load_settings(path)
        else:
            load_config(path)
    leaked = "audit-secret-marker" in str(error.value)
    assert leaked is False
    traceback_leaked = "audit-secret-marker" in "".join(traceback.format_exception(error.value))
    assert traceback_leaked is False
    if value == '["audit-secret-marker"':
        assert variable in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["workspace_dir", "trash_dir", "uploaded_plugins_dir"])
@pytest.mark.parametrize("relationship", ["same", "parent", "child", "alias"])
async def test_healthlog_root_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, relationship: str
) -> None:
    monkeypatch.setenv("MW_STATE_PATH", str(tmp_path / "state"))
    root = healthcheck_dir()
    alias = tmp_path / "alias"
    if relationship == "alias":
        alias.symlink_to(root, target_is_directory=True)
    target = {"same": root, "parent": root.parent, "child": root / "nested", "alias": alias}[relationship]
    config = ServerConfig.model_validate({owner: str(target)})
    async with database_context("healthlog-root-safety") as db:
        with pytest.raises(ValueError):
            create_app(db, config=config)
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["workspace", "trash"])
@pytest.mark.parametrize("relationship", ["same", "parent", "child"])
async def test_upload_root_conflicts(tmp_path: Path, owner: str, relationship: str) -> None:
    root = tmp_path / owner
    child = root / "jobs" / "1" if owner == "workspace" else trash_path(root, 1, "abcd0123456789ab", ".mkv").parent
    upload = {"same": root, "parent": tmp_path, "child": child}[relationship]
    config = ServerConfig(
        workspace_dir=str(tmp_path / "workspace"),
        trash_dir=str(tmp_path / "trash"),
        uploaded_plugins_dir=str(upload),
    )
    async with database_context("config-root-safety") as db:
        with pytest.raises(ValueError):
            create_app(db, config=config)


@pytest.mark.asyncio
async def test_trash_contains_workspace(tmp_path: Path) -> None:
    config = ServerConfig(
        workspace_dir=str(trash_path(tmp_path / "trash", 1, "abcd0123456789ab", ".mkv").parent),
        trash_dir=str(tmp_path / "trash"),
        uploaded_plugins_dir=str(tmp_path / "plugins"),
    )
    async with database_context("config-nested-roots") as db:
        with pytest.raises(ValueError):
            create_app(db, config=config)


def test_node_validation_privacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marker = "audit-secret-marker"
    for key in ("MW_NODE_ENROLLMENT_TOKEN", "MW_NODE_SERVER_URL", "MW_NODE_WORKSPACE_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MW_NODE_STATE_PATH", str(tmp_path / "state"))
    path = tmp_path / "node.toml"
    path.write_text(
        f'enrollment_token = ["{marker}"]\nserver_url = "ws://hub"\nworkspace_dir = "{tmp_path / "workspace"}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as error:
        load_settings(path, must_exist=True)
    leaked = marker in str(error.value)
    assert leaked is False
    traceback_leaked = marker in "".join(traceback.format_exception(error.value))
    assert traceback_leaked is False


@pytest.mark.asyncio
async def test_upload_contains_config(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    root.mkdir()
    path = root / "config.toml"
    path.write_text(
        f'workspace_dir = "{tmp_path / "workspace"}"\ntrash_dir = "{tmp_path / "trash"}"\nuploaded_plugins_dir = "{root}"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    async with database_context("config-upload-conflict") as db:
        with pytest.raises(ValueError):
            create_app(db, config=config, config_path=path)
