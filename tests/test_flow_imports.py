from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.flows.definition import FlowDefinition
from server.flows.validation import validate_definition
from server.plugins.host import PluginHost

REPO_ROOT = Path(__file__).resolve().parents[1]
PRESET_DIR = REPO_ROOT / "server" / "src" / "server" / "presets"


@pytest.mark.asyncio
async def test_presets_validate() -> None:
    paths = sorted(PRESET_DIR.glob("*.flow.json"))
    assert paths
    host = PluginHost([REPO_ROOT / "plugins"])
    try:
        for path in paths:
            payload = json.loads(path.read_text())
            assert set(payload) == {
                "_type",
                "_version",
                "name",
                "description",
                "route",
                "concepts",
                "definition",
            }
            assert payload["_type"] == "flow"
            assert payload["_version"] == 1
            definition = FlowDefinition.model_validate(payload["definition"])
            assert definition.model_dump(by_alias=True, exclude_unset=True) == payload["definition"]
            assert set(payload["definition"]["_ui"]["nodes"]) == {"start"} | {node.id for node in definition.nodes}
            assert await validate_definition(host, definition) == [], path.name
    finally:
        await asyncio.wait_for(host.aclose(), 10)
