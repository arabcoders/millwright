from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from common.ctx import PluginContext
from common.plan import RenderResult, dst_for, render

from server.flows.definition import FlowDefinition, PluginNode, TerminalNode, reachable, successors
from server.flows.engine import FlowContinuation, FlowResult, StepOutcome, evaluate
from server.flows.sidecars import sidecar_metadata, sidecar_subtitles
from server.plugins.host import PluginHost
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from common.media.probe import ProbeData
    from common.metadata import SidecarMetadata


@dataclass(kw_only=True, slots=True)
class DryRunResult:
    flow: FlowResult
    render: RenderResult | None


def deferred_nodes(continuation: FlowContinuation) -> list[str]:
    # Reachable post nodes need the real output probe. Terminals evaluate nothing.
    succ = successors(continuation.flow.edges)
    ids = {item.id for item in continuation.flow.nodes}
    post = reachable(continuation.node, succ) & ids
    return [item.id for item in continuation.flow.nodes if item.id in post and not isinstance(item, TerminalNode)]


async def dry_run(
    flow: FlowDefinition,
    probe: ProbeData,
    host: PluginHost,
    src: str,
    library_name: str = "",
    *,
    sidecars: list[str] | None = None,
    metadata: SidecarMetadata | None = None,
) -> DryRunResult:
    ctx = PluginContext(
        probe=probe,
        original_filename=Path(src).name,
        library={"name": library_name} if library_name else {},
        sidecar_subtitles=sidecar_subtitles(src) if sidecars is None else sidecars,
        sidecar_metadata=await wait_workspace_io(lambda: sidecar_metadata(src, flow)) if metadata is None else metadata,
    )
    result = await evaluate(flow, ctx, host)
    pending: list[str] = []
    if result.plan is not None and result.plan.video is not None and result.plan.video.encoder_priority:
        result.steps.append(
            StepOutcome(
                node=result.steps[-1].node, decision="deferred", reason="awaits compatible node encoder selection"
            )
        )
        pending.append("Encoder selection needs a compatible node.")
    if result.continuation is not None:
        by_id = {item.id: item for item in result.continuation.flow.nodes}
        post = deferred_nodes(result.continuation)
        for node_id in post:
            item = by_id[node_id]
            result.steps.append(
                StepOutcome(
                    node=node_id,
                    plugin=item.plugin if isinstance(item, PluginNode) else None,
                    decision="deferred",
                    reason="awaits the verified output probe",
                )
            )
        if post:
            pending.append("Later decisions need the verified output.")
    if result.status == "matched":
        if pending:
            result.status = "deferred"
            result.reason = " ".join(pending)
        else:
            selected = next(step for step in result.steps if step.decision == "plan")
            result.reason = selected.reason or "The selected plugin did not provide an explanation."
    rendered = (
        render(
            result.plan,
            probe,
            src,
            dst_for(src, result.plan),
            subtitle_path=str(Path(src).parent / result.plan.burn_subtitle.filename)
            if result.plan.burn_subtitle is not None
            else None,
        )
        if result.plan is not None and (result.plan.video is None or not result.plan.video.encoder_priority)
        else None
    )
    return DryRunResult(flow=result, render=rendered)
