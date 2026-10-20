from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.flows.definition import FlowDefinition

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Self

    from server.plugins.manifest import PluginManifest


class RenamePlugin(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    type: Literal["rename_plugin"]
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)


class RenameField(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    type: Literal["rename_field"]
    scope: Literal["inputs", "action", "event"] = "inputs"
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)


class RenameAction(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    type: Literal["rename_action"]
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)


class RenameEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    type: Literal["rename_event"]
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)


class RenameTargetPrefix(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    type: Literal["rename_target_prefix"]
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1)


ConfigChange = Annotated[
    RenamePlugin | RenameField | RenameAction | RenameEvent | RenameTargetPrefix, Field(discriminator="type")
]


class ConfigMigration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_version: int = Field(ge=1, strict=True)
    to_version: int = Field(ge=1, strict=True)
    changes: list[ConfigChange] = Field(min_length=1)

    @model_validator(mode="after")
    def check_versions(self) -> Self:
        if self.to_version <= self.from_version:
            msg = "A migration must increase the configuration version."
            raise ValueError(msg)
        return self


@dataclass(kw_only=True, slots=True)
class AppliedMigration:
    plugin_id: str
    record: str
    from_version: int
    to_version: int
    changes: list[str]


def find_manifest(plugin_id: str, manifests: Iterable[PluginManifest]) -> PluginManifest | None:
    matches = [
        manifest
        for manifest in manifests
        if plugin_id == manifest.id
        or any(
            isinstance(change, RenamePlugin) and plugin_id in (change.source, change.target)
            for migration in manifest.migrations
            for change in migration.changes
        )
    ]
    if len(matches) > 1:
        names = ", ".join(sorted(manifest.id for manifest in matches))
        msg = f"Plugin ID {plugin_id!r} is claimed by multiple plugins: {names}."
        raise ValueError(msg)
    return matches[0] if matches else None


def _steps(manifest: PluginManifest, version: int, label: str) -> list[ConfigMigration]:
    if type(version) is not int or version < 1:
        msg = f"{label} has an invalid configuration version."
        raise ValueError(msg)
    if version > manifest.config_version:
        msg = (
            f"{label} needs {manifest.id} configuration version {version}. "
            f"The installed plugin supports version {manifest.config_version}."
        )
        raise ValueError(msg)
    available = {migration.from_version: migration for migration in manifest.migrations}
    selected = []
    while version < manifest.config_version:
        migration = available.get(version)
        if migration is None:
            msg = f"{label}: {manifest.id} has no migration from configuration version {version}."
            raise ValueError(msg)
        selected.append(migration)
        version = migration.to_version
    return selected


def _rename_field(record: dict[str, Any], change: RenameField, label: str) -> bool:
    if change.source not in record or change.source == change.target:
        return False
    if change.target in record and record[change.target] != record[change.source]:
        msg = f"{label} has conflicting fields {change.source!r} and {change.target!r}."
        raise ValueError(msg)
    record[change.target] = record.pop(change.source)
    return True


def migrate_plugin_id(
    plugin_id: str,
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "plugin reference",
) -> str:
    manifest = find_manifest(plugin_id, manifests)
    if manifest is None or plugin_id == manifest.id:
        return plugin_id
    result = plugin_id
    for migration in sorted(manifest.migrations, key=lambda item: item.from_version):
        changes = []
        for change in migration.changes:
            if isinstance(change, RenamePlugin) and result == change.source and result != change.target:
                result = change.target
                changes.append(f"rename_plugin: {change.source} -> {change.target}")
        if changes and applied is not None:
            applied.append(
                AppliedMigration(
                    plugin_id=manifest.id,
                    record=record,
                    from_version=migration.from_version,
                    to_version=migration.to_version,
                    changes=changes,
                )
            )
    if result != manifest.id:
        msg = f"{record} has no migration from plugin ID {plugin_id!r} to {manifest.id!r}."
        raise ValueError(msg)
    return result


