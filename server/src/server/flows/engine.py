from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from common.actions import Action, EmbedMetadata
from common.diagnostics import error_message
from common.facts import builtin_facts
from common.metadata import SidecarMetadata
from common.plan import FFmpegPlan
from common.plan.metadata_updates import apply_metadata
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from server.flows.definition import (
    ConditionNode,
    FlowDefinition,
    PluginNode,
    TerminalNode,
    successors,
    when_satisfied,
)
from server.plugins.host import EvaluatedDecision, PluginHost
from server.plugins.manifest import PluginManifest
from server.plugins.migrations import migrate_definition

if TYPE_CHECKING:
    from common.ctx import FacetValue, PluginContext

LOG = logging.getLogger(__name__)

# Invalid decisions stop traversal, except best-effort notification nodes.
_KIND_DECISIONS = {
    "filter": frozenset({"match", "veto"}),
    "inspector": frozenset({"match", "veto", "skip"}),
    "transcode": frozenset({"plan", "skip"}),
    "action": frozenset({"actions"}),
}

_ACTIONS_ADAPTER = TypeAdapter(list[Action])


class FlowContinuation(BaseModel):
    # Keep the definition and plan edge target pinned for the post pass.
    flow: FlowDefinition
    node: str
    sidecar_metadata: SidecarMetadata = Field(default_factory=SidecarMetadata)


@dataclass(kw_only=True, slots=True)
class StepOutcome:
    node: str
    decision: str
    plugin: str | None = None
    reason: str = ""
    error: str | None = None
    facets: dict[str, FacetValue] = field(default_factory=dict)


@dataclass(kw_only=True, slots=True)
class FlowResult:
    status: str
    reason: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    facets: dict[str, FacetValue] = field(default_factory=dict)
    plan: FFmpegPlan | None = None
    actions: list[Action] = field(default_factory=list)
    # The post pass resumes at the selected plan edge with the output probe's facts.
    continuation: FlowContinuation | None = None
    # Union of the executed nodes' required worker tags. Enforced any-of at dispatch.
    requires: list[str] = field(default_factory=list)
    # Union of the executed nodes' required encoders. Enforced as a subset at dispatch.
    requires_encoders: list[str] = field(default_factory=list)
    # Flow actions run alongside plugin actions in the final post pass.
    post_actions: list[Action] = field(default_factory=list)


def decision_entry(outcome: StepOutcome) -> dict:
    return {
        "node": outcome.node,
        **({"step": outcome.plugin} if outcome.plugin is not None else {}),
        "outcome": outcome.decision,
        **({"reason": outcome.reason} if outcome.reason else {}),
        **({"error": outcome.error} if outcome.error else {}),
        **({"facets": outcome.facets} if outcome.facets else {}),
    }


@dataclass(kw_only=True, slots=True)
class _Walk:
    facets: dict[str, FacetValue]
    outcomes: list[StepOutcome] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    requires: set[str] = field(default_factory=set)
    requires_encoders: set[str] = field(default_factory=set)
    plan: FFmpegPlan | None = None
    continuation: FlowContinuation | None = None
    # First veto wins and stays final: routed veto branches may keep running,
    # but the flow can no longer end as matched.
    veto_reason: str | None = None
    terminal: str | None = None
    errored: bool = False


async def evaluate(flow: FlowDefinition, ctx: PluginContext, host: PluginHost) -> FlowResult:
    try:
        flow = FlowDefinition.model_validate(migrate_definition(flow, (info.manifest for info in host.plugins())))
    except ValueError as exc:
        return FlowResult(status="error", reason=str(exc))
    # Built-in facts own media.*. Plugin facet validation prevents reserved-key collisions.
    facets = {**builtin_facts(ctx.probe), **dict(ctx.facets)}
    if flow.when is not None and not when_satisfied(facets, flow.when):
        return FlowResult(status="skipped", reason="flow condition not met", facets=facets)
    walk = await _walk(flow, flow.entry, ctx, facets, host)
    if walk.veto_reason is not None or walk.terminal == "veto":
        status = "vetoed"
    elif walk.plan is not None:
        status = "matched"
    elif walk.errored:
        status = "error"
    else:
        status = "skipped"
    return FlowResult(
        status=status,
        reason=walk.veto_reason or "",
        steps=walk.outcomes,
        facets=facets,
        plan=walk.plan,
        actions=walk.actions,
        continuation=walk.continuation,
        requires=sorted(walk.requires),
        requires_encoders=sorted(walk.requires_encoders),
        post_actions=list(flow.actions),
    )


async def evaluate_post(continuation: FlowContinuation, ctx: PluginContext, host: PluginHost) -> FlowResult:
    try:
        flow = FlowDefinition.model_validate(
            migrate_definition(continuation.flow, (info.manifest for info in host.plugins()))
        )
    except ValueError as exc:
        return FlowResult(status="error", reason=str(exc))
    ctx = ctx.model_copy(update={"sidecar_metadata": continuation.sidecar_metadata, "metadata_requests": []})
    # Missing output metadata must not inherit the source's reserved facts.
    facets = {key: value for key, value in ctx.facets.items() if not key.startswith("media.")}
    facets.update(builtin_facts(ctx.probe))
    walk = await _walk(flow, continuation.node, ctx, facets, host)
    if walk.errored and walk.terminal not in {"skip", "veto"}:
        status = "error"
    elif walk.veto_reason is not None or walk.terminal == "veto":
        status = "vetoed"
    elif walk.terminal == "skip":
        status = "skipped"
    elif walk.plan is not None or walk.actions:
        status = "matched"
    else:
        status = "skipped"
    return FlowResult(
        status=status,
        reason=walk.veto_reason or "",
        steps=walk.outcomes,
        facets=facets,
        actions=walk.actions,
        plan=walk.plan,
        continuation=walk.continuation,
        requires=sorted(walk.requires),
        requires_encoders=sorted(walk.requires_encoders),
    )


