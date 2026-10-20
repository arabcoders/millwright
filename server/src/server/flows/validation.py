from __future__ import annotations

from typing import TYPE_CHECKING

from common.actions import EmbedMetadata, Notify

from server.flows.definition import (
    CONDITION_PORTS,
    KIND_PORTS,
    ConditionNode,
    FlowDefinition,
    PluginNode,
    TerminalNode,
    reachable,
    successors,
)
from server.plugins.manifest import PluginManifest
from server.plugins.migrations import migrate_definition

if TYPE_CHECKING:
    from collections.abc import Collection

    from server.plugins.host import PluginHost


def validate_flow(
    manifests: dict[str, PluginManifest], flow: FlowDefinition, disabled: Collection[str] = ()
) -> list[str]:
    ids = [item.id for item in flow.nodes]
    nodes = dict(zip(ids, flow.nodes, strict=True))
    succ = successors(flow.edges)
    errors: list[str] = []

    errors.extend(
        f"duplicate node id: {node_id}" for node_id in sorted({node_id for node_id in ids if ids.count(node_id) > 1})
    )
    if flow.entry not in nodes:
        errors.append(f"entry node not in graph: {flow.entry}")

    seen_ports: set[tuple[str, str]] = set()
    for edge in flow.edges:
        if edge.src not in nodes or edge.dst not in nodes:
            errors.append(f"edge {edge.src} -{edge.port}-> {edge.dst}: unknown node")
        if (edge.src, edge.port) in seen_ports:
            errors.append(f"node {edge.src}: duplicate edge on port {edge.port}")
        seen_ports.add((edge.src, edge.port))
        source = nodes.get(edge.src)
        if isinstance(source, ConditionNode):
            if edge.port not in CONDITION_PORTS:
                errors.append(f"condition {edge.src}: unknown port {edge.port}")
        elif isinstance(source, TerminalNode):
            if edge.port != "next":
                errors.append(f"terminal {edge.src}: unknown port {edge.port}")
        elif isinstance(source, PluginNode):
            manifest = manifests.get(source.plugin)
            if manifest is not None and edge.port not in KIND_PORTS[manifest.kind]:
                detail = f"node {edge.src}: port {edge.port} is not valid for {manifest.kind} plugin {source.plugin}"
                errors.append(detail)

    plugin_manifests: dict[str, PluginManifest] = {}
    for item in flow.nodes:
        if isinstance(item, ConditionNode):
            errors.extend(
                f"condition {item.id}: missing {port} edge"
                for port in sorted(CONDITION_PORTS)
                if port not in succ.get(item.id, {})
            )
        elif isinstance(item, PluginNode):
            if item.plugin in disabled:
                errors.append(f"plugin disabled: {item.plugin}")
                continue
            manifest = manifests.get(item.plugin)
            if manifest is None:
                errors.append(f"unknown plugin: {item.plugin}")
            else:
                plugin_manifests[item.id] = manifest

    live = reachable(flow.entry, succ) & nodes.keys() if flow.entry in nodes else set()
    orphans = set(nodes) - live
    if orphans:
        errors.append(f"nodes unreachable from entry: {', '.join(sorted(orphans))}")

    transcodes = {item_id for item_id, manifest in plugin_manifests.items() if manifest.kind == "transcode"}
    if not transcodes & live:
        errors.append("flow has no transcode step")

    veto_messages = {
        f"veto branch reaches transcode node: {bad}"
        for edge in flow.edges
        if edge.port == "veto" and edge.dst in nodes
        for bad in reachable(edge.dst, succ) & nodes.keys() & transcodes
    }
    errors.extend(sorted(veto_messages))

    for terminal in (item for item in flow.nodes if isinstance(item, TerminalNode)):
        target = succ.get(terminal.id, {}).get("next")
        if target is None:
            continue
        for node_id in sorted(reachable(target, succ) & nodes.keys()):
            node = nodes[node_id]
            if not isinstance(node, PluginNode) or (
                node_id in plugin_manifests and plugin_manifests[node_id].kind != "action"
            ):
                errors.append(f"terminal {terminal.id} next stage may contain only action plugins: {node_id}")

    pre_succ = {
        src: {port: dst for port, dst in ports.items() if not (port == "plan" and src in transcodes)}
        for src, ports in succ.items()
    }
    pre_live = reachable(flow.entry, pre_succ) & nodes.keys() if flow.entry in nodes else set()
    errors.extend(
        f"terminal {terminal.id} actions require a successful transcode"
        for terminal in flow.nodes
        if isinstance(terminal, TerminalNode) and terminal.id in pre_live and "next" in succ.get(terminal.id, {})
    )

    notify_nodes = {item.id for item in flow.nodes if isinstance(item, PluginNode) and item.plugin == "action.notify"}
    for transcode in transcodes & live:
        target = succ.get(transcode, {}).get("plan")
        if target is not None and target in nodes:
            errors.extend(
                f"notify node must be before transcode plan edge: {node_id}"
                for node_id in sorted(reachable(target, succ) & nodes.keys() & notify_nodes)
            )
    if any(isinstance(action, Notify) for action in flow.actions):
        errors.append("Flow actions cannot contain notify. Use an action.notify node before the transcode.")
    if any(isinstance(action, EmbedMetadata) for action in flow.actions):
        errors.append(
            "Flow actions cannot contain embed_metadata. Use an action.embed_metadata node before processing."
        )

    order = _topo_order(succ, live) if flow.entry in nodes and live else None
    if order is None:
        if flow.entry in nodes:
            errors.append("flow graph has a cycle")
    else:
        preds: dict[str, set[str]] = {}
        for edge in flow.edges:
            if edge.src in live and edge.dst in live:
                preds.setdefault(edge.dst, set()).add(edge.src)
        dom = _dominators(order, flow.entry, preds)
        for item in flow.nodes:
            if not isinstance(item, PluginNode) or item.id not in dom or item.id not in plugin_manifests:
                continue
            manifest = plugin_manifests[item.id]
            errors.extend(
                f"{item.plugin} requires {required} in an earlier node"
                for required in manifest.require_plugins
                if not any(
                    other != item.id and other in dom[item.id]
                    for other, om in plugin_manifests.items()
                    if om.id == required
                )
            )
        guaranteed = _guaranteed_facets(order, flow.entry, succ, nodes, plugin_manifests)
        declared = {f"{manifest.namespace}.{facet}" for manifest in manifests.values() for facet in manifest.facets}
        for item in flow.nodes:
            if not isinstance(item, ConditionNode) or item.id not in guaranteed:
                continue
            errors.extend(
                f"condition {item.id}: facet {facet} has no producer on every path"
                for facet in sorted(_condition_facets(item.when) & declared - guaranteed[item.id])
            )

    return errors


