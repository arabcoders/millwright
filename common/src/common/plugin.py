from __future__ import annotations

import typing
from enum import Enum
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

TextField = Annotated[str, Field(json_schema_extra={"ui": "text"})]

FieldType = Literal["int", "float", "bool", "str", "text", "password", "enum", "strings", "path_mappings"]


def input_fields(model: type[BaseModel]) -> list[dict[str, Any]]:
    return [_field(name, info) for name, info in model.model_fields.items()]


def build_inputs(model: type[BaseModel], raw: dict[str, Any]) -> BaseModel:
    unknown = sorted(set(raw) - set(model.model_fields))
    if unknown:
        msg = f"unknown inputs: {', '.join(unknown)}"
        raise ValueError(msg)
    for name, info in model.model_fields.items():
        # Pydantic's lax mode coerces booleans into numbers.
        if isinstance(raw.get(name), bool) and _annotation(info) in (int, float):
            msg = f"input {name!r} must be a number, got a bool"
            raise ValueError(msg)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from None


def _field(name: str, info: FieldInfo) -> dict[str, Any]:
    return {
        "name": name,
        "type": _field_type(info),
        "default": _default(info),
        "required": info.is_required(),
        "nullable": type(None) in get_args(info.annotation),
        "min": _bound(info, "ge"),
        "max": _bound(info, "le"),
        "values": _values(info),
        "help": info.description,
    }


def _field_type(info: FieldInfo) -> FieldType:
    annotation = _annotation(info)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "enum"
    if get_origin(annotation) is Literal:
        return "enum"
    if get_origin(annotation) is list:
        extra = info.json_schema_extra
        if isinstance(extra, dict) and extra.get("ui") == "path_mappings":
            return "path_mappings"
        return "strings"
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    extra = info.json_schema_extra
    if annotation is str and isinstance(extra, dict) and extra.get("ui") == "text":
        return "text"
    if annotation is str and isinstance(extra, dict) and extra.get("ui") == "password":
        return "password"
    return "str"


def _default(info: FieldInfo) -> Any:
    if info.is_required():
        return None
    default = info.default
    return default.value if isinstance(default, Enum) else default


def _bound(info: FieldInfo, attr: str) -> float | None:
    for constraint in info.metadata:
        value = getattr(constraint, attr, None)
        if value is not None:
            return value
    return None


def _values(info: FieldInfo) -> list[str] | None:
    annotation = _annotation(info)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [member.value for member in annotation]
    if get_origin(annotation) is Literal:
        return [str(arg) for arg in get_args(annotation)]
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if len(args) == 1 and get_origin(args[0]) is Literal:
            return [str(arg) for arg in get_args(args[0])]
    return None


def _annotation(info: FieldInfo) -> Any:
    annotation = info.annotation
    if get_origin(annotation) not in (UnionType, typing.Union):
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error["loc"])
        parts.append(f"{loc}: {error['msg']}" if loc else error["msg"])
    return ". ".join(parts)
