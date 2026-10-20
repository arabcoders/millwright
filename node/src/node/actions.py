from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.actions import ActionOutcome, ExtractSubtitles
from common.plan.renderer import render_subtitle
from common.process import ChildNotReapedError, drain
from common.workspace import resolve_workspace_path

from node.files import open_log, partial_output, publish_output
from node.process import spawn_tool, stop_tool

if TYPE_CHECKING:
    from pathlib import Path

    from common.actions import Action
    from common.media.probe import ProbeData

_EXTRACT_TIMEOUT_S = 120.0


@dataclass(kw_only=True, slots=True)
class ActionsSpec:
    job_id: int
    path: Path
    probe: ProbeData | None
    actions: list[Action]
    workspace_dir: Path
    log_path: Path
    output_path: Path | None = None


async def run_actions(spec: ActionsSpec, ffmpeg_bin: str) -> list[ActionOutcome]:
    spec.path = _confined(spec.workspace_dir, spec.path)
    spec.log_path = _confined(spec.workspace_dir, spec.log_path)
    if spec.output_path is not None:
        spec.output_path = _confined(spec.workspace_dir, spec.output_path)
    outcomes: list[ActionOutcome] = []
    for action in spec.actions:
        try:
            if isinstance(action, ExtractSubtitles):
                outcomes.extend(await _extract(spec, action, ffmpeg_bin))
        except ChildNotReapedError:
            raise
        except Exception as exc:
            outcomes.append(ActionOutcome(kind=action.kind, ok=False, error=str(exc)))
    await drain(asyncio.create_task(asyncio.to_thread(_append_results, spec.log_path, outcomes)))
    return outcomes


def _append_results(log_path: Path, outcomes: list[ActionOutcome]) -> None:
    with open_log(log_path) as handle:
        for outcome in outcomes:
            handle.write(f"action {outcome.kind}: {'ok' if outcome.ok else 'failed'}".encode())
            if outcome.error:
                handle.write(f" {outcome.error}".encode())
            handle.write(b"\n")


async def _extract(
    spec: ActionsSpec,
    action: ExtractSubtitles,
    ffmpeg_bin: str,
) -> list[ActionOutcome]:
    if spec.probe is None:
        return [ActionOutcome(kind=action.kind, ok=False, error="no probe")]
    streams = spec.probe.subtitle
    if action.languages:
        streams = [stream for stream in streams if stream.language in action.languages]
    labels = Counter(stream.language or f"stream{stream.index}" for stream in streams)
    names = [
        f"{stream.language or f'stream{stream.index}'}.{stream.index}"
        if labels[stream.language or f"stream{stream.index}"] > 1
        else stream.language or f"stream{stream.index}"
        for stream in streams
    ]
    if len(set(names)) != len(names):
        return [ActionOutcome(kind=action.kind, ok=False, error="subtitle artifact labels collide")]
    outcomes: list[ActionOutcome] = []
    for stream, label in zip(streams, names, strict=True):
        outcomes.extend([await _extract_one(spec, ffmpeg_bin, stream.index, label, fmt) for fmt in action.formats])
    return outcomes


async def _extract_one(
    spec: ActionsSpec,
    ffmpeg_bin: str,
    stream_index: int,
    label: str,
    fmt: str,
) -> ActionOutcome:
    output = spec.output_path or spec.path
    out = _confined(spec.workspace_dir, output.with_name(f"{output.stem}.{label}.{fmt}"))
    argv = [ffmpeg_bin, *render_subtitle(str(spec.path), str(out), stream_index=stream_index, format=fmt)]
    with partial_output(out) as partial:
        argv[-1] = str(partial.path)
        try:
            proc = await spawn_tool(
                *argv,
                workspace=output.parent,
                force_on_cancel=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_EXTRACT_TIMEOUT_S)
            except BaseException:
                await stop_tool(proc, force=True)
                raise
            if proc.returncode != 0:
                tail = (stderr or b"").decode(errors="replace")[-400:]
                return ActionOutcome(
                    kind="extract_subtitles", ok=False, error=tail or f"ffmpeg exited {proc.returncode}"
                )
            publish_output(partial, out)
            relative = str(out.relative_to(spec.workspace_dir))
            return ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[relative])
        except ChildNotReapedError:
            # Keep the partial file because the tool may still be writing to it.
            partial.retained = True
            raise
        except TimeoutError:
            return ActionOutcome(kind="extract_subtitles", ok=False, error="extraction timed out")


def _confined(root: Path, path: Path) -> Path:
    root = root.absolute()
    return resolve_workspace_path(root, str(path.relative_to(root)) if path.is_absolute() else str(path))
