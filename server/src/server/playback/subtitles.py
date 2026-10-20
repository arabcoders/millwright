from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from common.fs.acquire import directory_fd, regular_fd
from common.fs.identity import source_identity

# Native (vtt) tracks sort ahead of overlay-rendered ones.
SOURCE_FORMATS = {"vtt": 0, "srt": 1, "ass": 2}
SUBTITLE_SUFFIXES = {".vtt": "vtt", ".srt": "srt", ".ass": "ass", ".ssa": "ass"}
EMBEDDED_CODECS = {
    "subrip": ("srt", "vtt"),
    "srt": ("srt", "vtt"),
    "webvtt": ("vtt", "vtt"),
    "vtt": ("vtt", "vtt"),
    "mov_text": ("mov_text", "vtt"),
    "ass": ("ass", "ass"),
    "ssa": ("ass", "ass"),
}
BITMAP_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvb_subtitle", "dvd_subtitle"}
UNSUPPORTED_CODECS = {"dvb_teletext", "arib_caption"}
FONT_MIME_PREFIXES = ("font/", "application/x-truetype", "application/vnd.ms-opentype", "application/x-font")
FONT_MIMES = {
    "font/ttf": "font/ttf",
    "font/otf": "font/otf",
    "font/woff": "font/woff",
    "font/woff2": "font/woff2",
    "application/x-truetype-font": "font/ttf",
    "application/x-truetype-ttf": "font/ttf",
    "application/vnd.ms-opentype": "font/otf",
    "application/x-font-ttf": "font/ttf",
    "application/x-font-otf": "font/otf",
}
EXTERNAL_MAX_BYTES = 2 * 1024 * 1024
EMBEDDED_MAX_BYTES = 4 * 1024 * 1024
MAX_FONTS = 16
FONT_MAX_BYTES = 8 * 1024 * 1024
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252")
_LANG_SUFFIX = re.compile(r"\.(?P<lang>[A-Za-z]{2,3})\.[A-Za-z0-9]{1,4}$")


class SubtitleError(Exception):
    pass


@dataclass(kw_only=True, slots=True)
class SubtitleTrack:
    track_id: str
    kind: str  # "external" | "embedded"
    lang: str
    name: str
    source_format: str
    delivery_format: str
    renderer: str  # native | assjs | bitmap | unsupported
    # External basename or embedded absolute stream index.
    source: str
    # External track URLs include the sidecar's path-and-size generation.
    identity: dict[str, int] | None = field(default=None)

    def manifest(self, base: str) -> dict:
        entry = {
            "id": self.track_id,
            "lang": _safe(self.lang),
            "name": _safe(self.name),
            "source_format": self.source_format,
            "delivery_format": self.delivery_format,
            "renderer": self.renderer,
        }
        if self.renderer in {"native", "assjs"}:
            entry["url"] = f"{base}/subtitles/{self.track_id}.{self.delivery_format}"
            if self.kind == "external":
                version = hashlib.sha256(json.dumps([self.source, self.identity], sort_keys=True).encode()).hexdigest()[
                    :16
                ]
                entry["url"] += f"?v={version}"
        return entry


def _safe(value: str) -> str:
    return value.replace("\r", "").replace("\n", "").strip()


def decode_text(data: bytes) -> str:
    for encoding in _TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError, UnicodeError:
            continue
    return data.decode("utf-8", errors="replace")


def srt_to_vtt(text: str) -> str:
    lines = ["WEBVTT", ""]
    for block in re.split(r"\n\s*\n", text.strip(), flags=re.MULTILINE):
        rows = [row.strip("\ufeff").rstrip() for row in block.splitlines() if row.strip()]
        if not rows:
            continue
        if _SRT_TIME.fullmatch(rows[0]) is None and "-->" not in rows[0]:
            # Numeric cue counter: WebVTT permits but does not need it.
            rows = rows[1:]
        if not rows or "-->" not in rows[0]:
            continue
        rows[0] = rows[0].replace(",", ".")
        lines.extend(rows)
        lines.append("")
    if len(lines) <= 2:
        raise SubtitleError("no subtitle cues found")
    return "\n".join(lines)


