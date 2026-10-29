from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import stat
import tomllib
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomlkit
from common.enums import JobStatus
from common.fs.acquire import DIRECTORY_FLAGS, _check_entry, _check_parent, directory_fd, regular_fd
from common.secrets import resolve_secret
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import String, any_, delete, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.exc import IntegrityError

from server.core.config import ServerConfig
from server.flows.definition import FlowDefinition
from server.flows.store import get_flow, save_flow
from server.models import (
    FinalizationJournal,
    Flow,
    Job,
    JobAttempt,
    JobEvent,
    KeyValue,
    Library,
    MediaFile,
    NodeRow,
    NotificationDelivery,
    NotificationTarget,
    PluginState,
    ProbeRow,
    WebhookRegistration,
    utcnow,
)
from server.plugins.manifest import PluginManifest
from server.plugins.upload import (
    MAX_UPLOAD_BYTES,
    UploadError,
    archive_manifest,
    directory_manifest,
    install_plugin,
    plugin_snapshot,
)
from server.queue.store import create_library, update_library
from server.settings import SETTINGS_KEY, OperationalSettings, read_settings
from server.webhooks import WebhookBody
from server.workspace import recovery_jobs

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

_NAME: re.Pattern[str] = re.compile(r"^[\w][\w .-]{0,99}$")
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}\Z")
MAGIC = b"MWBK\x01"
SALT_SIZE = 16
NONCE_SIZE = 12
MAX_SIZE = 32 * 1024 * 1024
RESET_BUSY_STATUSES = (JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.VERIFYING, JobStatus.FINALIZING)


def _name(value: Any, label: str) -> str:
    valid = isinstance(value, str) and bool(value) and (_NAME.fullmatch(value) if label == "notification" else True)
    if not valid:
        msg = f"invalid {label} name"
        raise ValueError(msg)
    return value


def _items(data: dict, key: str) -> list[dict]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        msg = f"{key} must be a list of objects"
        raise ValueError(msg)
    field = "id" if key == "plugin_states" else "name"
    names = [_name(item.get(field), "plugin" if key == "plugin_states" else key.removesuffix("s")) for item in value]
    if len(set(names)) != len(names):
        raise ValueError("duplicate backup item")
    return value


async def export_data(session: AsyncSession) -> dict:
    # Pin one snapshot before reading related tables across separate awaits.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execution_options(read_only=True)
    )
    flows = (await session.scalars(select(Flow).order_by(Flow.name))).all()
    libraries = (await session.scalars(select(Library).order_by(Library.name))).all()
    notifications = (await session.scalars(select(NotificationTarget).order_by(NotificationTarget.name))).all()
    plugins = (await session.scalars(select(PluginState).order_by(PluginState.plugin_id))).all()
    nodes = (await session.scalars(select(NodeRow).order_by(NodeRow.name))).all()
    webhooks = (await session.scalars(select(WebhookRegistration).order_by(WebhookRegistration.name))).all()
    flow_names = {flow.id: flow.name for flow in flows}
    return {
        "settings": (await read_settings(session)).model_dump(mode="json"),
        "flows": [
            {
                "name": flow.name,
                "definition": json.loads(flow.definition),
            }
            for flow in flows
        ],
        "libraries": [
            {
                "name": library.name,
                "roots": library.roots,
                "cooldown_s": library.cooldown_s,
                "schedule": library.schedule,
                "watch": library.watch,
                "suspended": library.suspended,
                "scan_on_start": library.scan_on_start,
                "scan_interval_s": library.scan_interval_s,
                "mkv_stats": library.mkv_stats,
                "healthcheck_enabled": library.healthcheck_enabled,
                "processing_cutoff": library.processing_cutoff,
                "trash_mode": library.trash_mode,
                "trash_retention_days": library.trash_retention_days,
                "flow_name": flow_names.get(library.flow_id),
            }
            for library in libraries
        ],
        "notifications": [{"name": target.name, "url": target.url} for target in notifications],
        "webhooks": [
            {"name": row.name, "type": row.type, "api_key": row.api_key, "path_replacements": row.path_replacements}
            for row in webhooks
        ],
        "plugin_states": [{"id": state.plugin_id, "enabled": state.enabled} for state in plugins],
        "nodes": [
            {
                "name": node.name,
                "display_name": node.display_name,
                "token_hash": node.token_hash,
                "paused": node.paused,
                "transcode_limit": node.transcode_limit,
                "healthcheck_limit": node.healthcheck_limit,
                "mkv_stats_limit": node.mkv_stats_limit,
            }
            for node in nodes
        ],
    }


