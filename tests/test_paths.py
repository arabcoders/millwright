from __future__ import annotations

from pathlib import Path

import pytest

from node.cli import _credentials_path
from server.core.config import ServerConfig
from server.core.paths import (
    cache_dir,
    config_dir,
    config_file,
    state_dir,
    workspace_dir,
)


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MW_CONFIG_PATH",
        "MW_DATABASE_URL",
        "MW_STATE_PATH",
        "MW_CACHE_PATH",
        "MW_NODE_CONFIG",
        "MW_NODE_STATE_PATH",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_directories(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config/millwright"
    assert config_file() == tmp_path / ".config/millwright/config.toml"
    assert state_dir() == tmp_path / ".local/state/millwright"
    assert cache_dir() == tmp_path / ".cache/millwright"


def test_xdg_bases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "stat"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cach"))
    assert config_file() == tmp_path / "conf/millwright/config.toml"
    assert state_dir() == tmp_path / "stat/millwright"
    assert cache_dir() == tmp_path / "cach/millwright"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.setenv("MW_STATE_PATH", str(tmp_path / "state"))
    monkeypatch.setenv("MW_CACHE_PATH", str(tmp_path / "cache"))
    assert state_dir() == tmp_path / "state"
    assert _credentials_path() == tmp_path / "state/node-credentials.json"
    assert cache_dir() == tmp_path / "cache"


def test_config_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.setenv("MW_CONFIG_PATH", str(tmp_path / "env-config"))
    assert config_dir() == tmp_path / "env-config"
    assert config_file(tmp_path / "flag.toml") == tmp_path / "flag.toml"
    assert config_file(tmp_path) == tmp_path / "config.toml"
    assert config_file() == tmp_path / "env-config/config.toml"


def test_workspace_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.setenv("MW_STATE_PATH", str(tmp_path / "state"))
    assert workspace_dir() == tmp_path / "state/workspace"
    assert workspace_dir(ServerConfig(workspace_dir=str(tmp_path / "shared"))) == tmp_path / "shared"
