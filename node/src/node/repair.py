from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from common.media.probe import ProbeData, mkv_repair_ratio, probe_from_raw
from common.plan.renderer import render_subtitle
from common.plan.verification import validate_streams
from common.process import ChildNotReapedError, drain
from common.workspace import resolve_workspace_path

from node.files import create_output
from node.process import spawn_tool, stop_tool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection

    from common.capabilities import MkvTools

    from node.files import _Output

_UNKNOWN = {"", "unknown", "none"}
_FONT_MIMES = {".ttf": "application/x-truetype-font", ".otf": "application/vnd.ms-opentype"}


@dataclass(kw_only=True, frozen=True, slots=True)
class PreparedInput:
    path: Path
    probe: ProbeData


@asynccontextmanager
async def prepare_input(
    source: Path,
    probe: ProbeData,
    repair_dir: Path,
    *,
    tools: MkvTools,
    workspace_dir: Path,
    ffmpeg_bin: str | Path,
    ffprobe_bin: str | Path,
    log: Callable[[str], None],
    min_free_gb: float = 0,
    timeout_s: float = 600,
) -> AsyncIterator[PreparedInput]:
    ratio = mkv_repair_ratio(probe)
    if not ratio:
        yield PreparedInput(path=source, probe=probe)
        return
    source = resolve_workspace_path(workspace_dir, str(source.relative_to(workspace_dir)))
    repair_dir = resolve_workspace_path(
        workspace_dir, str((repair_dir / "repair-input.mkv").relative_to(workspace_dir))
    ).parent
    missing = [name for name in ("mkvmerge", "mkvextract", "mkvpropedit") if getattr(tools, name) is None]
    if missing:
        msg = f"MKV input normalization requires missing tools: {', '.join(missing)}."
        raise ValueError(msg)
    assert tools.mkvmerge is not None
    assert tools.mkvextract is not None
    assert tools.mkvpropedit is not None
    mkvmerge = tools.mkvmerge
    mkvextract = tools.mkvextract
    outputs: list[_Output] = []
    unreaped = False

    def output(name: str) -> _Output:
        acquired = create_output(repair_dir / name)
        outputs.append(acquired)
        return acquired

    async def identify(path: Path) -> dict:
        return json.loads(await _run([mkvmerge, "-J", str(path)], repair_dir.parent, log, timeout_s))

    async def extract(path: Path, mode: str, target: _Output, identifier: int | None = None) -> None:
        destination = str(target.path) if identifier is None else f"{identifier}:{target.path}"
        await _run([mkvextract, str(path), mode, destination], repair_dir.parent, log, timeout_s)

    try:
        log("phase: normalize MKV input\n")
        original = await identify(source)
        tracks: list[dict] = original.get("tracks", [])
        attachments: list[dict] = original.get("attachments", [])
        _check_alignment(probe, tracks, attachments)
        replacements = [track for track in tracks if track["properties"].get("codec_id") == "S_TEXT/WEBVTT"]
        for track, stream in zip(tracks, probe.streams, strict=False):
            if stream.codec_type == "subtitle" and stream.codec.lower() in _UNKNOWN and track not in replacements:
                codec = track["properties"].get("codec_id", "unknown")
                msg = f"Unsupported subtitle codec {codec!r} in MKV track {track['id']}."
                raise ValueError(msg)
        fonts = [
            attachment
            for attachment in attachments
            if Path(attachment["file_name"]).suffix.lower() in {".ttf", ".otf"}
            and attachment.get("content_type", "").lower().startswith("image/")
        ]
        if not replacements and not fonts:
            raise ValueError("MKV identification did not match the input's reported repair requirements.")
        source_size = source.stat().st_size
        required = min_free_gb * 1e9 + source_size * ratio
        free = shutil.disk_usage(source.parent).free
        if free < required:
            msg = f"Not enough workspace space for MKV normalization. Available {free} bytes. Required {required:g} bytes."
            raise ValueError(msg)
        font_digests: dict[int, str] = {}
        # MIME repair must preserve attachment bytes, not just their metadata.
        for n, attachment in enumerate(fonts):
            extracted = output(f"repair-font-{n}")
            await extract(source, "attachments", extracted, attachment["id"])
            extension = Path(attachment["file_name"]).suffix.lower()
            font_digests[attachment["properties"]["uid"]] = await drain(
                asyncio.create_task(asyncio.to_thread(_font_digest, extracted.path, extension))
            )
            extracted.discard()
        normalized = output("repair-input.mkv")
        if replacements:
            subtitles: list[_Output] = []
            for n, track in enumerate(replacements):
                extracted = output(f"repair-subtitle-{n}.vtt")
                converted = output(f"repair-subtitle-{n}.srt")
                await extract(source, "tracks", extracted, track["id"])
                argv = render_subtitle(str(extracted.path), str(converted.path), stream_index=0, format="srt")
                await _run([str(ffmpeg_bin), *argv], repair_dir.parent, log, timeout_s)
                log(f"MKV track {track['id']}: converted WebVTT to SRT. WebVTT formatting is not retained.\n")
                extracted.discard()
                subtitles.append(converted)
            replacement_ids = {track["id"]: n + 1 for n, track in enumerate(replacements)}
            order = ",".join(
                f"{replacement_ids[track['id']]}:0" if track["id"] in replacement_ids else f"0:{track['id']}"
                for track in tracks
            )
            argv = [
                tools.mkvmerge,
                "--normalize-language-ietf",
                "off",
                "--disable-track-statistics-tags",
                "--track-order",
                order,
            ]
            kept_subtitles = [
                str(track["id"])
                for track in tracks
                if track["type"] == "subtitles" and track["id"] not in replacement_ids
            ]
            argv += ["-o", str(normalized.path)]
            argv += ["--subtitle-tracks", ",".join(kept_subtitles)] if kept_subtitles else ["--no-subtitles"]
            argv += [str(source)]
            for track, subtitle in zip(replacements, subtitles, strict=True):
                properties = track["properties"]
                argv += [
                    "--language",
                    f"0:{properties.get('language_ietf') or properties.get('language', 'und')}",
                    "--track-name",
                    f"0:{properties.get('track_name', '')}",
                    "--default-track-flag",
                    f"0:{int(properties.get('default_track', False))}",
                    "--forced-display-flag",
                    f"0:{int(properties.get('forced_track', False))}",
                    str(subtitle.path),
                ]
            await _run(argv, repair_dir.parent, log, timeout_s)
        else:
            await drain(asyncio.create_task(asyncio.to_thread(shutil.copyfile, source, normalized.path)))
        if fonts:
            argv = [tools.mkvpropedit, str(normalized.path)]
            for attachment in fonts:
                uid = attachment["properties"]["uid"]
                mime = _FONT_MIMES[Path(attachment["file_name"]).suffix.lower()]
                argv += ["--attachment-mime-type", mime, "--update-attachment", f"={uid}"]
            await _run(argv, repair_dir.parent, log, timeout_s)
        repaired = await identify(normalized.path)
        _check_tracks(tracks, repaired.get("tracks", []), {track["id"] for track in replacements})
        _check_attachments(attachments, repaired.get("attachments", []), font_digests)
        for n, attachment in enumerate(repaired.get("attachments", [])):
            uid = attachment["properties"]["uid"]
            if uid not in font_digests:
                continue
            extracted = output(f"repair-verified-font-{n}")
            await extract(normalized.path, "attachments", extracted, attachment["id"])
            digest = await drain(
                asyncio.create_task(
                    asyncio.to_thread(_font_digest, extracted.path, Path(attachment["file_name"]).suffix.lower())
                )
            )
            if digest != font_digests[uid]:
                msg = f"MKV normalization changed font attachment UID {uid}."
                raise ValueError(msg)
            extracted.discard()
        raw = json.loads(
            await _run(
                [
                    str(ffprobe_bin),
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-show_format",
                    str(normalized.path),
                ],
                repair_dir.parent,
                log,
                timeout_s,
            )
        )
        normalized_probe = probe_from_raw(normalized.path, normalized.path.stat().st_size, raw)
        _check_alignment(normalized_probe, repaired.get("tracks", []), repaired.get("attachments", []))
        validate_streams(probe, normalized_probe, [])
        if probe.duration_s > 0 and not 0.98 <= normalized_probe.duration_s / probe.duration_s <= 1.02:
            raise ValueError("MKV normalization changed the input duration.")
        log("MKV input normalization verified.\n")
        yield PreparedInput(path=normalized.path, probe=normalized_probe)
    except ChildNotReapedError:
        # Cleanup must not unlink files still in use by an interrupted tool.
        unreaped = True
        raise
    finally:
        if not unreaped:
            for acquired in reversed(outputs):
                acquired.discard()