async def reset_data(session: AsyncSession, keep: set[str]) -> None:
    categories = {"flows", "libraries", "nodes", "notifications", "plugins", "webhooks", "settings"}
    if not keep <= categories:
        raise ValueError("invalid reset category")

    # Reset owns the whole dataset. Table locks also exclude inserts that row
    # locks cannot cover while the active-work check and deletion run.
    tables = (
        Library,
        MediaFile,
        Job,
        JobAttempt,
        JobEvent,
        ProbeRow,
        FinalizationJournal,
        Flow,
        NodeRow,
        NotificationTarget,
        NotificationDelivery,
        PluginState,
        WebhookRegistration,
        KeyValue,
    )
    await session.execute(
        text(f"LOCK TABLE {', '.join(model.__tablename__ for model in tables)} IN SHARE ROW EXCLUSIVE MODE")
    )
    protected = await session.scalar(
        select(Job.id).where(or_(Job.status.in_(RESET_BUSY_STATUSES), Job.id.in_(recovery_jobs()))).limit(1)
    )
    if protected is not None:
        raise ValueError("cannot reset while work is active or recovery is pending")

    await session.execute(delete(NotificationDelivery))
    await session.execute(delete(FinalizationJournal))
    await session.execute(delete(JobEvent))
    await session.execute(delete(JobAttempt))
    await session.execute(delete(Job))
    await session.execute(update(MediaFile).values(probe_id=None))
    await session.execute(delete(ProbeRow))
    await session.execute(delete(MediaFile))
    if "libraries" not in keep:
        await session.execute(delete(Library))
    else:
        await session.execute(update(Library).values(suspended=True))
        if "flows" not in keep:
            await session.execute(update(Library).values(flow_id=None))
    if "flows" not in keep:
        await session.execute(delete(Flow))
    if "nodes" not in keep:
        await session.execute(delete(NodeRow))
    if "plugins" not in keep:
        await session.execute(delete(PluginState))
    if "notifications" not in keep:
        await session.execute(delete(NotificationTarget))
    if "webhooks" not in keep:
        await session.execute(delete(WebhookRegistration))
    if "settings" not in keep:
        await session.execute(delete(KeyValue).where(KeyValue.key == SETTINGS_KEY))


