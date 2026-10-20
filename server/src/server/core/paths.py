from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

APP_DIR = "millwright"
CONFIG_NAME = "config.toml"

if TYPE_CHECKING:
    from server.core.config import ServerConfig


def _dir(env: str, xdg: str, fallback: str) -> Path:
    override = os.environ.get(env)
    if override:
        return Path(override).expanduser()
    base = os.environ.get(xdg) or Path.home() / fallback
    return Path(base).expanduser() / APP_DIR


def config_dir() -> Path:
    override = os.environ.get("MW_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / APP_DIR


def config_file(override: Path | None = None) -> Path:
    if override is not None:
        return override if override.suffix == ".toml" else override / CONFIG_NAME
    return config_dir() / CONFIG_NAME


def state_dir() -> Path:
    return _dir("MW_STATE_PATH", "XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    return _dir("MW_CACHE_PATH", "XDG_CACHE_HOME", ".cache")


def uploaded_plugins_dir(config: ServerConfig | None = None) -> Path:
    if config is not None and config.uploaded_plugins_dir:
        return Path(config.uploaded_plugins_dir).expanduser()
    return state_dir() / "uploaded_plugins"


def workspace_dir(config: ServerConfig | None = None) -> Path:
    if config is not None and config.workspace_dir:
        return Path(config.workspace_dir).expanduser()
    return state_dir() / "workspace"


def trash_dir(config: ServerConfig | None = None) -> Path:
    if config is not None and config.trash_dir:
        return Path(config.trash_dir).expanduser()
    return state_dir() / "trash"


def healthcheck_dir() -> Path:
    return state_dir() / "healthchecks"


def playback_dir() -> Path:
    # Playback files are deliberately outside the job workspace: playback must
    # never interact with staging, finalization, or recovery files.
    return state_dir() / "playback"


def validate_runtime_roots(
    *,
    workspace: Path,
    trash: Path,
    uploads: Path | None = None,
    config_path: Path | None = None,
) -> None:
    roots = [
        ("workspace", workspace.expanduser().resolve()),
        ("trash", trash.expanduser().resolve()),
        ("healthcheck diagnostics", healthcheck_dir().resolve()),
    ]
    if uploads is not None:
        roots.append(("uploaded plugins", uploads.expanduser().resolve()))
    for index, (name, root) in enumerate(roots):
        for other_name, other_root in roots[index + 1 :]:
            if root.is_relative_to(other_root) or other_root.is_relative_to(root):
                msg = f"{name} and {other_name} directories overlap. Runtime roots must be separate."
                raise ValueError(msg)
        if config_path is not None:
            critical = config_path.expanduser().resolve()
            if critical.is_relative_to(root) or root.is_relative_to(critical):
                msg = f"{name} directory must be separate from the config file"
                raise ValueError(msg)
