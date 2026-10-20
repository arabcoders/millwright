from __future__ import annotations

import argparse
import logging
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from common.dates import Dates
from node import cli
from node.cli import ENV_OVERRIDES, _credentials_path, default_config_path, load_settings
from server.core.config import load_config
from tests.helpers import make_test_temp_dir


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MW_NODE_CONFIG",
        "MW_CONFIG_PATH",
        "XDG_CONFIG_HOME",
        *ENV_OVERRIDES.values(),
        "MW_NODE_STATE_PATH",
        "MW_STATE_PATH",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(make_test_temp_dir("state-home")))


def config(body: str) -> Path:
    path = make_test_temp_dir("node-cli") / "node.toml"
    path.write_text(body, encoding="utf-8")
    return path


def body(root: Path) -> str:
    return f'enrollment_token = "enroll-secret"\nserver_url = "http://hub"\nworkspace_dir = "{root}"\n'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://server:8000", "ws://server:8000/api/hub"),
        ("https://server/", "wss://server/api/hub"),
        ("ws://server", "ws://server/api/hub"),
        ("wss://server", "wss://server/api/hub"),
        ("https://server/api/hub", "wss://server/api/hub"),
        ("wss://server/api/hub/", "wss://server/api/hub"),
        ("http://[::1]:8000", "ws://[::1]:8000/api/hub"),
        (
            " https://proxy-user:proxy-password@server?key=private-query ",
            "wss://proxy-user:proxy-password@server/api/hub?key=private-query",
        ),
    ],
)
def test_hub_url_conversion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str, expected: str) -> None:
    monkeypatch.setenv("MW_NODE_SERVER_URL", value)
    assert load_settings(config(body(tmp_path / "workspace"))).server_url == expected


@pytest.mark.parametrize(
    "value",
    [
        "server:8000",
        "https://",
        "http://server:0",
        "https://user:private-value@server:bad",
        "https://server/wrong?key=private-value",
        "https://server#private-value",
    ],
)
def test_hub_url_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str) -> None:
    monkeypatch.setenv("MW_NODE_SERVER_URL", value)
    with pytest.raises(ValueError) as error:
        load_settings(config(body(tmp_path / "workspace")))
    assert "MW_NODE_SERVER_URL" in str(error.value)
    assert "private-value" not in str(error.value)


def test_credentials_load_on_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("workspace")
    state = make_test_temp_dir("state")
    monkeypatch.setenv("MW_NODE_STATE_PATH", str(state))
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "enroll-secret")
    monkeypatch.setenv("MW_NODE_SERVER_URL", "ws://hub")
    monkeypatch.setenv("MW_NODE_WORKSPACE_DIR", str(root))
    credentials = state / "node-credentials.json"
    credentials.write_text('{"node_id":"node-a1b2c3d4e5f60708","token":"node-token"}\n', encoding="utf-8")
    settings = load_settings(config(""))
    assert (settings.node_id, settings.token) == ("node-a1b2c3d4e5f60708", "node-token")
    assert settings.enrollment_token == "enroll-secret"
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "rotated-secret")
    settings = load_settings(config(""))
    assert (settings.node_id, settings.token) == ("node-a1b2c3d4e5f60708", "node-token")
    assert settings.enrollment_token == "rotated-secret"
    monkeypatch.delenv("MW_NODE_ENROLLMENT_TOKEN")
    settings = load_settings(config(""))
    assert (settings.node_id, settings.token) == ("node-a1b2c3d4e5f60708", "node-token")
    assert settings.enrollment_token is None


@pytest.mark.parametrize("source", ["node", "shared", "xdg", "home"])
def test_state_path_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MW_NODE_STATE_PATH", "~/node-state")
    monkeypatch.setenv("MW_STATE_PATH", "~/shared-state")
    monkeypatch.setenv("XDG_STATE_HOME", "~/xdg-state")
    if source != "node":
        monkeypatch.delenv("MW_NODE_STATE_PATH")
    if source in ("xdg", "home"):
        monkeypatch.delenv("MW_STATE_PATH")
    if source == "home":
        monkeypatch.delenv("XDG_STATE_HOME")
    expected = {
        "node": home / "node-state",
        "shared": home / "shared-state",
        "xdg": home / "xdg-state" / "millwright",
        "home": home / ".local" / "state" / "millwright",
    }[source] / "node-credentials.json"
    assert _credentials_path() == expected
    expected.parent.mkdir(parents=True)
    expected.write_text('{"node_id":"node-a1b2c3d4e5f60708","token":"node-token"}\n', encoding="utf-8")
    settings = load_settings(config(body(tmp_path / "workspace")))
    assert settings.credentials_path == expected
    assert (settings.node_id, settings.token) == ("node-a1b2c3d4e5f60708", "node-token")


def test_missing_required() -> None:
    with pytest.raises(ValueError) as exc:
        load_settings(config(""))
    assert "MW_NODE_WORKSPACE_DIR" in str(exc.value)