async def restore_data(session: AsyncSession, data: dict, selection: dict) -> None:
    from server.api.libraries import LibraryBody
    from server.api.notifications import _validate_url

    if not isinstance(data, dict) or not isinstance(selection, dict):
        raise TypeError("data and selection must be objects")
    replace = selection.get("replace", False)
    if not isinstance(replace, bool):
        raise ValueError("replace must be a boolean")
    restore_settings = selection.get("settings", False)
    if not isinstance(restore_settings, bool):
        raise ValueError("settings selection must be a boolean")
    settings = OperationalSettings.model_validate(data["settings"]) if restore_settings else None
    selected: dict[str, list[str]] = {}
    for key in ("flows", "libraries", "notifications", "plugins", "nodes", "webhooks"):
        values = selection.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            msg = f"selection {key} must be a list of names"
            raise ValueError(msg)
        selected[key] = values

    flows = {}
    for item in _items(data, "flows"):
        name = _name(item.get("name"), "flow")
        if name in selected["flows"]:
            flows[name] = FlowDefinition.model_validate(item.get("definition"))
    libraries: dict[str, LibraryBody] = {}
    roots_override = selection.get("roots", {})
    if not isinstance(roots_override, dict):
        raise ValueError("roots must be an object")
    for item in _items(data, "libraries"):
        name = _name(item.get("name"), "library")
        if name not in selected["libraries"]:
            continue
        payload = {
            key: item[key]
            for key in (
                "name",
                "roots",
                "cooldown_s",
                "schedule",
                "watch",
                "suspended",
                "scan_on_start",
                "scan_interval_s",
                "mkv_stats",
                "healthcheck_enabled",
                "processing_cutoff",
                "trash_mode",
                "trash_retention_days",
            )
            if key in item
        }
        payload["roots"] = roots_override.get(name, payload["roots"])
        payload["watch"] = False
        payload["scan_on_start"] = False
        payload["suspended"] = True
        libraries[name] = LibraryBody.model_validate(payload)
    notifications = {}
    for item in _items(data, "notifications"):
        name = _name(item.get("name"), "notification")
        if name in selected["notifications"]:
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("invalid notification URL")
            _validate_url(url)
            notifications[name] = {"url": url}
    webhooks = {}
    for item in _items(data, "webhooks"):
        name = _name(item.get("name"), "webhook")
        if name in selected["webhooks"]:
            if not _NAME.fullmatch(name) or not isinstance(item.get("api_key"), str) or not item["api_key"]:
                raise ValueError("invalid webhook name or API key")
            body = WebhookBody.model_validate({key: item[key] for key in ("type", "path_replacements")})
            webhooks[name] = {**body.model_dump(mode="json"), "api_key": item["api_key"]}
    plugins = {}
    for item in _items(data, "plugin_states"):
        plugin_id = _name(item.get("id"), "plugin")
        if plugin_id in selected["plugins"]:
            plugins[plugin_id] = item
    nodes = {}
    for item in _items(data, "nodes"):
        name = _name(item.get("name"), "node")
        if name not in selected["nodes"]:
            continue
        token_hash = item.get("token_hash")
        display_name = item.get("display_name")
        limit = item.get("transcode_limit")
        capacities = [item.get("healthcheck_limit"), item.get("mkv_stats_limit")]
        if (
            not isinstance(token_hash, str)
            or not _TOKEN_HASH.fullmatch(token_hash)
            or not isinstance(display_name, str)
            or not display_name.strip()
            or not isinstance(item.get("paused"), bool)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in capacities)
        ):
            raise ValueError("invalid node credential or settings")
        nodes[name] = item

    from server.queue.store import ACTIVE_STATUSES

    # Configuration replacement locks only the selected resources and libraries
    # using the selected flows before checking their active work.
    await session.execute(
        select(Library.id)
        .where(
            or_(
                Library.name == any_(literal(list(libraries), ARRAY(String))),
                Library.flow_id.in_(select(Flow.id).where(Flow.name == any_(literal(list(flows), ARRAY(String))))),
            )
        )
        .order_by(Library.id)
        .with_for_update()
    )
    for model, column, names in (
        (Flow, Flow.name, flows),
        (NotificationTarget, NotificationTarget.name, notifications),
        (PluginState, PluginState.plugin_id, plugins),
        (NodeRow, NodeRow.name, nodes),
        (WebhookRegistration, WebhookRegistration.name, webhooks),
    ):
        await session.execute(
            select(model.id)
            .where(column == any_(literal(list(names), ARRAY(String))))
            .order_by(model.id)
            .with_for_update()
        )
    existing_flows = {
        row.name: row for row in (await session.scalars(select(Flow).execution_options(populate_existing=True))).all()
    }
    existing_libraries = {
        row.name: row
        for row in (await session.scalars(select(Library).execution_options(populate_existing=True))).all()
    }
    existing_notifications = {
        row.name: row
        for row in (await session.scalars(select(NotificationTarget).execution_options(populate_existing=True))).all()
    }
    existing_plugins = {
        row.plugin_id: row
        for row in (await session.scalars(select(PluginState).execution_options(populate_existing=True))).all()
    }
    existing_nodes = {
        row.name: row
        for row in (await session.scalars(select(NodeRow).execution_options(populate_existing=True))).all()
    }
    existing_webhooks = {
        row.name: row
        for row in (await session.scalars(select(WebhookRegistration).execution_options(populate_existing=True))).all()
    }
    for column, names in ((Library.name, libraries), (Flow.name, flows)):
        affected = (
            select(Library.id)
            .outerjoin(Flow, Flow.id == Library.flow_id)
            .where(column == any_(literal(list(names), ARRAY(String))))
        )
        busy = await session.scalar(
            select(Job.id)
            .where(
                or_(Job.status.in_([status.value for status in ACTIVE_STATUSES]), Job.id.in_(recovery_jobs())),
                Job.library_id.in_(affected),
            )
            .limit(1)
        )
        if busy is not None:
            raise ValueError("cannot replace configuration while library work is active or recovery is pending")
    for values, existing, label in (
        (flows, existing_flows, "flow"),
        (libraries, existing_libraries, "library"),
        (notifications, existing_notifications, "notification"),
        (plugins, existing_plugins, "plugin"),
        (nodes, existing_nodes, "node"),
        (webhooks, existing_webhooks, "webhook"),
    ):
        conflicts = set(values) & set(existing)
        if not replace and conflicts:
            msg = f"{label} already exists: {', '.join(sorted(conflicts))}"
            raise ValueError(msg)
    available = {
        key: {
            _name(
                item.get("id" if key == "plugin_states" else "name"), "plugin" if key == "plugin_states" else key[:-1]
            )
            for item in _items(data, key)
        }
        for key in ("flows", "libraries", "notifications", "plugin_states", "nodes", "webhooks")
    }
    for key, values in (
        ("flows", selected["flows"]),
        ("libraries", selected["libraries"]),
        ("notifications", selected["notifications"]),
        ("plugin_states", selected["plugins"]),
        ("nodes", selected["nodes"]),
        ("webhooks", selected["webhooks"]),
    ):
        missing = set(values) - available[key]
        if missing:
            msg = f"selected {key} not found: {sorted(missing)!r}"
            raise ValueError(msg)
    for library in libraries.values():
        flow_name = next(
            (item.get("flow_name") for item in _items(data, "libraries") if item.get("name") == library.name), None
        )
        if flow_name is not None and flow_name not in selected["flows"]:
            msg = f"select flow {flow_name} to restore the library"
            raise ValueError(msg)

    try:
        flow_rows = {}
        for name, definition in flows.items():
            existing = await get_flow(session, name)
            flow_rows[name] = await save_flow(
                session, name, definition, expected_version=existing.version if existing else None
            )
        for name, body in libraries.items():
            raw = next(item for item in _items(data, "libraries") if item.get("name") == name)
            flow_name = raw.get("flow_name")
            flow_row = flow_rows[flow_name] if flow_name else None
            fields = body.model_dump(exclude={"name"})
            fields["flow_id"] = flow_row.id if flow_row else None
            if name in existing_libraries:
                await update_library(session, existing_libraries[name].id, fields)
            else:
                await create_library(session, name=name, **fields)
        for name, item in notifications.items():
            target = existing_notifications.get(name) or NotificationTarget(name=name, url=item["url"])
            target.url = item["url"]
            session.add(target)
        for name, item in webhooks.items():
            registration = existing_webhooks.get(name) or WebhookRegistration(name=name)
            registration.type = item["type"]
            registration.api_key = item["api_key"]
            registration.path_replacements = item["path_replacements"]
            session.add(registration)
        for plugin_id, item in plugins.items():
            state = existing_plugins.get(plugin_id) or PluginState(plugin_id=plugin_id)
            enabled = item.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("invalid plugin state")
            state.enabled = enabled and plugin_id not in data.get("uploaded_plugins", {})
            session.add(state)
        for name, item in nodes.items():
            node = existing_nodes.get(name) or NodeRow(name=name)
            if name in existing_nodes and node.state != "offline":
                raise ValueError("only offline nodes may be replaced")
            if (
                name in existing_nodes
                and await session.scalar(
                    select(Job.id)
                    .where(Job.node_id == node.id, Job.status.in_([status.value for status in ACTIVE_STATUSES]))
                    .limit(1)
                )
                is not None
            ):
                raise ValueError("cannot replace credentials while node work is active")
            node.token_hash = item["token_hash"]
            node.display_name = item["display_name"]
            node.paused = item["paused"]
            node.transcode_limit = item["transcode_limit"]
            node.healthcheck_limit = item["healthcheck_limit"]
            node.mkv_stats_limit = item["mkv_stats_limit"]
            node.state = "offline"
            session.add(node)
        if settings is not None:
            statement = insert(KeyValue).values(key=SETTINGS_KEY, value=settings.model_dump(mode="json"))
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[KeyValue.key],
                    set_={"value": statement.excluded.value, "updated_at": utcnow()},
                )
            )
        await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != "23505":
            raise
        await session.rollback()
        raise ValueError("selected item already exists") from None