async def _run(argv: list[str], workspace: Path, log: Callable[[str], None], timeout_s: float) -> bytes:
    log(f"normalization argv: {json.dumps(argv)}\n")
    proc = await spawn_tool(
        *argv,
        workspace=workspace,
        force_on_cancel=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        await stop_tool(proc, force=True)
        msg = f"{Path(argv[0]).name} normalization timed out after {timeout_s:g} seconds."
        raise ValueError(msg) from None
    except BaseException:
        await stop_tool(proc, force=True)
        raise
    if stderr:
        log(stderr.decode(errors="replace"))
    if proc.returncode != 0:
        detail = (stderr or stdout).decode(errors="replace").strip()
        msg = f"{Path(argv[0]).name} normalization exited {proc.returncode}: {detail or 'No diagnostic output.'}"
        raise ValueError(msg)
    if "-J" not in argv and "-print_format" not in argv and stdout:
        log(stdout.decode(errors="replace"))
    return stdout


def _check_alignment(probe: ProbeData, tracks: list[dict], attachments: list[dict]) -> None:
    if len(probe.streams) != len(tracks) + len(attachments):
        raise ValueError("MKV identification does not match the probed stream count.")
    types = {"audio": "audio", "video": "video", "subtitles": "subtitle"}
    for n, track in enumerate(tracks):
        stream = probe.streams[n]
        properties = track["properties"]
        if stream.index != n or stream.codec_type != types.get(track["type"]):
            raise ValueError("MKV track order does not match the probed stream order.")
        if (stream.language or "und") != properties.get("language", "und") or (stream.title or "") != properties.get(
            "track_name", ""
        ):
            raise ValueError("MKV track metadata does not match the probed stream order.")
    for n, attachment in enumerate(attachments, start=len(tracks)):
        if probe.streams[n].index != n or probe.streams[n].filename != attachment["file_name"]:
            raise ValueError("MKV attachment order does not match the probed stream order.")


def _check_tracks(before: list[dict], after: list[dict], replaced: Collection[int]) -> None:
    if len(before) != len(after):
        raise ValueError("MKV normalization changed the track count.")
    for original, repaired in zip(before, after, strict=True):
        expected = original["properties"].copy()
        actual = repaired["properties"]
        keys = ["language", "track_name", "default_track", "forced_track"]
        if original["id"] not in replaced:
            keys += ["codec_id"]
        elif actual.get("codec_id") != "S_TEXT/UTF8":
            raise ValueError("MKV normalization did not produce an SRT subtitle track.")
        if original["type"] != repaired["type"] or any(expected.get(key) != actual.get(key) for key in keys):
            msg = f"MKV normalization changed track identity or metadata for track {original['id']}."
            raise ValueError(msg)


def _check_attachments(before: list[dict], after: list[dict], fonts: dict[int, str]) -> None:
    if len(before) != len(after):
        raise ValueError("MKV normalization changed the attachment count.")
    for original, repaired in zip(before, after, strict=True):
        expected = original.copy()
        if original["properties"]["uid"] in fonts:
            expected["content_type"] = _FONT_MIMES[Path(original["file_name"]).suffix.lower()]
        for key in ("file_name", "content_type", "description", "size", "properties"):
            if expected.get(key) != repaired.get(key):
                raise ValueError("MKV normalization changed attachment identity or metadata.")


def _font_digest(path: Path, extension: str) -> str:
    with path.open("rb") as source:
        header = source.read(12)
        signatures = {".ttf": {b"\x00\x01\x00\x00", b"true"}, ".otf": {b"OTTO"}}
        if len(header) != 12 or header[:4] not in signatures[extension]:
            raise ValueError("Image-labelled font attachment does not contain a matching font signature.")
        source.seek(0)
        return hashlib.file_digest(source, "sha256").hexdigest()
