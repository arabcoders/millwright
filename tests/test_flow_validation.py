from __future__ import annotations

from typing import Any, Literal

from server.flows.definition import ConditionNode, FlowDefinition, FlowEdge, PluginNode, TerminalNode
from server.flows.validation import validate_flow
from server.plugins.manifest import PluginManifest


def manifest(
    plugin_id: str,
    kind: Literal["filter", "transcode", "action", "inspector"] = "filter",
    require_plugins: list[str] | None = None,
    facets: dict[str, str] | None = None,
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        kind=kind,
        name=plugin_id,
        description="Test plugin manifest",
        author="Test author",
        url="https://example.com/plugin",
        version="1.0.0",
        require_plugins=require_plugins or [],
        facets=facets or {},
    )


def pnode(node_id: str, plugin: str) -> PluginNode:
    return PluginNode(id=node_id, plugin=plugin)


def cnode(node_id: str, when: dict[str, Any]) -> ConditionNode:
    return ConditionNode(id=node_id, when=when)


def tnode(node_id: str) -> TerminalNode:
    return TerminalNode(id=node_id, outcome="done")


def graph(
    entry: str,
    nodes: list,
    edges: list[tuple[str, str, str]],
) -> FlowDefinition:
    return FlowDefinition(
        entry=entry,
        nodes=nodes,
        edges=[FlowEdge(src=src, port=port, dst=dst) for src, port, dst in edges],
    )


