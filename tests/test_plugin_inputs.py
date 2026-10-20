from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from common.plugin import TextField, build_inputs, input_fields


class Mode(StrEnum):
    FAST = "fast"
    SLOW = "slow"


class Everything(BaseModel):
    count: int = Field(default=3, ge=1, le=9)
    ratio: float = 0.5
    flag: bool = False
    note: TextField = "hello"
    key: str = Field(default="", json_schema_extra={"ui": "password"})
    mode: Mode = Mode.SLOW
    pick: Literal["a", "b"] = "a"
    tags: list[str] = ["x"]
    events: list[Literal["a", "b"]] = ["a"]
    label: str = "plain"
    needed: int
    optional_count: int | None = None
    optional_pick: Literal["x", "y"] | None = None


def test_derives_field_types() -> None:
    assert [
        {key: value for key, value in field.items() if key not in {"required", "nullable"}}
        for field in input_fields(Everything)
    ] == [
        {"name": "count", "type": "int", "default": 3, "min": 1, "max": 9, "values": None, "help": None},
        {"name": "ratio", "type": "float", "default": 0.5, "min": None, "max": None, "values": None, "help": None},
        {"name": "flag", "type": "bool", "default": False, "min": None, "max": None, "values": None, "help": None},
        {"name": "note", "type": "text", "default": "hello", "min": None, "max": None, "values": None, "help": None},
        {"name": "key", "type": "password", "default": "", "min": None, "max": None, "values": None, "help": None},
        {
            "name": "mode",
            "type": "enum",
            "default": "slow",
            "min": None,
            "max": None,
            "values": ["fast", "slow"],
            "help": None,
        },
        {"name": "pick", "type": "enum", "default": "a", "min": None, "max": None, "values": ["a", "b"], "help": None},
        {"name": "tags", "type": "strings", "default": ["x"], "min": None, "max": None, "values": None, "help": None},
        {
            "name": "events",
            "type": "strings",
            "default": ["a"],
            "min": None,
            "max": None,
            "values": ["a", "b"],
            "help": None,
        },
        {"name": "label", "type": "str", "default": "plain", "min": None, "max": None, "values": None, "help": None},
        {"name": "needed", "type": "int", "default": None, "min": None, "max": None, "values": None, "help": None},
        {
            "name": "optional_count",
            "type": "int",
            "default": None,
            "min": None,
            "max": None,
            "values": None,
            "help": None,
        },
        {
            "name": "optional_pick",
            "type": "enum",
            "default": None,
            "min": None,
            "max": None,
            "values": ["x", "y"],
            "help": None,
        },
    ]

    assert {field["name"]: field["required"] for field in input_fields(Everything)} == {
        name: info.is_required() for name, info in Everything.model_fields.items()
    }
    with pytest.raises(ValueError):
        build_inputs(Everything, {"needed": 1, "events": ["c"]})
    values = build_inputs(Everything, {"needed": 1, "events": ["a", "b"], "mode": "fast"}).model_dump()
    assert values["events"] == ["a", "b"]
    assert values["mode"] is Mode.FAST
    assert values["needed"] == 1


def test_help_and_bounds() -> None:
    class Sized(BaseModel):
        size: float = Field(default=1.0, ge=0, le=10, description="GiB")

    assert input_fields(Sized) == [
        {
            "name": "size",
            "type": "float",
            "required": False,
            "nullable": False,
            "default": 1.0,
            "min": 0,
            "max": 10,
            "values": None,
            "help": "GiB",
        }
    ]


def test_required_and_integer() -> None:
    class Inputs(BaseModel):
        count: int
        tags: list[str] = []

    with pytest.raises(ValueError):
        build_inputs(Inputs, {})
    with pytest.raises(ValueError):
        build_inputs(Inputs, {"count": 1.5})
    assert build_inputs(Inputs, {"count": 0, "tags": []}).model_dump() == {"count": 0, "tags": []}
    with pytest.raises(ValueError):
        build_inputs(Inputs, {"count": 0, "future": True})


def test_nullable_inputs() -> None:
    class Inputs(BaseModel):
        count: int | None = None
        label: str = "default"

    fields = input_fields(Inputs)
    assert fields[0]["nullable"] is True and fields[1]["nullable"] is False
    assert build_inputs(Inputs, {"count": None}).model_dump() == {"count": None, "label": "default"}
    with pytest.raises(ValueError):
        build_inputs(Inputs, {"label": None})
