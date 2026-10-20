from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.flows.definition import ConditionNode, FlowDefinition, PluginNode, when_satisfied

FACETS = {
    "filter.gate.eligible": True,
    "media.size_bytes": 4_000_000_000,
    "media.container": "mkv",
    "media.audio.descriptive_stream_indices": [2],
    "filter.other.unexpected_languages": ["jpn"],
}


def satisfied(when: dict[str, Any]) -> bool:
    return when_satisfied(FACETS, when)


def test_membership_operator_accepted() -> None:
    ConditionNode(id="c", when={"filter.other.unexpected_languages": {"contains": "jpn"}})
    assert satisfied({"filter.other.unexpected_languages": {"not_contains": "eng"}})
    assert satisfied({"media.audio.descriptive_stream_indices": {"contains": 2}})


def test_empty_combinator_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"any": []})


def test_non_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"media.size_bytes": None})


def test_multi_operator_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"media.size_bytes": {"gt": 1, "lt": 5}})


def test_ordering_scalar() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"filter.other.unexpected_languages": {"gt": ["jpn"]}})


def test_ne_literal() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"media.container": {"ne": {"x": 1}}})


def test_membership_operand_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"filter.other.unexpected_languages": {"contains": ["jpn"]}})


def test_nested_invalid() -> None:
    with pytest.raises(ValidationError):
        ConditionNode(id="c", when={"any": [{"media.codec": {"foo": 1}}]})


def test_operators_compare_numerically() -> None:
    assert satisfied({"media.size_bytes": {"gt": 3_999_999_999}})
    assert satisfied({"media.size_bytes": {"gte": 4_000_000_000}})
    assert not satisfied({"media.size_bytes": {"lt": 4_000_000_000}})
    assert satisfied({"media.size_bytes": {"lte": 4_000_000_000}})


def test_operators_compare_strings() -> None:
    assert satisfied({"media.container": {"gt": "avi"}})
    assert not satisfied({"media.container": {"lt": "avi"}})
    assert satisfied({"media.container": {"ne": "mp4"}})
    assert not satisfied({"media.container": {"ne": "mkv"}})


def test_missing_facet() -> None:
    assert not satisfied({"media.absent": {"ne": 1}})
    assert not satisfied({"media.absent": "mkv"})


def test_type_mismatch() -> None:
    assert not satisfied({"media.container": {"gte": 5}})
    assert not satisfied({"filter.gate.eligible": {"gt": 0}})


def test_any_branch() -> None:
    assert satisfied({"any": [{"media.container": "mp4"}, {"media.container": "mkv"}]})
    assert not satisfied({"any": [{"media.container": "mp4"}, {"media.container": "mov"}]})


def test_all_branches() -> None:
    assert satisfied({"all": [{"media.container": "mkv"}, {"media.size_bytes": {"gte": 1}}]})
    assert not satisfied({"all": [{"media.container": "mkv"}, {"media.size_bytes": {"gte": 4_000_000_001}}]})


def test_combinator_keys() -> None:
    when = {
        "any": [{"media.container": "mp4"}, {"filter.other.unexpected_languages": {"ne": []}}],
        "filter.gate.eligible": True,
    }
    assert satisfied(when)
    assert not satisfied({**when, "filter.gate.eligible": False})


def test_nested_combinators() -> None:
    when = {
        "all": [
            {"any": [{"media.container": "mp4"}, {"media.container": "mov"}]},
            {"media.size_bytes": {"gt": 1}},
        ]
    }
    assert not satisfied(when)


def test_flow_when_rejects() -> None:
    with pytest.raises(ValidationError):
        FlowDefinition(
            entry="enc",
            nodes=[PluginNode(id="enc", plugin="transcode.video")],
            when={"media.container": {"foo": "mkv"}},
        )
