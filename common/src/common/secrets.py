from __future__ import annotations

from pathlib import Path

_FILE_PREFIX = "file:"


def resolve_secret(value: str, *, base_dir: Path | None = None) -> str:
    if not value.startswith(_FILE_PREFIX):
        return value
    path = Path(value[len(_FILE_PREFIX) :])
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"secret file {str(path)!r} is unreadable: {exc}"
        raise ValueError(msg) from exc
    if not content:
        msg = f"secret file {str(path)!r} is empty"
        raise ValueError(msg)
    return content