def _key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def encrypt(payload: bytes, password: str) -> bytes:
    if len(payload) + len(MAGIC) + SALT_SIZE + NONCE_SIZE + 16 >= MAX_SIZE:
        raise ValueError("backup is too large")
    salt = secrets.token_bytes(SALT_SIZE)
    nonce = secrets.token_bytes(NONCE_SIZE)
    header = MAGIC + salt + nonce
    return header + AESGCM(_key(password, salt)).encrypt(nonce, payload, header)


def decrypt(data: bytes, password: str) -> bytes:
    if len(data) >= MAX_SIZE:
        raise ValueError("backup is too large")
    if len(data) < len(MAGIC) + SALT_SIZE + NONCE_SIZE + 16 or not data.startswith(MAGIC):
        raise ValueError("invalid backup or password")
    header = data[: len(MAGIC) + SALT_SIZE + NONCE_SIZE]
    salt = header[len(MAGIC) : len(MAGIC) + SALT_SIZE]
    nonce = header[-NONCE_SIZE:]
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, data[len(header) :], header)
    except (InvalidTag, ValueError, UnicodeError) as error:
        raise ValueError("invalid backup or password") from error


def export_config(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError("config path must not be a symlink")
    if not path.is_file():
        return None
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    auth = doc.get("auth")
    oidc = auth.get("oidc") if isinstance(auth, dict) else None
    if isinstance(oidc, dict):
        for key in ("client_id", "secret"):
            value = oidc.get(key)
            if isinstance(value, str) and value.startswith("file:"):
                oidc[key] = resolve_secret(value, base_dir=path.parent)
    for key in ("api_key", "node_enrollment_token", "database_url"):
        value = doc.get(key)
        if isinstance(value, str) and value.startswith("file:"):
            doc[key] = resolve_secret(value, base_dir=path.parent)
    return doc.as_string()


def validate_config(content: str) -> None:
    data = tomllib.loads(content)
    oidc = data.get("auth", {}).get("oidc") if isinstance(data.get("auth"), dict) else None
    if isinstance(oidc, dict) and any(
        isinstance(oidc.get(key), str) and oidc[key].startswith("file:") for key in ("client_id", "secret")
    ):
        raise ValueError("backup config contains unresolved secret references")
    if any(
        isinstance(data.get(key), str) and data[key].startswith("file:")
        for key in ("node_enrollment_token", "api_key", "database_url")
    ):
        raise ValueError("backup config contains unresolved secret references")
    ServerConfig.model_validate(data)


def prepared_config(path: Path, content: str) -> tuple[str, dict[str, str]]:
    validate_config(content)
    doc = tomlkit.parse(content)
    values = {}
    generation = secrets.token_hex(16)
    for key in ("api_key", "node_enrollment_token", "database_url"):
        value = doc.get(key)
        if isinstance(value, str):
            name = f"{generation}-{key}"
            values[name] = value
            doc[key] = f"file:secrets/{name}"
    auth = doc.get("auth")
    oidc = auth.get("oidc") if isinstance(auth, dict) else None
    if isinstance(oidc, dict):
        for key in ("client_id", "secret"):
            value = oidc.get(key)
            if isinstance(value, str):
                name = f"{generation}-" + ("oidc_client_id" if key == "client_id" else "oidc_client_secret")
                values[name] = value
                oidc[key] = f"file:secrets/{name}"
    directory = path.parent / "secrets"
    if directory.is_symlink() or (
        directory.exists() and (not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) != 0o700)
    ):
        raise ValueError("secrets directory must be private")
    for name in values:
        target = directory / name
        if target.is_symlink() or target.exists() or (directory / f".{name}.partial").exists():
            raise ValueError("secret destination is unsafe")
    return doc.as_string(), values


