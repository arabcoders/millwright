from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from common.ctx import PluginContext
from common.plugin import build_inputs, input_fields
from pydantic import BaseModel

from server.plugins.manifest import PluginManifest, load_manifest

if TYPE_CHECKING:
    from types import ModuleType


class InputError(ValueError):
    # The type name crosses the wire. The host does not count it toward degradation.
    pass


@dataclass(kw_only=True, slots=True)
class _Plugin:
    manifest: PluginManifest
    module: ModuleType
    inputs: type[BaseModel] | None


def _emit(response: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("ffmpeg_converter_plugin", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load plugin module at {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    # Pydantic resolves annotations through sys.modules. Without registration,
    # string annotations (PEP 563) stay unresolved and INPUTS breaks
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _inputs_model(module: ModuleType) -> type[BaseModel] | None:
    model = getattr(module, "INPUTS", None)
    if model is None:
        return None
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        msg = f"INPUTS must be a pydantic BaseModel subclass, got {model!r}"
        raise TypeError(msg)
    return model


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _normalize_decision(result: Any) -> dict[str, Any]:
    data = _plain(result)
    if not isinstance(data, dict):
        msg = f"decision must be an object, got {type(result).__name__}"
        raise TypeError(msg)
    match data.get("type"):
        case "match":
            facets = data.get("facets", {})
            if not isinstance(facets, dict):
                msg = "match decision facets must be an object"
                raise ValueError(msg)
            for key in facets:
                if not isinstance(key, str) or not key or key.startswith("media.") or "." in key:
                    msg = f"invalid facet key: {key!r}"
                    raise ValueError(msg)
            return {"type": "match", "facets": facets}
        case "veto" | "skip":
            reason = data.get("reason")
            if not isinstance(reason, str):
                msg = f"{data['type']} decision requires a string reason"
                raise ValueError(msg)
            return {"type": data["type"], "reason": reason}
        case "error":
            error = data.get("error")
            if not isinstance(error, str) or not error:
                msg = "error decision requires a nonempty string error"
                raise ValueError(msg)
            return {"type": "error", "error": error}
        case "plan":
            plan = data.get("plan")
            if not isinstance(plan, dict):
                msg = "plan decision requires a plan object"
                raise ValueError(msg)
            reason = data.get("reason")
            if reason is not None and not isinstance(reason, str):
                msg = "plan decision reason must be a string"
                raise ValueError(msg)
            decision = {"type": "plan", "plan": plan}
            if reason is not None:
                decision["reason"] = reason
            return decision
        case "actions":
            actions = data.get("actions", [])
            if not isinstance(actions, list):
                msg = "actions decision requires a list"
                raise ValueError(msg)
            return {"type": "actions", "actions": actions}
        case other:
            msg = f"unsupported decision type: {other!r}"
            raise ValueError(msg)


def _describe(plugin: _Plugin) -> dict[str, Any]:
    fields = input_fields(plugin.inputs) if plugin.inputs is not None else []
    return {"manifest": plugin.manifest.model_dump(mode="json"), "inputs": fields}


def _decide(plugin: _Plugin, request: dict[str, Any]) -> dict[str, Any]:
    ctx = PluginContext.model_validate(request.get("ctx"))
    raw = request.get("inputs")
    inputs = _instantiate(plugin, raw if isinstance(raw, dict) else {})
    result = plugin.module.decide(ctx, inputs)
    return _normalize_decision(result)


def _instantiate(plugin: _Plugin, raw: dict[str, Any]) -> Any:
    if plugin.inputs is None:
        return raw
    try:
        return build_inputs(plugin.inputs, raw)
    except ValueError as exc:
        raise InputError(str(exc)) from None


def _handle(plugin: _Plugin, request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "ping":
        return {"pong": True}
    if op == "describe":
        return _describe(plugin)
    if op == "validate_inputs":
        _instantiate(plugin, request["inputs"])
        return {"valid": True}
    if op == "decide":
        return {"decision": _decide(plugin, request)}
    msg = f"unsupported op: {op!r}"
    raise ValueError(msg)


def _respond(plugin: _Plugin, line: str) -> dict[str, Any]:
    try:
        return _handle(plugin, _parse_request(line))
    except Exception as exc:
        return {"error": {"message": str(exc), "type": type(exc).__name__}}


def _parse_request(line: str) -> dict[str, Any]:
    request = json.loads(line)
    if not isinstance(request, dict):
        msg = "request must be a JSON object"
        raise ValueError(msg)
    return request


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        _emit({"error": {"message": "usage: python -m server.plugins.runner <plugin_dir>", "type": "UsageError"}})
        return 2
    # Importing plugin.py must not trigger the watcher by creating __pycache__.
    sys.dont_write_bytecode = True
    plugin_dir = Path(args[0])
    try:
        manifest = load_manifest(plugin_dir)
        module = _load_module(plugin_dir / "plugin.py")
        plugin = _Plugin(manifest=manifest, module=module, inputs=_inputs_model(module))
    except Exception as exc:
        _emit({"error": {"message": f"{type(exc).__name__}: {exc}", "type": "RunnerStartupError"}})
        return 1
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        _emit(_respond(plugin, line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
