from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError
from common.config import apply_env_overrides
from common.enums import JobStatus
from server.api.app import create_app
from server.core.config import ServerConfig, ensure_config, is_oidc_ready, load_config
from server.settings import OperationalSettings
from tests.helpers import database_context, make_test_temp_dir
from tests.test_staging import add_job, get_job


@pytest.mark.parametrize("field", ["max_probes", "max_mkv_stats"])
def test_metadata_limits(field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        OperationalSettings.model_validate({field: 0})
    assert [error["loc"] for error in raised.value.errors()] == [(field,)]


def write_config(body: str) -> Path:
    path = make_test_temp_dir("config") / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_first_start_config(tmp_path: Path) -> None:
    path = tmp_path / "millwright" / "config.toml"
    ensure_config(path)
    config = load_config(path)
    assert config.api_key and config.node_enrollment_token
    assert config.api_key != config.node_enrollment_token
    content = path.read_text(encoding="utf-8")
    assert 'api_key = "file:secrets/api_key"' in content
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert (path.parent / "secrets").stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    for name in ("api_key", "node_enrollment_token"):
        assert (path.parent / "secrets" / name).stat().st_mode & 0o777 == 0o600
    ensure_config(path)
    assert load_config(path).api_key == config.api_key
    assert load_config(path).node_enrollment_token == config.node_enrollment_token


def test_existing_config_preserved(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    content = '# Keep this configuration.\napi_key = "existing-key"\naccess_log = false\n'
    path.write_text(content, encoding="utf-8")
    ensure_config(path)
    assert path.read_text(encoding="utf-8") == content
    assert not (tmp_path / "secrets").exists()


def test_config_key_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MW_API_KEY", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        "# Keep this configuration.\naccess_log = false\n" + FULL_OIDC,
        encoding="utf-8",
    )
    ensure_config(path)
    config = load_config(path)
    content = path.read_text(encoding="utf-8")
    assert config.api_key
    assert config.access_log is False
    assert config.auth.oidc is not None
    assert config.auth.oidc.username == "operator"
    assert content.startswith("# Keep this configuration.")
    assert config.api_key not in content
    secret = tmp_path / "secrets" / "api_key"
    assert secret.read_text(encoding="utf-8").strip() == config.api_key
    assert secret.stat().st_mode & 0o777 == 0o600
    ensure_config(path)
    assert path.read_text(encoding="utf-8") == content
    assert load_config(path).api_key == config.api_key


def test_insecure_secrets_rejected(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        ensure_config(tmp_path / "config.toml")
    assert not (tmp_path / "config.toml").exists()


FULL_OIDC = """
[auth.oidc]
username = "operator"
issuer = "https://sso.example.com/realms/main"
client_id = "transcode"
secret = "literal-secret"
redirect_uri = "https://transcode.example.com/api/auth/oidc/callback"
post_login_url = "https://transcode.example.com/login/callback"
"""


def test_retention_nonnegative() -> None:
    with pytest.raises(ValueError):
        OperationalSettings(job_retention_days=-1)


def test_healthcheck_backend_validated() -> None:
    with pytest.raises(ValueError):
        OperationalSettings.model_validate({"healthcheck_hwaccel": "cuda"})


@pytest.mark.parametrize("reserve", [-1.0, float("nan"), float("inf")])
def test_workspace_reserve_invalid(reserve: float) -> None:
    with pytest.raises(ValueError):
        OperationalSettings(min_free_gb=reserve)


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve, staged", [(1.0, True), (10.0, False)])
async def test_workspace_reserve_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reserve: float, staged: bool
) -> None:
    monkeypatch.setattr("server.staging._workspace_free", lambda _path: 1_500_000_000)
    source = tmp_path / "source.mkv"
    source.write_bytes(b"media")
    async with database_context("configured-reserve") as factory:
        job_id = await add_job(factory, source)
        app = create_app(
            factory,
            config=ServerConfig(workspace_dir=str(tmp_path / "workspace")),
            cache_root=tmp_path / "cache",
        )
        try:
            await app.state.settings.update({"min_free_gb": reserve})
            assert await app.state.staging.stage_job(job_id) == int(staged)
            job = await get_job(factory, job_id)
            assert job.status == (JobStatus.QUEUED if staged else JobStatus.STAGING)
        finally:
            await app.state.staging.aclose()
            await app.state.events.aclose()


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_secret_file_resolves(absolute: bool) -> None:
    config_dir = make_test_temp_dir("config")
    (config_dir / "secrets").mkdir()
    secret = config_dir / "secrets" / "oidc"
    secret.write_text("  file-secret  \n", encoding="utf-8")
    reference = str(secret) if absolute else "secrets/oidc"
    path = config_dir / "config.toml"
    path.write_text(FULL_OIDC.replace("literal-secret", f"file:{reference}"), encoding="utf-8")
    config = load_config(path)
    assert config.auth.oidc is not None
    assert config.auth.oidc.secret == "file-secret"


def test_client_id_file() -> None:
    config_dir = make_test_temp_dir("config")
    (config_dir / "client-id").write_text("  transcode-from-file  \n", encoding="utf-8")
    path = config_dir / "config.toml"
    path.write_text(FULL_OIDC.replace('client_id = "transcode"', 'client_id = "file:client-id"'), encoding="utf-8")
    config = load_config(path)
    assert config.auth.oidc is not None
    assert config.auth.oidc.client_id == "transcode-from-file"


def test_missing_client_id() -> None:
    path = write_config(FULL_OIDC.replace('client_id = "transcode"', 'client_id = "file:missing-client-id"'))
    with pytest.raises(ValueError):
        load_config(path)


def test_unknown_keys_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="server.config"):
        config = load_config(write_config(FULL_OIDC + "\nbogus_top = 1\n\n[auth]\nbogus = 2\n\n[scanner]\nbogus = 3\n"))
    assert is_oidc_ready(config)
    records = [record for record in caplog.records if record.name == "server.config"]
    assert len(records) == 3 and all(record.levelno == logging.WARNING for record in records)


