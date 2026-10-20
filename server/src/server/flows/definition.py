from __future__ import annotations

import operator
from typing import TYPE_CHECKING, Annotated, Any, Literal

from common.actions import Action
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from common.ctx import FacetValue

# Pydantic resolves these aliases at class creation, so their types need runtime imports.
WhenFacets = dict[str, Any]
FlowActions = list[Action]

# Facets accept a literal for equality or a single-operator object. "any" and
# "all" combine nested conditions. Dotted facet namespaces prevent key collisions.
_WHEN_ORDER = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
_WHEN_MEMBERSHIP = {"contains", "not_contains"}
_WHEN_COMBINATORS = frozenset({"any", "all"})


def when_problem(when: dict[str, Any], path: str = "when_facets") -> str | None:
    for key, value in when.items():
        if key in _WHEN_COMBINATORS:
            if (problem := _combinator_problem(key, value, path)) is not None:
                return problem
        elif isinstance(value, dict):
            if (problem := _operator_problem(key, value, path)) is not None:
                return problem
        elif not _is_literal(value):
            return f"{path}.{key}: value must be a literal or an operator object"
    return None


def _combinator_problem(key: str, value: Any, path: str) -> str | None:
    if not isinstance(value, list) or not value:
        return f"{path}.{key}: must be a non-empty list of conditions"
    for index, sub in enumerate(value):
        if not isinstance(sub, dict):
            return f"{path}.{key}[{index}]: must be a condition object"
        if (problem := when_problem(sub, f"{path}.{key}[{index}]")) is not None:
            return problem
    return None


def _operator_problem(key: str, value: dict[str, Any], path: str) -> str | None:
    if len(value) != 1:
        return f"{path}.{key}: operator object must carry exactly one operator"
    op, operand = next(iter(value.items()))
    if op != "ne" and op not in _WHEN_ORDER and op not in _WHEN_MEMBERSHIP:
        return f"{path}.{key}: unknown operator {op!r}"
    if not _is_literal(operand):
        return f"{path}.{key}: operand must be a literal"
    if op in _WHEN_ORDER and (isinstance(operand, bool) or not isinstance(operand, (int, float, str))):
        return f"{path}.{key}: {op} needs a number or string operand"
    if op in _WHEN_MEMBERSHIP and (isinstance(operand, bool) or not isinstance(operand, (str, int))):
        return f"{path}.{key}: {op} needs a string or integer operand"
    return None


def _is_literal(value: Any) -> bool:
    return isinstance(value, (str, int, float)) or (
        isinstance(value, list)
        and (
            not value
            or all(isinstance(item, str) for item in value)
            or all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        )
    )


def when_satisfied(facets: Mapping[str, FacetValue], when: dict[str, Any]) -> bool:
    # Total on any dict: malformed shapes read as "not met" so evaluation can
    # never raise, even for definitions that skipped parse-time validation.
    for key, expected in when.items():
        if key in _WHEN_COMBINATORS:
            subs = [sub for sub in expected if isinstance(sub, dict)] if isinstance(expected, list) else []
            if key == "any" and not any(when_satisfied(facets, sub) for sub in subs):
                return False
            if key == "all" and not all(when_satisfied(facets, sub) for sub in subs):
                return False
        elif key not in facets or not _value_matches(facets[key], expected):
            return False
    return True


def _value_matches(actual: FacetValue, expected: Any) -> bool:
    if not isinstance(expected, dict):
        return actual == expected
    if len(expected) != 1:
        return False
    op, operand = next(iter(expected.items()))
    if op == "ne":
        return actual != operand
    if op in _WHEN_MEMBERSHIP:
        return (
            isinstance(actual, list)
            and isinstance(operand, (str, int))
            and not isinstance(operand, bool)
            and ((operand in actual) == (op == "contains"))
        )
    if op not in _WHEN_ORDER or not _comparable(actual, operand):
        return False
    return _WHEN_ORDER[op](actual, operand)


def _comparable(actual: FacetValue, operand: Any) -> bool:
    if isinstance(actual, bool) or isinstance(operand, bool):
        return False
    if isinstance(actual, (int, float)) and isinstance(operand, (int, float)):
        return True
    return isinstance(actual, str) and isinstance(operand, str)


# One successor per port keeps the walk deterministic.
CONDITION_PORTS = frozenset({"true", "false"})
KIND_PORTS: dict[str, frozenset[str]] = {
    "filter": frozenset({"match", "veto"}),
    "inspector": frozenset({"match", "skip", "veto"}),
    "transcode": frozenset({"plan", "skip"}),
    "action": frozenset({"next"}),
}


class PluginNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["plugin"] = "plugin"
    id: str = Field(min_length=1)
    plugin: str
    config_version: int = Field(default=1, ge=1, strict=True)
    inputs: dict[str, Any] = Field(default_factory=dict)


class ConditionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["condition"] = "condition"
    id: str = Field(min_length=1)
    when: WhenFacets

    @model_validator(mode="after")
    def _valid_when(self) -> ConditionNode:
        if (problem := when_problem(self.when, path=f"{self.id}.when")) is not None:
            raise ValueError(problem)
        return self


class TerminalNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["terminal"] = "terminal"
    id: str = Field(min_length=1)
    # "done" skips if no plan or post actions were selected.
    outcome: Literal["skip", "done", "veto"]


FlowNode = Annotated[PluginNode | ConditionNode | TerminalNode, Field(discriminator="type")]


class FlowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    port: str
    dst: str


def successors(edges: list[FlowEdge]) -> dict[str, dict[str, str]]:
    table: dict[str, dict[str, str]] = {}
    for edge in edges:
        table.setdefault(edge.src, {})[edge.port] = edge.dst
    return table


def reachable(start: str | None, succ: Mapping[str, Mapping[str, str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start] if start is not None else []
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(succ.get(node, {}).values())
    return seen


class FlowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    entry: str
    nodes: list[FlowNode]
    edges: list[FlowEdge] = Field(default_factory=list)
    actions: FlowActions = Field(default_factory=list)
    # Gates the whole flow: unsatisfied means no node runs and the job skips.
    when: WhenFacets | None = None
    # Editor layout passes through storage and export. The engine ignores it.
    ui: dict[str, Any] | None = Field(default=None, alias="_ui")

    @model_validator(mode="after")
    def _valid_when(self) -> FlowDefinition:
        if self.when is not None and (problem := when_problem(self.when, path="when")) is not None:
            raise ValueError(problem)
        return self