async def _walk(
    flow: FlowDefinition,
    start: str,
    ctx: PluginContext,
    facets: dict[str, FacetValue],
    host: PluginHost,
) -> _Walk:
    nodes = {item.id: item for item in flow.nodes}
    succ = successors(flow.edges)
    walk = _Walk(facets=facets)
    ctx = ctx.model_copy(update={"metadata_requests": list(ctx.metadata_requests)})

    def hop(target: str | None) -> str | None:
        return target if target is not None and target in nodes else None

    node = hop(start)
    while node is not None:
        item = nodes[node]
        ports = succ.get(node, {})
        if isinstance(item, TerminalNode):
            walk.terminal = item.outcome
            walk.outcomes.append(StepOutcome(node=node, decision=item.outcome))
            node = hop(ports.get("next"))
            continue
        if isinstance(item, ConditionNode):
            taken = "true" if when_satisfied(facets, item.when) else "false"
            walk.outcomes.append(StepOutcome(node=node, decision=taken))
            node = hop(ports.get(taken))
            continue

        manifest = host.describe(item.plugin)
        if manifest is None:
            outcome = StepOutcome(
                node=node, plugin=item.plugin, decision="error", error=f"unknown plugin: {item.plugin}"
            )
            plan = None
        else:
            # Requirements follow the executed path: unvisited nodes never gate dispatch.
            walk.requires.update(manifest.requires)
            walk.requires_encoders.update(manifest.requires_encoders)
            host_ctx = ctx.model_dump(mode="json")
            host_ctx["facets"] = facets
            decision = await host.evaluate(item.plugin, host_ctx, item.inputs)
            outcome, plan = _apply_decision(item, manifest, decision, facets, walk.actions)
            metadata = [action for action in walk.actions if isinstance(action, EmbedMetadata)]
            ctx.metadata_requests.extend(metadata)
            walk.actions = [action for action in walk.actions if not isinstance(action, EmbedMetadata)]
            if plan is not None:
                try:
                    plan = apply_metadata(plan, ctx)
                except ValueError as exc:
                    outcome = StepOutcome(node=node, plugin=item.plugin, decision="error", error=str(exc))
                    plan = None
        walk.outcomes.append(outcome)
        if outcome.decision == "error":
            if item.plugin == "action.notify":
                outcome.reason = "Notification failed."
                LOG.warning("Notification node %s failed. Continuing flow", node, extra={"error": outcome.error})
                node = hop(ports.get("next"))
                continue
            walk.errored = True
            break
        if outcome.decision == "veto" and walk.veto_reason is None:
            walk.veto_reason = outcome.reason
        if plan is not None:
            walk.plan = plan
            walk.continuation = (
                FlowContinuation(node=target, flow=flow, sidecar_metadata=ctx.sidecar_metadata)
                if (target := hop(ports.get("plan")))
                else None
            )
            break
        node = hop(ports.get("next" if outcome.decision == "actions" else outcome.decision))
    return walk


def _apply_decision(
    item: PluginNode,
    manifest: PluginManifest,
    decision: EvaluatedDecision,
    facets: dict[str, FacetValue],
    actions: list[Action],
) -> tuple[StepOutcome, FFmpegPlan | None]:
    node, plugin = item.id, item.plugin
    if decision.type == "error":
        return StepOutcome(node=node, plugin=plugin, decision="error", error=decision.error or "plugin error"), None
    if decision.type not in _KIND_DECISIONS[manifest.kind]:
        detail = f"{plugin} ({manifest.kind}) returned unexpected decision {decision.type!r}"
        return StepOutcome(node=node, plugin=plugin, decision="error", error=detail), None

    if decision.type == "match":
        merged = {f"{manifest.namespace}.{key}": value for key, value in (decision.facets or {}).items()}
        facets.update(merged)
        return StepOutcome(node=node, plugin=plugin, decision="match", facets=merged), None
    if decision.type == "veto":
        return StepOutcome(node=node, plugin=plugin, decision="veto", reason=decision.reason or ""), None
    if decision.type == "skip":
        return StepOutcome(node=node, plugin=plugin, decision="skip", reason=decision.reason or ""), None
    if decision.type == "plan":
        try:
            plan = FFmpegPlan.model_validate(decision.plan)
        except ValidationError as exc:
            return StepOutcome(node=node, plugin=plugin, decision="error", error=f"invalid plan: {exc}"), None
        return StepOutcome(node=node, plugin=plugin, decision="plan", reason=decision.reason or ""), plan
    try:
        produced = _ACTIONS_ADAPTER.validate_python(decision.actions or [])
    except ValidationError as exc:
        return StepOutcome(
            node=node, plugin=plugin, decision="error", error=f"invalid actions: {error_message(exc)}"
        ), None
    for action in produced:
        action.plugin_id = manifest.id
        action.config_version = manifest.config_version
    actions.extend(produced)
    return StepOutcome(node=node, plugin=plugin, decision="actions"), None
