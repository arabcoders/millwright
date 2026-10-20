from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

from common.fs.acquire import entry_stat
from common.metadata import MetadataSource, MetadataValues, SidecarMetadata

if TYPE_CHECKING:
    from server.flows.definition import FlowDefinition

LOG = logging.getLogger(__name__)

_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}


def sidecar_subtitles(source: str) -> list[str]:
    path = Path(source)
    parent = path.parent
    try:
        parent_stat = entry_stat(parent, follow_symlinks=True)
    except NotADirectoryError:
        return []
    if parent_stat is None or not S_ISDIR(parent_stat.st_mode):
        return []
    candidates = []
    for item in parent.iterdir():
        if item.suffix.lower() not in _SUBTITLE_SUFFIXES:
            continue
        item_stat = entry_stat(item)
        if item_stat is not None and S_ISREG(item_stat.st_mode):
            candidates.append(item.name)
    return sorted(candidates)


def sidecar_metadata(source: str, flow: FlowDefinition) -> SidecarMetadata:
    from server.flows.definition import PluginNode

    if not any(isinstance(node, PluginNode) and node.plugin == "action.embed_metadata" for node in flow.nodes):
        return SidecarMetadata()
    path = Path(source)
    result = SidecarMetadata()
    candidates: list[tuple[MetadataSource, Path]] = [
        ("nfo", path.with_suffix(".nfo")),
        ("info_json", path.with_suffix(".info.json")),
    ]
    for kind, candidate in candidates:
        if candidate.name == "tvshow.nfo":
            continue
        try:
            content = candidate.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            if kind == "info_json" and isinstance(exc, UnicodeDecodeError):
                LOG.warning("Skipping unusable JSON metadata sidecar %s: %s", candidate, exc)
                continue
            result.errors[kind] = f"Cannot read metadata sidecar {candidate}: {exc}. Fix it or remove it."
            continue
        try:
            metadata = _nfo(content) if kind == "nfo" else _info_json(content)
        except json.JSONDecodeError as exc:
            LOG.warning("Skipping unusable JSON metadata sidecar %s: %s", candidate, exc)
        except (ET.ParseError, ValueError, TypeError) as exc:
            result.errors[kind] = f"Invalid metadata sidecar {candidate}: {exc}. Fix it or remove it."
        else:
            if metadata is not None:
                result.sources[kind] = metadata
    return result


def _nfo(content: str) -> MetadataValues | None:
    if re.fullmatch(r"https?://[^\s<>]+", content.strip(), flags=re.IGNORECASE):
        return None
    root = ET.fromstring(content)  # noqa: S314 -- Library sidecars are operator-controlled, like the media.
    if root.tag not in {"movie", "episodedetails"}:
        return None

    def text(name: str) -> str | None:
        return (root.findtext(name) or "").strip() or None

    genres = [value for item in root.findall("genre") if (value := (item.text or "").strip())]
    return MetadataValues(
        title=text("title"),
        description=text("plot") or text("outline"),
        date=text("aired") if root.tag == "episodedetails" else text("premiered") or text("year"),
        genre="; ".join(genres) or None,
        show=text("showtitle") if root.tag == "episodedetails" else None,
        season_number=_number(text("season"), "season") if root.tag == "episodedetails" else None,
        episode_sort=_number(text("episode"), "episode") if root.tag == "episodedetails" else None,
    )


def _number(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not value.isdecimal():
        msg = f"{field} must be a nonnegative integer, got {value!r}"
        raise ValueError(msg)
    return str(int(value))


def _info_json(content: str) -> MetadataValues | None:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("expected a yt-dlp individual-video JSON object")
    if data.get("_type") not in {None, "video"} or "entries" in data:
        return None

    def text(name: str) -> str | None:
        value = data.get(name)
        if value is not None and not isinstance(value, str):
            msg = f"{name} must be text"
            raise ValueError(msg)
        return value.strip() or None if value is not None else None

    categories = data.get("categories") or []
    if not isinstance(categories, list) or any(not isinstance(value, str) for value in categories):
        raise ValueError("categories must be a list of text values")
    date = text("upload_date")
    if date is not None:
        if len(date) != 8 or not date.isdecimal():
            msg = f"upload_date must use YYYYMMDD, got {date!r}"
            raise ValueError(msg)
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return MetadataValues(
        title=text("title") or text("fulltitle"),
        description=text("description"),
        date=date,
        genre="; ".join(value.strip() for value in categories if value.strip()) or None,
        artist=text("channel") or text("uploader"),
    )