def test_node_enrollment_token() -> None:
    default = load_config(write_config(""))
    assert default.node_enrollment_token is None
    secret = make_test_temp_dir("config") / "enrollment"
    secret.write_text("  fleet-secret  \n", encoding="utf-8")
    config = load_config(write_config(f'node_enrollment_token = "file:{secret}"\n'))
    assert config.node_enrollment_token == "fleet-secret"


def test_missing_secret_file() -> None:
    missing = make_test_temp_dir("config") / "nope"
    with pytest.raises(ValueError):
        load_config(write_config(FULL_OIDC.replace("literal-secret", f"file:{missing}")))


def test_missing_config_file() -> None:
    with pytest.raises(ValueError):
        load_config(make_test_temp_dir("config") / "config.toml")


def test_oidc_config_incomplete() -> None:
    incomplete = FULL_OIDC.replace('secret = "literal-secret"\n', "")
    with pytest.raises(ValueError):
        load_config(write_config(incomplete))


def test_timezone_defaults_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert ServerConfig().tz == "UTC"
    assert load_config(write_config("")).tz == "UTC"


def test_timezone_uses_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Kuwait")
    assert ServerConfig().tz == "Asia/Kuwait"
    assert load_config(write_config("")).tz == "Asia/Kuwait"


def test_timezone_toml_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Kuwait")
    assert load_config(write_config('tz = "Europe/Berlin"\n')).tz == "Europe/Berlin"


@pytest.mark.parametrize("timezone", ["Not/A_Timezone", "  "])
def test_timezone_invalid(timezone: str) -> None:
    with pytest.raises(ValueError):
        load_config(write_config(f'tz = "{timezone}"\n'))