def test_disabled_plugin_error() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("filter.gate"),
            manifest("transcode.video", "transcode"),
        ]
    }
    flow = graph(
        "gate",
        [pnode("gate", "filter.gate"), pnode("enc", "transcode.video"), tnode("done")],
        [("gate", "match", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 0
    assert len(validate_flow(manifests, flow, disabled={"filter.gate"})) == 1


def test_unknown_plugin_flagged() -> None:
    flow = graph("bad", [pnode("bad", "filter.nope")], [])
    assert len(validate_flow({}, flow)) == 2


def test_require_plugins_ordering() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("filter.other"),
            manifest("transcode.video", "transcode", ["filter.other"]),
        ]
    }
    ordered = graph(
        "inspect",
        [pnode("inspect", "filter.other"), pnode("enc", "transcode.video"), tnode("done")],
        [("inspect", "match", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, ordered)) == 0
    reversed_flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("inspect", "filter.other"), tnode("done")],
        [("enc", "skip", "inspect"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, reversed_flow)) == 1


def test_require_plugins_dominance() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("filter.other"),
            manifest("transcode.video", "transcode", ["filter.other"]),
        ]
    }
    # The inspect filter runs on one branch only, so it cannot guarantee its
    # facets for the join node even though it precedes it on one path.
    flow = graph(
        "gate",
        [
            cnode("gate", {"media.container": "mp4"}),
            pnode("inspect", "filter.other"),
            pnode("enc", "transcode.video"),
            tnode("done"),
        ],
        [
            ("gate", "true", "inspect"),
            ("gate", "false", "enc"),
            ("inspect", "match", "enc"),
            ("enc", "plan", "done"),
        ],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_condition_requires_facet() -> None:
    manifests = {
        "inspector.tags": manifest("inspector.tags", "inspector", facets={"language": "str"}),
        "transcode.video": manifest("transcode.video", "transcode"),
    }
    flow = graph(
        "inspect",
        [
            pnode("inspect", "inspector.tags"),
            cnode("check", {"tags.language": "en"}),
            pnode("enc", "transcode.video"),
            tnode("done"),
        ],
        [
            ("inspect", "match", "check"),
            ("inspect", "skip", "enc"),
            ("check", "true", "enc"),
            ("check", "false", "enc"),
            ("enc", "plan", "done"),
        ],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_condition_missing_facet_path() -> None:
    manifests = {
        "inspector.tags": manifest("inspector.tags", "inspector", facets={"language": "str"}),
        "transcode.video": manifest("transcode.video", "transcode"),
    }
    flow = graph(
        "gate",
        [
            cnode("gate", {"media.container": "mp4"}),
            pnode("inspect", "inspector.tags"),
            cnode("check", {"all": [{"any": [{"tags.language": "en"}]}]}),
            pnode("enc", "transcode.video"),
            tnode("done"),
        ],
        [
            ("gate", "true", "inspect"),
            ("gate", "false", "check"),
            ("inspect", "match", "check"),
            ("inspect", "skip", "check"),
            ("check", "true", "enc"),
            ("check", "false", "enc"),
            ("enc", "plan", "done"),
        ],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_condition_absent_producer() -> None:
    manifests = {
        "inspector.tags": manifest("inspector.tags", "inspector", facets={"language": "str"}),
        "transcode.video": manifest("transcode.video", "transcode"),
    }
    flow = graph(
        "check",
        [cnode("check", {"tags.language": "en"}), pnode("enc", "transcode.video"), tnode("done")],
        [("check", "true", "enc"), ("check", "false", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_condition_branch_producers() -> None:
    manifests = {
        "inspector.tags": manifest("inspector.tags", "inspector", facets={"language": "str"}),
        "transcode.video": manifest("transcode.video", "transcode"),
    }
    flow = graph(
        "gate",
        [
            cnode("gate", {"media.container": "mp4"}),
            pnode("left", "inspector.tags"),
            pnode("right", "inspector.tags"),
            cnode("check", {"tags.language": "en"}),
            pnode("enc", "transcode.video"),
            tnode("done"),
        ],
        [
            ("gate", "true", "left"),
            ("gate", "false", "right"),
            ("left", "match", "check"),
            ("right", "match", "check"),
            ("check", "true", "enc"),
            ("check", "false", "enc"),
            ("enc", "plan", "done"),
        ],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_zero_transcodes_flagged() -> None:
    manifests = {m.id: m for m in [manifest("filter.gate"), manifest("filter.other")]}
    flow = graph(
        "size",
        [pnode("size", "filter.gate"), pnode("foreign", "filter.other")],
        [("size", "match", "foreign")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_multiple_transcodes_accepted() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("transcode.video", "transcode"),
            manifest("transcode.video", "transcode"),
        ]
    }
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("remux", "transcode.video"), tnode("done")],
        [("enc", "skip", "remux"), ("enc", "plan", "done"), ("remux", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_stages_accepted() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode"), manifest("transcode.video", "transcode")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("alt", "transcode.video"), tnode("done")],
        [("enc", "plan", "alt"), ("alt", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_post_transcode_skip() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("transcode.video", "transcode"),
            manifest("inspector.other", "inspector"),
            manifest("transcode.video", "transcode"),
        ]
    }
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("subs", "inspector.other"), pnode("remux", "transcode.video")],
        [("enc", "plan", "subs"), ("subs", "skip", "remux")],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_cycle_flagged() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode"), manifest("filter.gate")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("size", "filter.gate"), tnode("done")],
        [("enc", "skip", "size"), ("size", "match", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_condition_ports_required() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode")]}
    flow = graph(
        "gate",
        [cnode("gate", {"media.container": "mp4"}), pnode("enc", "transcode.video"), tnode("done")],
        [("gate", "true", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_duplicate_edge_flagged() -> None:
    manifests = {
        m.id: m
        for m in [
            manifest("transcode.video", "transcode"),
            manifest("filter.gate"),
            manifest("filter.other"),
        ]
    }
    flow = graph(
        "enc",
        [
            pnode("enc", "transcode.video"),
            pnode("size", "filter.gate"),
            pnode("foreign", "filter.other"),
            tnode("done"),
        ],
        [("enc", "skip", "size"), ("enc", "skip", "foreign"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 2


def test_unknown_edge() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done")],
        [("enc", "skip", "ghost"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_bad_port_flagged() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode"), manifest("filter.gate")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("size", "filter.gate"), tnode("done")],
        [("enc", "sideways", "size"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_plan_edge() -> None:
    manifests = {m.id: m for m in [manifest("filter.gate"), manifest("transcode.video", "transcode")]}
    flow = graph(
        "size",
        [pnode("size", "filter.gate"), pnode("enc", "transcode.video"), tnode("done")],
        [("size", "plan", "enc"), ("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_terminal_action_stage() -> None:
    manifests = {
        item.id: item for item in [manifest("transcode.video", "transcode"), manifest("action.other", "action")]
    }
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done"), pnode("act", "action.other")],
        [("enc", "plan", "done"), ("done", "next", "act")],
    )
    assert len(validate_flow(manifests, flow)) == 0


def test_terminal_bad_stage() -> None:
    manifests = {item.id: item for item in [manifest("transcode.video", "transcode"), manifest("filter.other")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done"), pnode("bad", "filter.other")],
        [("enc", "plan", "done"), ("done", "next", "bad")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_terminal_bad_port() -> None:
    manifests = {"transcode.video": manifest("transcode.video", "transcode")}
    flow = graph(
        "enc", [pnode("enc", "transcode.video"), tnode("done")], [("enc", "plan", "done"), ("done", "bad", "enc")]
    )
    assert len(validate_flow(manifests, flow)) == 2


def test_terminal_preplan_stage() -> None:
    manifests = {
        item.id: item for item in [manifest("transcode.video", "transcode"), manifest("action.other", "action")]
    }
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), tnode("done"), pnode("act", "action.other")],
        [("enc", "plan", "done"), ("enc", "skip", "done"), ("done", "next", "act")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_notify_must_precede_plan() -> None:
    manifests = {
        item.id: item for item in [manifest("transcode.video", "transcode"), manifest("action.notify", "action")]
    }
    preplan = graph(
        "notify",
        [pnode("notify", "action.notify"), pnode("enc", "transcode.video"), tnode("done")],
        [("notify", "next", "enc"), ("enc", "plan", "done")],
    )
    postplan = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("notify", "action.notify")],
        [("enc", "plan", "notify"), ("enc", "skip", "notify")],
    )
    assert len(validate_flow(manifests, preplan)) == 0
    assert len(validate_flow(manifests, postplan)) == 1


def test_unreachable_nodes_flagged() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode"), manifest("filter.gate")]}
    flow = graph(
        "enc",
        [pnode("enc", "transcode.video"), pnode("size", "filter.gate"), tnode("done")],
        [("enc", "plan", "done")],
    )
    assert len(validate_flow(manifests, flow)) == 1


def test_duplicate_nodes() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode")]}
    flow = graph("enc", [pnode("enc", "transcode.video"), pnode("enc", "transcode.video")], [])
    assert len(validate_flow(manifests, flow)) == 1


def test_missing_entry() -> None:
    manifests = {m.id: m for m in [manifest("transcode.video", "transcode")]}
    flow = graph("ghost", [pnode("enc", "transcode.video"), tnode("done")], [("enc", "plan", "done")])
    assert len(validate_flow(manifests, flow)) == 3
