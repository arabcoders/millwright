from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


def apply_env_overrides(
    data: dict[str, Any], overrides: Mapping[str, str], *, json_fields: Collection[str] = ()
) -> None:
    for field, variable in overrides.items():
        if variable not in os.environ:
            continue
        value: Any = os.environ[variable]
        if field in json_fields:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                msg = f"invalid JSON in environment variable {variable}"
                raise ValueError(msg) from None
        parts = field.split(".")
        target = data
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