def test_secret_file(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("workspace")
    secret = root / "token"
    secret.write_text(" secret \n")
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", f"file:{secret}")
    settings = load_settings(config(body(root).replace("enroll-secret", "file:missing")))
    assert settings.enrollment_token == "secret"


@pytest.mark.parametrize("source", ["literal", "file"])
def test_shared_enrollment_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    (tmp_path / "enrollment").write_text("shared-secret\n")
    server_path = tmp_path / "config.toml"
    server_path.write_text('node_enrollment_token = "file:missing-server-secret"\n')
    node_path = tmp_path / "node.toml"
    node_path.write_text(body(tmp_path / "workspace").replace("enroll-secret", "file:missing-node-secret"))
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "shared-secret" if source == "literal" else "file:enrollment")
    server = load_config(server_path)
    node = load_settings(node_path)
    assert server.node_enrollment_token == node.enrollment_token == "shared-secret"
    assert node.token == "shared-secret" and node.node_id is None


def test_unknown_warns(caplog: pytest.LogCaptureFixture) -> None:
    root = make_test_temp_dir("workspace")
    with caplog.at_level(logging.WARNING, logger="node.cli"):
        settings = load_settings(config(body(root) + "bogus = 1\n"))
    assert settings.workspace_dir == str(root)
    assert any(record.name == "node.cli" and record.levelno == logging.WARNING for record in caplog.records)


def test_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MW_NODE_CONFIG", str(tmp_path / "flag.toml"))
    assert default_config_path() == tmp_path / "flag.toml"


def test_env_optional_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("workspace")
    values = {
        "enrollment_token": "env-token",
        "server_url": "https://env-hub",
        "workspace_dir": str(root),
        "ffmpeg_bin": "/env/ffmpeg",
        "ffprobe_bin": "/env/ffprobe",
        "worker_tags": '["gpu", "cpu"]',
        "min_free_gb": "1.5",
        "hard_size_ceiling": "2.5",
        "stall_timeout_s": "180.5",
        "heartbeat_s": "3.5",
        "backoff_base_s": "0.5",
        "backoff_max_s": "5.5",
    }
    for field, value in values.items():
        monkeypatch.setenv(ENV_OVERRIDES[field], value)
    settings = load_settings(config(body(root) + 'worker_tags = ["toml"]\nmin_free_gb = 20.0\n'))
    assert settings.token == "env-token"
    assert settings.enrollment_token == "env-token"
    assert settings.server_url == "wss://env-hub/api/hub"
    assert settings.workspace_dir == str(root)
    assert settings.ffmpeg_bin == "/env/ffmpeg"
    assert settings.ffprobe_bin == "/env/ffprobe"
    assert settings.capabilities.worker_tags == ["gpu", "cpu"]
    assert settings.min_free_gb == 1.5
    assert settings.hard_size_ceiling == 2.5
    assert settings.stall_timeout_s == 180.5
    assert settings.heartbeat_s == 3.5
    assert settings.backoff_base_s == 0.5
    assert settings.backoff_max_s == 5.5


def test_optional_toml_preserved() -> None:
    root = make_test_temp_dir("workspace")
    settings = load_settings(
        config(body(root) + 'ffmpeg_bin = "custom-ffmpeg"\nworker_tags = ["gpu"]\nheartbeat_s = 8.0\n')
    )
    assert settings.node_id is None and settings.enrollment_token == "enroll-secret"
    assert settings.workspace_dir == str(root)
    assert settings.server_url == "ws://hub/api/hub"
    assert settings.ffmpeg_bin == "custom-ffmpeg"
    assert settings.capabilities.worker_tags == ["gpu"]
    assert settings.heartbeat_s == 8.0
    assert settings.backoff_base_s == 1.0


def test_empty_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("workspace")
    monkeypatch.setenv("MW_NODE_ENROLLMENT_TOKEN", "")
    with pytest.raises(ValueError):
        load_settings(config(body(root)))


@pytest.mark.usefixtures("logging_state")
def test_fatal_exit_logs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    failure = OSError("hub connection unavailable")
    monkeypatch.setattr(cli, "_amain", AsyncMock(side_effect=failure))
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 1
    record = json.loads(capsys.readouterr().err)
    assert record["logger"] == "node.cli" and record["level"] == "error"
    assert record["event"]
    assert str(failure) in record["exception"]


@pytest.mark.asyncio
async def test_startup_logs_cause(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing = tmp_path / "missing-ffmpeg"
    path = config(body(tmp_path / "workspace") + f'ffmpeg_bin = "{missing}"\n')
    result = await cli._amain(argparse.Namespace(config=path), Dates("UTC"))
    assert result == 2
    record = next(record for record in caplog.records if record.name == "node.cli" and record.levelno == logging.ERROR)
    assert str(missing) in getattr(record, "error")