def test_env_overrides_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "database_url": "postgresql://env-db/millwright",
        "access_log": "false",
        "disable_jobs": "true",
        "enable_docs": "true",
        "workspace_dir": "/env/workspace",
        "trash_dir": "/env/trash",
        "node_enrollment_token": "env-enrollment",
        "api_key": "env-api-key",
        "uploaded_plugins_dir": "/env/plugins",
        "tz": "Europe/Berlin",
        "auth_oidc_username": "env-operator",
        "auth_oidc_issuer": "https://env.example.com",
        "auth_oidc_client_id": "env-client",
        "auth_oidc_secret": "env-secret",
        "auth_oidc_redirect_uri": "https://env.example.com/callback",
        "auth_oidc_scopes": '["openid", "email"]',
        "auth_oidc_post_login_url": "https://env.example.com/login",
    }
    for field, value in values.items():
        monkeypatch.setenv(f"MW_{field.upper()}", value)
    config = load_config(
        write_config(
            'database_url = "postgresql://toml-db/millwright"\naccess_log = true\nenable_docs = false\n' + FULL_OIDC
        )
    )
    assert config.model_dump() == {
        "database_url": "postgresql://env-db/millwright",
        "access_log": False,
        "disable_jobs": True,
        "enable_docs": True,
        "workspace_dir": "/env/workspace",
        "trash_dir": "/env/trash",
        "node_enrollment_token": "env-enrollment",
        "api_key": "env-api-key",
        "uploaded_plugins_dir": "/env/plugins",
        "tz": "Europe/Berlin",
        "auth": {
            "oidc": {
                "username": "env-operator",
                "issuer": "https://env.example.com",
                "client_id": "env-client",
                "secret": "env-secret",
                "redirect_uri": "https://env.example.com/callback",
                "scopes": ["openid", "email"],
                "post_login_url": "https://env.example.com/login",
            },
        },
    }


def test_nested_env_preserves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_AUTH_OIDC_ISSUER", "https://env.example.com")
    config = load_config(write_config(FULL_OIDC))
    assert config.auth.oidc is not None
    assert config.auth.oidc.issuer == "https://env.example.com"
    assert config.auth.oidc.secret == "literal-secret"


def test_env_creates_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    for field, value in {
        "issuer": "https://env.example.com",
        "client_id": "env-client",
        "secret": "env-secret",
        "redirect_uri": "https://env.example.com/callback",
    }.items():
        monkeypatch.setenv(f"MW_AUTH_OIDC_{field.upper()}", value)
    assert is_oidc_ready(load_config(write_config("")))


def test_env_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_API_KEY", "")
    assert load_config(write_config('api_key = "toml-secret"\n')).api_key == ""


def test_env_secret_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_AUTH_OIDC_SECRET", "env-secret")
    config = load_config(write_config(FULL_OIDC.replace('secret = "literal-secret"', 'secret = "file:missing"')))
    assert config.auth.oidc is not None
    assert config.auth.oidc.secret == "env-secret"


def test_env_secret_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'api_key = "file:missing"\nnode_enrollment_token = "file:missing"\n'
        + FULL_OIDC.replace("literal-secret", "file:missing").replace(
            'client_id = "transcode"', 'client_id = "file:missing"'
        ),
        encoding="utf-8",
    )
    for field in ("API_KEY", "NODE_ENROLLMENT_TOKEN", "AUTH_OIDC_SECRET", "AUTH_OIDC_CLIENT_ID"):
        (tmp_path / field).write_text(f"  {field.lower()}  \n", encoding="utf-8")
        monkeypatch.setenv(f"MW_{field}", f"file:{field}")
    config = load_config(path)
    assert config.api_key == "api_key"
    assert config.node_enrollment_token == "node_enrollment_token"
    assert config.auth.oidc is not None
    assert config.auth.oidc.secret == "auth_oidc_secret"
    assert config.auth.oidc.client_id == "auth_oidc_client_id"


@pytest.mark.parametrize("environment", [False, True], ids=["toml", "environment"])
def test_database_file_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: bool) -> None:
    url = "postgresql://fixture-user:fixture-password@fixture-host/millwright"
    (tmp_path / "database-url").write_text(f"  {url}\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        'database_url = "file:missing"\n' if environment else 'database_url = "file:database-url"\n', encoding="utf-8"
    )
    if environment:
        monkeypatch.setenv("MW_DATABASE_URL", "file:database-url")
    else:
        monkeypatch.delenv("MW_DATABASE_URL", raising=False)
    assert load_config(path).database_url == url


def test_env_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MW_SETTINGS_OPTIONS", '{"enabled": true, "count": 2}')
    data = {"settings": {"name": "retained"}}
    apply_env_overrides(data, {"settings.options": "MW_SETTINGS_OPTIONS"}, json_fields={"settings.options"})
    assert data == {"settings": {"name": "retained", "options": {"enabled": True, "count": 2}}}
