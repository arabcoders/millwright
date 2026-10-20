from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import os


def source_identity(stat: os.stat_result) -> dict[str, int]:
    return {"size": stat.st_size}