_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}")


def read_external(parent: Path, name: str) -> str:
    with directory_fd(parent) as directory, regular_fd(name, directory) as fd:
        info = os.fstat(fd)
        if info.st_size > EXTERNAL_MAX_BYTES:
            raise SubtitleError("subtitle file too large")
        data = b""
        while len(data) < info.st_size:
            chunk = os.pread(fd, min(65536, info.st_size - len(data)), len(data))
            if not chunk:
                break
            data += chunk
        if len(data) != info.st_size:
            raise SubtitleError("subtitle changed while reading")
    return decode_text(data)


def discover_external(media: Path) -> list[SubtitleTrack]:
    found: list[tuple[int, str, SubtitleTrack]] = []
    try:
        with directory_fd(media.parent) as directory:
            entries = sorted(os.listdir(directory))
    except OSError:
        return []
    stem = media.stem
    for entry in entries:
        path = Path(entry)
        if not path.name.startswith(f"{stem}."):
            continue
        source_format = SUBTITLE_SUFFIXES.get(path.suffix.lower())
        if source_format is None:
            continue
        try:
            with directory_fd(media.parent) as directory, regular_fd(path.name, directory) as fd:
                info = os.fstat(fd)
                if info.st_size < 1:
                    continue
                identity = source_identity(info)
        except OSError, ValueError:
            continue
        lang_match = _LANG_SUFFIX.search(path.name)
        lang = lang_match.group("lang").lower() if lang_match else "und"
        delivery = "ass" if source_format == "ass" else "vtt"
        track = SubtitleTrack(
            track_id="",
            kind="external",
            lang=lang,
            name=f"{path.suffix[1:].upper()} ({len(found)}) - {lang}",
            source_format=source_format,
            delivery_format=delivery,
            renderer="assjs" if delivery == "ass" else "native",
            source=path.name,
            identity=identity,
        )
        found.append((SOURCE_FORMATS[source_format], path.name, track))
    tracks: list[SubtitleTrack] = []
    for ordinal, (_order, _name, track) in enumerate(sorted(found)):
        track.track_id = f"x{ordinal}"
        tracks.append(track)
    return tracks


def discover_embedded(raw: dict) -> list[SubtitleTrack]:
    tracks: list[SubtitleTrack] = []
    ordinal = 0
    for stream in raw.get("streams") or []:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "")
        index = int(stream["index"])
        lang = str((stream.get("tags") or {}).get("language") or "und").lower()
        title = (stream.get("tags") or {}).get("title")
        if codec in BITMAP_CODECS or codec in UNSUPPORTED_CODECS:
            tracks.append(
                SubtitleTrack(
                    track_id=f"e{index}",
                    kind="embedded",
                    lang=lang,
                    name=title or f"Bitmap ({ordinal}) - {lang}",
                    source_format=codec,
                    delivery_format="",
                    renderer="bitmap" if codec in BITMAP_CODECS else "unsupported",
                    source=str(index),
                )
            )
        elif codec in EMBEDDED_CODECS:
            source_format, delivery = EMBEDDED_CODECS[codec]
            name = title or f"{source_format.upper()} ({ordinal}) - {lang}"
            tracks.append(
                SubtitleTrack(
                    track_id=f"e{index}",
                    kind="embedded",
                    lang=lang,
                    name=name,
                    source_format=source_format,
                    delivery_format=delivery,
                    renderer="assjs" if delivery == "ass" else "native",
                    source=str(index),
                )
            )
        ordinal += 1
    return tracks


@dataclass(kw_only=True, slots=True)
class FontAttachment:
    font_id: str
    family: str
    mimetype: str
    stream_index: int

    def manifest(self, base: str) -> dict:
        return {
            "id": self.font_id,
            "family": _safe(self.family),
            "mimetype": self.mimetype,
            "url": f"{base}/fonts/{self.font_id}",
        }