def restore_config(path: Path, content: str) -> None:
    check_config_destination(path)
    content, values = prepared_config(path, content)
    with directory_fd(path.parent, create=True) as parent_fd:
        parent_identity = os.fstat(parent_fd)
        config_name = path.name
        partial_name = f".{config_name}.partial"
        try:
            original = os.stat(config_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            original = None
        secrets_name = "secrets"
        try:
            os.mkdir(secrets_name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        secrets_fd = os.open(secrets_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        secrets_identity = os.fstat(secrets_fd)
        if stat.S_IMODE(secrets_identity.st_mode) != 0o700:
            os.close(secrets_fd)
            raise ValueError("secrets directory must be private")
        published: list[tuple[str, int]] = []
        partial_stat: os.stat_result | None = None
        try:
            for name, value in values.items():
                secret_partial = f".{name}.partial"
                fd = os.open(
                    secret_partial,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=secrets_fd,
                )
                partial_stat = os.fstat(fd)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as output:
                        output.write(value + "\n")
                        output.flush()
                        os.fsync(output.fileno())
                    current_partial = os.stat(secret_partial, dir_fd=secrets_fd, follow_symlinks=False)
                    if (current_partial.st_dev, current_partial.st_ino) != (partial_stat.st_dev, partial_stat.st_ino):
                        raise ValueError("secret staging file changed during restore")
                    current_parent = os.stat(path.parent, follow_symlinks=False)
                    if (current_parent.st_dev, current_parent.st_ino) != (
                        parent_identity.st_dev,
                        parent_identity.st_ino,
                    ):
                        raise ValueError("config directory changed during restore")
                    current_secrets = os.stat(secrets_name, dir_fd=parent_fd, follow_symlinks=False)
                    if (current_secrets.st_dev, current_secrets.st_ino) != (
                        secrets_identity.st_dev,
                        secrets_identity.st_ino,
                    ):
                        raise ValueError("secrets directory changed during restore")
                    os.link(secret_partial, name, src_dir_fd=secrets_fd, dst_dir_fd=secrets_fd, follow_symlinks=False)
                    published_stat = os.stat(name, dir_fd=secrets_fd, follow_symlinks=False)
                    if (published_stat.st_dev, published_stat.st_ino) != (partial_stat.st_dev, partial_stat.st_ino):
                        raise ValueError("secret publication source changed during restore")
                    published.append((name, partial_stat.st_ino))
                finally:
                    os.close(fd)
                    try:
                        current = os.stat(secret_partial, dir_fd=secrets_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if current is not None and partial_stat is not None and current.st_ino == partial_stat.st_ino:
                        os.unlink(secret_partial, dir_fd=secrets_fd)
                    partial_stat = None
            staged: os.stat_result | None = None
            fd = os.open(partial_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
            staged = os.fstat(fd)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                current_staged = os.stat(partial_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current_staged.st_dev, current_staged.st_ino) != (staged.st_dev, staged.st_ino):
                    raise ValueError("config staging file changed during restore")
                if original is not None:
                    current = os.stat(config_name, dir_fd=parent_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                        raise ValueError("config destination changed during restore")
                else:
                    try:
                        os.stat(config_name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError("config destination appeared during restore")
                current_parent = os.stat(path.parent, follow_symlinks=False)
                if (current_parent.st_dev, current_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
                    raise ValueError("config directory changed during restore")
                current_secrets = os.stat(secrets_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current_secrets.st_dev, current_secrets.st_ino) != (
                    secrets_identity.st_dev,
                    secrets_identity.st_ino,
                ):
                    raise ValueError("secrets directory changed during restore")
                for name, inode in published:
                    current = os.stat(name, dir_fd=secrets_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (secrets_identity.st_dev, inode) or current.st_nlink != 1:
                        raise ValueError("secret generation changed during restore")
                os.replace(partial_name, config_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            finally:
                os.close(fd)
                try:
                    current = os.stat(partial_name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if staged is not None and current is not None and current.st_ino == staged.st_ino:
                    os.unlink(partial_name, dir_fd=parent_fd)
        except BaseException:
            for name, inode in published:
                try:
                    current = os.stat(name, dir_fd=secrets_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if current.st_ino == inode:
                    os.unlink(name, dir_fd=secrets_fd)
            raise
        finally:
            os.close(secrets_fd)


def check_config_destination(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("config path must not be a symlink")
    if path.with_name(f".{path.name}.partial").exists():
        raise ValueError("config partial file already exists")


def export_plugins(root: Path) -> dict[str, str]:
    archives: dict[str, str] = {}
    if not root.exists() and not root.is_symlink():
        return archives
    with directory_fd(root) as root_fd:
        for name in sorted(os.listdir(root_fd)):
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if name.startswith(".") or not stat.S_ISDIR(metadata.st_mode):
                continue
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
            try:
                _check_entry(Path(name), root_fd, fd)
                try:
                    manifest = directory_manifest(fd)
                except FileNotFoundError:
                    continue
                if manifest.id != name:
                    raise ValueError("plugin manifest id does not match installed directory")
                snapshot = plugin_snapshot(fd)
                buffer = io.BytesIO()
                total = 0

                def archive_directory(
                    parent_fd: int, prefix: str, output: zipfile.ZipFile, buffer: io.BytesIO = buffer
                ) -> None:
                    nonlocal total
                    for leaf in sorted(os.listdir(parent_fd)):
                        entry = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                        relative = prefix + leaf
                        if stat.S_ISDIR(entry.st_mode):
                            child = os.open(leaf, DIRECTORY_FLAGS, dir_fd=parent_fd)
                            try:
                                _check_entry(Path(leaf), parent_fd, child)
                                archive_directory(child, relative + "/", output)
                                _check_entry(Path(leaf), parent_fd, child)
                            finally:
                                os.close(child)
                        else:
                            with regular_fd(leaf, parent_fd) as source_fd:
                                if os.fstat(source_fd).st_nlink != 1:
                                    raise ValueError("uploaded plugin contains a hardlink")
                                with os.fdopen(os.dup(source_fd), "rb") as source, output.open(relative, "w") as target:
                                    while chunk := source.read(1024 * 1024):
                                        total += len(chunk)
                                        if total > MAX_SIZE:
                                            raise ValueError("uploaded plugin is too large to back up")
                                        target.write(chunk)
                                        if buffer.tell() >= MAX_SIZE:
                                            raise ValueError("backup is too large")
                                _check_entry(Path(leaf), parent_fd, source_fd)

                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
                    archive_directory(fd, "", output)
                _check_entry(Path(name), root_fd, fd)
                _check_parent(root / name, root_fd)
                if plugin_snapshot(fd) != snapshot or directory_manifest(fd) != manifest:
                    raise ValueError("uploaded plugin generation changed during backup")
                if len(buffer.getvalue()) > MAX_UPLOAD_BYTES:
                    raise ValueError("uploaded plugin is too large to back up")
                archives[name] = base64.b64encode(buffer.getvalue()).decode("ascii")
            finally:
                os.close(fd)
    return archives


def _read_plugin(encoded: str) -> tuple[bytes, PluginManifest]:
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("uploaded plugin is too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _, manifest = archive_manifest(archive)
    return data, manifest


def plugin_archive(name: str, encoded: str, *, bundled_taken: Callable[[str], bool]) -> bytes:
    if not name or Path(name).name != name or name.startswith("."):
        raise ValueError("invalid plugin name")
    try:
        data, manifest = _read_plugin(encoded)
    except (ValueError, OSError, UnicodeError, KeyError, zipfile.BadZipFile) as exc:
        msg = f"invalid plugin archive: {name}"
        raise ValueError(msg) from exc
    if manifest.id != name or bundled_taken(name):
        raise ValueError("plugin manifest id does not match backup entry")
    return data


def restore_plugin(root: Path, name: str, data: bytes, *, replace: bool) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            _, manifest = archive_manifest(archive)
        if manifest.id != name:
            raise ValueError("plugin manifest id does not match backup entry")
        install_plugin(data, root, bundled_taken=lambda _: False, replace=replace)
    except (UploadError, OSError, zipfile.BadZipFile) as exc:
        msg = f"invalid plugin archive: {name}"
        raise ValueError(msg) from exc
