from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.ctx import PluginContext


def decide(ctx: PluginContext, _inputs: Any) -> dict[str, Any]:
    source = ctx.source_probe.size_bytes if ctx.source_probe is not None else 0
    return {
        "type": "actions",
        "actions": [
            {
                "kind": "extract_subtitles",
                "formats": [f"{ctx.probe.container}-{ctx.probe.size_bytes}"],
                "languages": [f"{source}-{ctx.facets.get('min_size.eligible', 'none')}"],
            }
        ],
    }
