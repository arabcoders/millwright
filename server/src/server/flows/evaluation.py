from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from common.ctx import PluginContext

from server.flows.definition import FlowDefinition
from server.flows.engine import evaluate
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from server.plugins.migrations import migrate_definition
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from common.media.probe import ProbeData

    from server.flows.engine import FlowResult
    from server.models import Flow, Library
    from server.plugins.host import PluginHost


async def evaluate_file(
    path: str, probe: ProbeData, library: Library, flow: Flow, host: PluginHost
) -> tuple[FlowResult, dict]:
    definition = FlowDefinition.model_validate(
        migrate_definition(json.loads(flow.definition), (info.manifest for info in host.plugins()))
    )
    ctx = PluginContext(
        probe=probe,
        original_filename=Path(path).name,
        library={"name": library.name},
        sidecar_subtitles=await wait_workspace_io(lambda: sidecar_subtitles(path)),
        sidecar_metadata=await wait_workspace_io(lambda: sidecar_metadata(path, definition)),
    )
    result = await evaluate(definition, ctx, host)
    snapshot = {
        "id": flow.id,
        "name": flow.name,
        "version": flow.version,
        "definition": definition.model_dump(mode="json"),
    }
    return result, snapshot