def migrate_definition(
    definition: FlowDefinition | dict[str, Any],
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "flow",
) -> dict[str, Any]:
    manifests = list(manifests)
    migrated = definition.model_dump(mode="json") if isinstance(definition, FlowDefinition) else deepcopy(definition)
    # Validate without serializing defaults into unrelated stored configurations.
    FlowDefinition.model_validate({**migrated, "actions": []})
    for node in migrated.get("nodes", []):
        if node.get("type") != "plugin":
            continue
        manifest = find_manifest(node["plugin"], manifests)
        if manifest is None:
            continue
        label = f"{record} step {node.get('id')!r}"
        for migration in _steps(manifest, node.get("config_version", 1), label):
            changes = []
            for change in migration.changes:
                if isinstance(change, RenamePlugin):
                    if node["plugin"] == change.source and change.source != change.target:
                        node["plugin"] = change.target
                        changes.append(f"rename_plugin: {change.source} -> {change.target}")
                elif (
                    isinstance(change, RenameField)
                    and change.scope == "inputs"
                    and _rename_field(node.get("inputs", {}), change, label)
                ):
                    changes.append(f"rename_field (inputs): {change.source} -> {change.target}")
            node["config_version"] = migration.to_version
            if applied is not None:
                applied.append(
                    AppliedMigration(
                        plugin_id=manifest.id,
                        record=label,
                        from_version=migration.from_version,
                        to_version=migration.to_version,
                        changes=changes,
                    )
                )
        if node["plugin"] != manifest.id:
            msg = f"{label} has no migration from plugin ID {node['plugin']!r} to {manifest.id!r}."
            raise ValueError(msg)
    if "actions" in migrated:
        migrated["actions"] = _actions(migrated["actions"], manifests, applied=applied, record=f"{record}.actions")
    FlowDefinition.model_validate(migrated)
    return migrated


def _action_manifest(action: dict[str, Any], manifests: list[PluginManifest]) -> PluginManifest | None:
    if action.get("plugin_id") is not None:
        return find_manifest(action["plugin_id"], manifests)
    matches = [
        manifest
        for manifest in manifests
        if any(
            isinstance(change, RenameAction) and action.get("kind") in (change.source, change.target)
            for migration in manifest.migrations
            for change in migration.changes
        )
    ]
    if len(matches) > 1:
        names = ", ".join(sorted(manifest.id for manifest in matches))
        msg = f"Action kind {action.get('kind')!r} is claimed by multiple plugin migrations: {names}."
        raise ValueError(msg)
    return matches[0] if matches else None


def migrate_action(
    action: dict[str, Any],
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "action",
) -> dict[str, Any]:
    manifests = list(manifests)
    result = deepcopy(action)
    manifest = _action_manifest(result, manifests)
    if manifest is None:
        return result
    steps = _steps(manifest, result.get("config_version", 1), record)
    for migration in steps:
        changes = []
        for change in migration.changes:
            if (
                isinstance(change, RenameAction)
                and result.get("kind") == change.source
                and change.source != change.target
            ):
                result["kind"] = change.target
                changes.append(f"rename_action: {change.source} -> {change.target}")
            elif (
                isinstance(change, RenamePlugin)
                and result.get("plugin_id") == change.source
                and change.source != change.target
            ):
                result["plugin_id"] = change.target
                changes.append(f"rename_plugin: {change.source} -> {change.target}")
            elif isinstance(change, RenameField) and change.scope == "action":
                if _rename_field(result, change, record):
                    changes.append(f"rename_field (action): {change.source} -> {change.target}")
        result["config_version"] = migration.to_version
        if migration is steps[-1] and result.get("plugin_id") is None:
            result["plugin_id"] = manifest.id
            changes.append(f"Recorded plugin_id {manifest.id}")
        if applied is not None:
            applied.append(
                AppliedMigration(
                    plugin_id=manifest.id,
                    record=record,
                    from_version=migration.from_version,
                    to_version=migration.to_version,
                    changes=changes,
                )
            )
    return result