def discover_fonts(raw: dict) -> list[FontAttachment]:
    fonts: list[FontAttachment] = []
    for stream in raw.get("streams") or []:
        if stream.get("codec_type") != "attachment":
            continue
        tags = stream.get("tags") or {}
        mimetype = str(tags.get("mimetype") or "").lower()
        if not mimetype.startswith(FONT_MIME_PREFIXES):
            continue
        filename = str(tags.get("filename") or "").strip()
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            continue
        family = Path(filename).stem
        if not family or any(font.family == family for font in fonts):
            continue
        fonts.append(
            FontAttachment(
                font_id=f"f{stream['index']}",
                family=family,
                mimetype=FONT_MIMES.get(mimetype, "font/ttf"),
                stream_index=int(stream["index"]),
            )
        )
        if len(fonts) >= MAX_FONTS:
            break
    return fonts


def extraction_argv(track: SubtitleTrack, src: str, dst: str) -> list[str]:
    codec = ["-c:s", "copy"] if track.delivery_format == "ass" else ["-c:s", "webvtt"]
    return [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        src,
        "-map",
        f"0:{track.source}",
        *codec,
        "-fs",
        str(EMBEDDED_MAX_BYTES),
        "-f",
        "ass" if track.delivery_format == "ass" else "webvtt",
        dst,
    ]


def font_argv(stream_index: int, src: str, dst: str) -> list[str]:
    # dump_attachment writes to its option value. FFmpeg still requires one
    # output, satisfied by the null muxer so the run exits successfully.
    return [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-dump_attachment:" + str(stream_index),
        dst,
        "-i",
        src,
        "-t",
        "0",
        "-c",
        "copy",
        "-f",
        "null",
        "-",
    ]


def track_by_id(tracks: list[SubtitleTrack], track_id: str) -> SubtitleTrack | None:
    return next((track for track in tracks if track.track_id == track_id), None)


def font_by_id(fonts: list[FontAttachment], font_id: str) -> FontAttachment | None:
    return next((font for font in fonts if font.font_id == font_id), None)


_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")
_FAMILY_NAME_IDS = (16, 1)
_FULL_NAME_IDS = (4, 2)


def font_names(data: bytes) -> list[str]:
    """
    ASS scripts can reference any declared family or full name, so report them all.

    Malformed data yields no names. Never infer a family from the attachment name.
    """
    if len(data) < 12 or data[:4] not in _SFNT_MAGIC:
        return []
    num_tables = struct.unpack_from(">H", data, 4)[0]
    if len(data) < 12 + num_tables * 16:
        return []
    for index in range(num_tables):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        if tag != b"name":
            continue
        if offset + length > len(data) or length < 6:
            return []
        return _parse_name_table(data[offset : offset + length])
    return []


def font_metadata(data: bytes) -> dict:
    names = font_names(data)
    if not names:
        raise SubtitleError("Font family metadata is unavailable.")
    weight, style = 400, "normal"
    count = struct.unpack_from(">H", data, 4)[0]
    for index in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        if tag == b"OS/2" and length >= 64 and offset + length <= len(data):
            weight = min(1000, max(1, struct.unpack_from(">H", data, offset + 4)[0]))
            flags = struct.unpack_from(">H", data, offset + 62)[0]
            style = "italic" if flags & 1 else "normal"
    return {"families": names, "weight": str(weight), "style": style}


def _parse_name_table(table: bytes) -> list[str]:
    count, string_offset = struct.unpack_from(">HH", table, 2)
    if len(table) < 6 + count * 12:
        return []
    entries: dict[int, list[tuple[int, int, int]]] = {}
    for index in range(count):
        platform, _encoding, _language, name_id, length, offset = struct.unpack_from(">HHHHHH", table, 6 + index * 12)
        entries.setdefault(name_id, []).append((platform, string_offset + offset, length))
    names: list[str] = []
    for name_ids in (_FAMILY_NAME_IDS, _FULL_NAME_IDS):
        for name_id in name_ids:
            for platform, offset, length in entries.get(name_id, ()):
                start, end = offset, offset + length
                if end > len(table):
                    continue
                raw = table[start:end]
                text = raw.decode("utf-16-be" if platform in (0, 3) else "latin-1", errors="replace")
                text = text.strip()
                if text and text not in names:
                    names.append(text)
    return names