def _condition_facets(when: dict[str, object]) -> set[str]:
    facets: set[str] = set()
    for key, value in when.items():
        if key in {"any", "all"}:
            if isinstance(value, list):
                for nested in value:
                    if isinstance(nested, dict):
                        facets.update(_condition_facets(nested))
        else:
            facets.add(key)
    return facets


def _guaranteed_facets(
    order: list[str],
    entry: str,
    succ: dict[str, dict[str, str]],
    nodes: dict[str, PluginNode | ConditionNode | TerminalNode],
    manifests: dict[str, PluginManifest],
) -> dict[str, set[str]]:
    incoming: dict[str, list[set[str]]] = {entry: [set()]}
    guaranteed: dict[str, set[str]] = {}
    for node_id in order:
        paths = incoming.get(node_id, [])
        if not paths:
            continue
        available = set.intersection(*paths)
        guaranteed[node_id] = available
        node = nodes[node_id]
        manifest = manifests.get(node_id) if isinstance(node, PluginNode) else None
        for port, target in succ.get(node_id, {}).items():
            next_facets = available | (
                {f"{manifest.namespace}.{facet}" for facet in manifest.facets}
                if port == "match" and manifest is not None
                else set()
            )
            incoming.setdefault(target, []).append(next_facets)
    return guaranteed


def _topo_order(succ: dict[str, dict[str, str]], live: set[str]) -> list[str] | None:
    indegree = dict.fromkeys(live, 0)
    for src in live:
        for dst in succ.get(src, {}).values():
            if dst in indegree:
                indegree[dst] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for dst in succ.get(node, {}).values():
            if dst in indegree:
                indegree[dst] -= 1
                if indegree[dst] == 0:
                    queue.append(dst)
        queue.sort()
    return order if len(order) == len(live) else None


def _dominators(order: list[str], entry: str, preds: dict[str, set[str]]) -> dict[str, set[str]]:
    dom: dict[str, set[str]] = {entry: {entry}}
    for node in order:
        if node == entry:
            continue
        ready = [dom[pred] for pred in preds.get(node, ()) if pred in dom]
        dom[node] = {node} | (set.intersection(*ready) if ready else set())
    return dom


async def validate_definition(host: PluginHost, definition: FlowDefinition) -> list[str]:
    infos = host.plugins()
    manifests = {info.manifest.id: info.manifest for info in infos}
    disabled = {info.manifest.id for info in infos if info.state == "disabled"}
    try:
        migrated = FlowDefinition.model_validate(migrate_definition(definition, manifests.values()))
    except ValueError as exc:
        return [str(exc)]
    errors = validate_flow(manifests, migrated, disabled)
    for item in migrated.nodes:
        if isinstance(item, PluginNode):
            problems = await host.validate_inputs(item.plugin, item.inputs)
            errors.extend(f"{item.plugin}: {problem}" for problem in problems)
    if not errors:
        # Failed validation must not leave a caller with a partially migrated draft.
        definition.nodes = migrated.nodes
        definition.actions = migrated.actions
    return errors