def _actions(
    value: Any,
    manifests: list[PluginManifest],
    *,
    applied: list[AppliedMigration] | None,
    record: str,
) -> Any:
    if isinstance(value, list):
        return [
            _actions(item, manifests, applied=applied, record=f"{record}[{index}]") for index, item in enumerate(value)
        ]
    return migrate_action(value, manifests, applied=applied, record=record) if isinstance(value, dict) else value


def migrate_results(
    value: Any,
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "action results",
) -> Any:
    manifests = list(manifests)
    if isinstance(value, list):
        return [
            migrate_results(item, manifests, applied=applied, record=f"{record}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    manifest = _action_manifest(result, manifests)
    pending: list[AppliedMigration] = []
    if manifest is not None:
        for migration in _steps(manifest, result.get("config_version", 1), f"Action result {result.get('kind')!r}"):
            changes = []
            for change in migration.changes:
                if (
                    isinstance(change, RenameAction)
                    and result.get("kind") == change.source
                    and change.source != change.target
                ):
                    result["kind"] = change.target
                    changes.append(f"rename_action: {change.source} -> {change.target}")
            if changes:
                pending.append(
                    AppliedMigration(
                        plugin_id=manifest.id,
                        record=record,
                        from_version=migration.from_version,
                        to_version=migration.to_version,
                        changes=changes,
                    )
                )
    if result != value and applied is not None:
        applied.extend(pending)
    return result


def migrate_execution(
    value: Any,
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "execution",
) -> Any:
    manifests = list(manifests)
    if isinstance(value, list):
        return [
            migrate_execution(item, manifests, applied=applied, record=f"{record}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    if "entry" in value and "nodes" in value:
        return migrate_definition(value, manifests, applied=applied, record=record)
    result = deepcopy(value)
    if "node" in result and "outcome" in result and isinstance(result.get("step"), str):
        result["step"] = migrate_plugin_id(result["step"], manifests, applied=applied, record=f"{record}.step")
    # These are execution containers, not arbitrary plugin inputs, facets, or text.
    for key in ("action", "actions", "pre_actions", "post_actions", "publication_actions"):
        if key in result:
            result[key] = _actions(result[key], manifests, applied=applied, record=f"{record}.{key}")
    for key in ("action_results", "results"):
        if key in result:
            result[key] = migrate_results(result[key], manifests, applied=applied, record=f"{record}.{key}")
    for key in ("flow", "definition", "continuation", "flow_snapshot", "decision_log", "previous"):
        if key in result:
            result[key] = migrate_execution(result[key], manifests, applied=applied, record=f"{record}.{key}")
    return result


def migrate_event(
    event_type: str,
    payload: dict[str, Any],
    targets: list[str],
    manifests: Iterable[PluginManifest],
    *,
    applied: list[AppliedMigration] | None = None,
    record: str = "event",
) -> tuple[str, dict[str, Any], list[str]]:
    manifests = list(manifests)
    action = payload.get("action")
    manifest = _action_manifest(action, manifests) if isinstance(action, dict) else None
    result = migrate_execution(payload, manifests, applied=applied, record=f"{record}.payload")
    targets = list(targets)
    if manifest is not None and isinstance(action, dict):
        for migration in _steps(manifest, action.get("config_version", 1), f"Event {event_type!r}"):
            changes = []
            for change in migration.changes:
                if isinstance(change, RenameEvent) and event_type == change.source and change.source != change.target:
                    event_type = change.target
                    changes.append(f"rename_event: {change.source} -> {change.target}")
                elif isinstance(change, RenameField) and change.scope == "event":
                    if _rename_field(result, change, record):
                        changes.append(f"rename_field (event): {change.source} -> {change.target}")
                elif isinstance(change, RenameTargetPrefix):
                    for index, target in enumerate(targets):
                        if target.startswith(change.source) and change.source != change.target:
                            targets[index] = change.target + target[len(change.source) :]
                            changes.append(f"rename_target_prefix: {target} -> {targets[index]}")
            if changes and applied is not None:
                applied.append(
                    AppliedMigration(
                        plugin_id=manifest.id,
                        record=record,
                        from_version=migration.from_version,
                        to_version=migration.to_version,
                        changes=changes,
                    )
                )
    return event_type, result, targets
