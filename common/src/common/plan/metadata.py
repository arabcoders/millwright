from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.media.probe import ProbeData
    from common.metadata import MetadataValues

# MP4 uses mdta keys. Only the keys below are supported.
_KEYS = {
    "mkv": {
        "title": "title",
        "description": "DESCRIPTION",
        "date": "DATE_RELEASED",
        "genre": "GENRE",
        "artist": "ARTIST",
        "show": "SHOW",
        "season_number": "SEASON_NUMBER",
        "episode_sort": "EPISODE_SORT",
    },
    "mp4": {
        "title": "title",
        "description": "description",
        "date": "date",
        "genre": "genre",
        "artist": "artist",
        "show": "show",
        "season_number": "season_number",
        "episode_sort": "episode_sort",
    },
}
_ALIASES = {
    "date": ("date", "date_released", "year"),
    "show": ("show", "tvshow"),
    "description": ("description", "synopsis"),
}


def global_tags(probe: ProbeData) -> dict[str, str]:
    tags = ((probe.raw or {}).get("format") or {}).get("tags") or {}
    return {key: str(value) for key, value in tags.items()}


def field_tags(field: str, tags: dict[str, str]) -> list[tuple[str, str]]:
    aliases = _ALIASES.get(field, (field,))
    return [(key, value) for key, value in tags.items() if key.casefold() in aliases]


def _equal(field: str, current: str, wanted: str) -> bool:
    if field in {"season_number", "episode_sort"} and current.isdecimal() and wanted.isdecimal():
        return int(current) == int(wanted)
    return current.strip() == wanted.strip()


def metadata_updates(values: MetadataValues | None, probe: ProbeData, container: str) -> dict[str, str]:
    if values is None:
        return {}
    if container not in _KEYS:
        msg = f"Sidecar metadata embedding supports MKV and MP4, not {container.upper()}."
        raise ValueError(msg)
    tags = global_tags(probe)
    updates: dict[str, str] = {}
    for field, wanted in values.model_dump(exclude_none=True).items():
        existing = field_tags(field, tags)
        if existing and all(_equal(field, current, wanted) for _, current in existing):
            continue
        key = _KEYS[container][field]
        # Replace existing spelling and aliases rather than adding a second title/date.
        # MP4 mdta preserves key casing. MKV normalizes most keys to uppercase.
        if existing:
            updates.update({name: wanted for name, _ in existing})
        else:
            updates[key] = wanted
    return updates
