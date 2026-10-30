from __future__ import annotations

import logging
import os
import secrets
import stat
import tomllib
from pathlib import Path
from typing import Any, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomlkit
from common.config import apply_env_overrides
from common.secrets import resolve_secret
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class OidcConfig(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    issuer: str
    client_id: str
    secret: str
    redirect_uri: str
    username: str | None = None
    scopes: list[str] = ["openid", "profile", "email"]
    post_login_url: str | None = None

    @field_validator("issuer")
    @classmethod
    def _issuer_is_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            msg = "issuer must be an http(s) URL"
            raise ValueError(msg)
        return value


LOG = logging.getLogger("server.config")


def _default_timezone() -> str:
    return (os.environ.get("TZ") or "UTC").strip() or "UTC"


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    oidc: OidcConfig | None = None


class ServerConfig(BaseModel):
    # Unknown keys warn and are ignored so operators can identify config mistakes.
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    auth: AuthConfig = AuthConfig()
    access_log: bool = True
    database_url: str = Field(
        default="postgresql+psycopg://millwright:millwright@127.0.0.1:55432/millwright", repr=False
    )
    disable_jobs: bool = False
    enable_docs: bool = False
    workspace_dir: str | None = None
    trash_dir: str | None = None
    api_key: str | None = None
    # None uses the state directory's uploaded_plugins subdirectory.
    uploaded_plugins_dir: str | None = None
    tz: str = Field(default_factory=_default_timezone)

    @field_validator("tz")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            msg = "timezone must not be empty"
            raise ValueError(msg)
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError, ValueError:
            msg = "invalid timezone"
            raise ValueError(msg) from None
        return value


def _env_fields(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    fields = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        annotation = field.annotation
        nested = next(
            (
                candidate
                for candidate in (annotation, *get_args(annotation))
                if isinstance(candidate, type) and issubclass(candidate, BaseModel)
            ),
            None,
        )
        if nested is not None:
            fields.update(_env_fields(nested, f"{path}."))
        else:
            fields[path] = annotation
    return fields


_ENV_FIELDS = _env_fields(ServerConfig)
ENV_OVERRIDES = {field: f"MW_{field.replace('.', '_').upper()}" for field in _ENV_FIELDS}
_JSON_ENV_FIELDS = {field for field, annotation in _ENV_FIELDS.items() if get_origin(annotation) in (list, dict)}


def oidc_missing_fields(oidc: OidcConfig | None) -> list[str]:
    if oidc is None:
        return ["auth.oidc.issuer", "auth.oidc.client_id", "auth.oidc.secret", "auth.oidc.redirect_uri"]
    fields = ("issuer", "client_id", "secret", "redirect_uri")
    return [f"auth.oidc.{name}" for name in fields if not getattr(oidc, name)]


def is_oidc_ready(config: ServerConfig) -> bool:
    return config.auth.oidc is not None and not oidc_missing_fields(config.auth.oidc)


def _warn_extras(model: BaseModel, where: str) -> None:
    # Key names only: extra values may contain secrets and must never be logged.
    for key in getattr(model, "__pydantic_extra__", {}):
        LOG.warning("ignoring unknown config key %s%s", where, key)
    if isinstance(model, AuthConfig) and model.oidc is not None:
        _warn_extras(model.oidc, "auth.oidc.")
    if isinstance(model, ServerConfig):
        _warn_extras(model.auth, "auth.")


def load_config(path: Path) -> ServerConfig:
    if not path.is_file():
        msg = f"config file not found: {path}"
        raise ValueError(msg)
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"invalid TOML in {path}: {exc}"
        raise ValueError(msg) from exc

    apply_env_overrides(data, ENV_OVERRIDES, json_fields=_JSON_ENV_FIELDS)
    oidc = data.get("auth", {}).get("oidc") if isinstance(data.get("auth"), dict) else None
    if isinstance(oidc, dict):
        for key in ("client_id", "secret"):
            if isinstance(oidc.get(key), str):
                oidc[key] = resolve_secret(oidc[key], base_dir=path.parent)
    if isinstance(data.get("api_key"), str):
        data["api_key"] = resolve_secret(data["api_key"], base_dir=path.parent)
    if isinstance(data.get("database_url"), str):
        data["database_url"] = resolve_secret(data["database_url"], base_dir=path.parent)
    try:
        config = ServerConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"invalid server config in {path}: {exc}"
        raise ValueError(msg) from None
    _warn_extras(config, "")
    return config


def ensure_config(path: Path) -> None:
    existing = path.exists()
    if existing:
        content = path.read_bytes()
        data = tomllib.loads(content.decode("utf-8"))
        if data.get("api_key") or os.environ.get("MW_API_KEY"):
            return
    else:
        template = Path(__file__).resolve().parents[1] / "config.example.toml"
        content = template.read_bytes()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_dir = path.parent / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    if secret_dir.is_symlink() or stat.S_IMODE(secret_dir.stat().st_mode) != 0o700:
        msg = f"secrets directory must be private: {secret_dir}"
        raise ValueError(msg)
    secret_path = secret_dir / "api_key"
    if secret_path.is_symlink():
        msg = f"secret file must not be a symlink: {secret_path}"
        raise ValueError(msg)
    if secret_path.exists():
        if not secret_path.is_file() or stat.S_IMODE(secret_path.stat().st_mode) != 0o600:
            msg = f"secret file must be private: {secret_path}"
            raise ValueError(msg)
    else:
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(32) + "\n")
    if existing:
        document = tomlkit.parse(content.decode("utf-8"))
        document["api_key"] = "file:secrets/api_key"
        content = tomlkit.dumps(document).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if existing else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(content)
