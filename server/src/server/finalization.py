from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from common.actions import Action, ActionOutcome, PreserveTimestamps
from common.enums import JobStatus, TranscodeStatus
from common.fs import effects
from common.fs.identity import source_identity
from common.media.probe import ProbeData, probe_from_raw
from common.media.tools import run_ffprobe
from common.plan import dst_for
from common.plan.models import FFmpegPlan
from common.plan.renderer import render
from common.plan.verification import validate_duration, validate_streams
from common.workspace import resolve_workspace_path
from pydantic import TypeAdapter
from sqlalchemy import delete, exists, or_, select, update

from server.fs.source import lock_job_source
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    ProbeRow,
    TrashEntry,
    utcnow,
)
from server.notifications import record_publication_notifications
from server.queue.store import finish, set_transcode_result
from server.storage import storage_io
from server.trash import _confined, library_mutation_lock, store_trash, trash_path
from server.workspace import staged_dates, verify_staged_source, wait_workspace_io

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


LOG = logging.getLogger(__name__)
QUERY_BATCH = 500


class FinalizationError(ValueError):
    pass


@dataclass(kw_only=True)
class PublicationResult:
    committed: bool
    pending: bool = False
    probe: ProbeData | None = None
    warning: str | None = None
    required_artifacts: dict[str, str] = field(default_factory=dict)


async def finalize(
    sessionmaker: async_sessionmaker[AsyncSession],
    attempt_id: int,
    *,
    workspace_dir: Path,
    trash_root: Path | None,
    ffprobe_bin: str | Path,
    preserve_timestamps: bool = False,
) -> PublicationResult:
    async with sessionmaker() as session:
        attempt = await session.get(JobAttempt, attempt_id)
        job = await session.get(Job, attempt.job_id) if attempt is not None else None
        media = await session.get(MediaFile, job.media_file_id) if job is not None else None
        library = await session.get(Library, job.library_id) if job is not None else None
        if (
            attempt is None
            or job is None
            or media is None
            or library is None
            or library.deleted
            or job.kind not in {"transcode", "mkv_stats"}
            or job.status != JobStatus.FINALIZING
            or attempt.attempt_number != job.attempts
            or attempt.output_path is None
            or media.library_id != library.id
        ):
            raise FinalizationError("finalization context missing")
        library_stamp = library.updated_at
        job_stamp = job.updated_at
        stages = list(
            (
                await session.scalars(
                    select(JobEvent)
                    .where(JobEvent.attempt_id == attempt.id, JobEvent.type == "stage")
                    .order_by(JobEvent.id)
                )
            ).all()
        )
        if not stages or any(event.payload.get("result") != "succeeded" for event in stages):
            raise FinalizationError("accepted final stage missing")
        stage = stages[-1].payload
        if stage.get("output_path") != attempt.output_path:
            raise FinalizationError("final stage output changed")
        attempt_root = workspace_dir / "jobs" / str(job.id) / "attempts" / str(attempt.attempt_number)
        artifact = await wait_workspace_io(lambda relative=attempt.output_path: _artifact(workspace_dir, relative))
        if artifact.parent != attempt_root:
            raise FinalizationError("final artifact is outside the attempt output")
        plan = FFmpegPlan.model_validate(stage["plan"])
        input_probe = ProbeData.model_validate(stage["probe"])
        source = Path(media.path)
        destination = Path(dst_for(media.path, plan, input_container=input_probe.container))
        reencoded = False
        dropped_subtitles = False
        for event in stages:
            stage_plan = FFmpegPlan.model_validate(event.payload["plan"])
            stage_probe = ProbeData.model_validate(event.payload["probe"])
            reencoded |= (
                stage_plan.video is not None or bool(stage_plan.audio_encode) or stage_plan.burn_subtitle is not None
            )
            indexes = {stream.index for stream in stage_probe.subtitle}
            rendered = render(
                stage_plan,
                stage_probe,
                "source",
                "output",
                subtitle_path="subtitle" if stage_plan.burn_subtitle else None,
            )
            reencoded |= any(item.outcome == "transcode" for item in rendered.stream_log)
            dropped_subtitles |= any(
                item.stream_index in indexes and item.outcome == "drop" for item in rendered.stream_log
            )
        retain = library.trash_mode == "all" or (library.trash_mode == "transcode" and reencoded)
        actions = TypeAdapter(list[Action]).validate_python(
            (job.pre_actions or []) + stage.get("post_actions", job.post_actions or [])
        )
        preserve_timestamps |= any(isinstance(action, PreserveTimestamps) for action in actions)
        required: list[tuple[str, Path]] = []
        if dropped_subtitles and not retain:
            for event in stages:
                for outcome in TypeAdapter(list[ActionOutcome]).validate_python(
                    event.payload.get("action_results", [])
                ):
                    if outcome.kind == "extract_subtitles" and outcome.ok:
                        required += await wait_workspace_io(
                            lambda event=event, outcome=outcome: _artifact_destinations(
                                workspace_dir,
                                job,
                                attempt,
                                event.payload.get("output_path"),
                                destination,
                                outcome.artifacts,
                            )
                        )
        root = await wait_workspace_io(lambda: _publication_root(source, library.roots))
        # A shared root lock permits independent publications. The attempt
        # lock excludes duplicate publishers and cleanup across journal commits.
        async with (
            library_mutation_lock(root, wait=True, shared=True),
            library_mutation_lock(attempt_root, wait=True, workspace=True),
        ):
            try:
                await _authorize_publication(
                    session, library.id, source, destination, library.roots, attempt_id=attempt_id
                )
                if library.updated_at != library_stamp or job.updated_at != job_stamp:
                    raise FinalizationError("Finalization settings changed before publication.")  # noqa: TRY301
                existing = await session.scalar(
                    select(FinalizationJournal.id).where(FinalizationJournal.attempt_id == attempt_id)
                )
                if existing is not None:
                    raise FinalizationError(  # noqa: TRY301 -- Preserve existing publication records for recovery.
                        "Publication is already journaled. Recover it before publishing again."
                    )
                verified, _ = await _finalize_locked(
                    session,
                    attempt,
                    source,
                    destination,
                    workspace_dir,
                    attempt.output_path,
                    trash_root,
                    input_probe,
                    plan,
                    ffprobe_bin,
                    preserve_timestamps,
                    library_id=library.id,
                    library_roots=library.roots,
                    trash_mode="all" if retain else "none",
                    trash_retention_days=library.trash_retention_days,
                    required_artifacts=required,
                )
                journal = await session.scalar(
                    select(FinalizationJournal).where(FinalizationJournal.attempt_id == attempt_id)
                )
                assert journal is not None
                await wait_workspace_io(lambda path=Path(journal.partial_path): path.unlink(missing_ok=True))
                return PublicationResult(
                    committed=True, probe=verified, required_artifacts={name: str(path) for name, path in required}
                )
            except Exception as exc:
                await session.rollback()
                journal = await session.scalar(
                    select(FinalizationJournal).where(FinalizationJournal.attempt_id == attempt_id)
                )
                recovery_error = None
                if (
                    journal is not None
                    and journal.state in {"prepared", "discard_prepared"}
                    and await wait_workspace_io(lambda: Path(journal.source_path).exists())
                ):
                    try:
                        await _recover_locked(session, journal)
                    except Exception as cleanup_exc:
                        recovery_error = cleanup_exc
                committed = journal is not None and journal.state in {"completed", "discard_completed"}
                pending = journal is not None and (
                    committed
                    or recovery_error is not None
                    or journal.state
                    in {"original_moved", "discard_original_moved", "artifacts_pending", "discard_artifacts_pending"}
                    or (
                        journal.state in {"prepared", "discard_prepared"}
                        and not await wait_workspace_io(lambda: Path(journal.source_path).exists())
                    )
                )
                if not pending:
                    raise
                warning = f"Finalization {'cleanup' if committed else 'recovery'} pending ({type(exc).__name__}): {exc or type(exc).__name__}. Publication recovery records retained."
                if recovery_error is not None:
                    warning += f" File replacement recovery failed ({type(recovery_error).__name__}): {recovery_error}."
                probe = None
                if committed:
                    await session.refresh(attempt)
                    probe = ProbeData.model_validate(attempt.output_probe)
                return PublicationResult(
                    committed=committed,
                    pending=True,
                    probe=probe,
                    warning=warning,
                    required_artifacts={name: str(path) for name, path in required},
                )


def _publication_root(source: Path, roots: list[str]) -> Path:
    matching = [Path(root).resolve() for root in roots if source.resolve().is_relative_to(Path(root).resolve())]
    if len(matching) != 1:
        raise FinalizationError("source must be inside exactly one library root")
    return matching[0]


def _artifact_destinations(
    workspace_dir: Path, job: Job, attempt: JobAttempt, relative: str | None, final_path: Path, artifacts: list[str]
) -> list[tuple[str, Path]]:
    if relative is None or attempt.job_id != job.id or attempt.attempt_number <= 0:
        raise FinalizationError("post-action output context missing")
    output = _artifact(workspace_dir, relative)
    attempt_root = workspace_dir / "jobs" / str(job.id) / "attempts" / str(attempt.attempt_number)
    destinations = []
    for artifact in artifacts:
        source = _artifact(workspace_dir, artifact)
        if source.parent != output.parent or source.parent != attempt_root:
            raise FinalizationError("post-action artifact is outside the attempt output")
        prefix = output.stem + "."
        if not source.name.startswith(prefix) or len(source.name) <= len(prefix):
            raise FinalizationError("post-action artifact has an unsafe suffix")
        destinations.append((artifact, final_path.with_name(f"{final_path.stem}.{source.name[len(prefix) :]}")))
    return destinations


async def publish_artifacts(
    sessionmaker: async_sessionmaker[AsyncSession],
    attempt_id: int,
    results: list[ActionOutcome],
    *,
    workspace_dir: Path,
) -> list[ActionOutcome]:
    published_results = []
    async with sessionmaker() as session:
        for outcome in results:
            promoted: list[str] = []
            if outcome.ok and outcome.artifacts:
                try:
                    attempt = await session.get(JobAttempt, attempt_id, populate_existing=True)
                    job = (
                        await session.get(Job, attempt.job_id, populate_existing=True) if attempt is not None else None
                    )
                    journal = await session.scalar(
                        select(FinalizationJournal).where(FinalizationJournal.attempt_id == attempt_id)
                    )
                    if (
                        attempt is None
                        or job is None
                        or journal is None
                        or job.status != JobStatus.SUCCEEDED
                        or attempt.attempt_number != job.attempts
                        or journal.state not in {"completed", "discard_completed"}
                    ):
                        raise FinalizationError("post-action publication context missing")  # noqa: TRY301
                    library = await session.get(Library, job.library_id, populate_existing=True)
                    if library is None:
                        raise FinalizationError("publication library unavailable")  # noqa: TRY301
                    final_path = Path(journal.destination_path)
                    publication = await _publication(session, journal)
                    stages = list(
                        (
                            await session.scalars(
                                select(JobEvent)
                                .where(JobEvent.attempt_id == attempt_id, JobEvent.type == "stage")
                                .order_by(JobEvent.id)
                            )
                        ).all()
                    )
                    attempt_root = workspace_dir / "jobs" / str(job.id) / "attempts" / str(attempt.attempt_number)
                    async with (
                        library_mutation_lock(_publication_root(final_path, library.roots), wait=True, shared=True),
                        library_mutation_lock(attempt_root, wait=True, workspace=True),
                    ):
                        for artifact in outcome.artifacts:
                            source = _artifact(workspace_dir, artifact)
                            relative = next(
                                (
                                    event.payload["output_path"]
                                    for event in stages
                                    if source.name.startswith(Path(event.payload["output_path"]).stem + ".")
                                ),
                                None,
                            )
                            destinations = _artifact_destinations(
                                workspace_dir, job, attempt, relative, final_path, [artifact]
                            )
                            destination = destinations[0][1]
                            await _authorize_publication(
                                session,
                                job.library_id,
                                final_path,
                                destination,
                                publication.payload["roots"],
                                attempt_id=attempt_id,
                            )
                            await storage_io(_move_artifact, source, destination)
                            promoted.append(str(destination))
                        await session.commit()
                    outcome = outcome.model_copy(update={"artifacts": promoted})
                except Exception as exc:
                    await session.rollback()
                    outcome = outcome.model_copy(
                        update={
                            "ok": False,
                            "error": f"Artifact promotion failed: {exc}",
                            "artifacts": promoted,
                        }
                    )
            elif outcome.artifacts:
                outcome = outcome.model_copy(update={"artifacts": []})
            published_results.append(outcome)
    return published_results


async def discard_rejected_artifact(
    sessionmaker: async_sessionmaker[AsyncSession], attempt_id: int, *, workspace_dir: Path
) -> str | None:
    try:
        async with sessionmaker() as session:
            attempt = await session.get(JobAttempt, attempt_id)
            job = await lock_job_source(session, attempt.job_id, attempt_id=attempt_id) if attempt is not None else None
            if (
                attempt is None
                or job is None
                or job.status != JobStatus.VERIFYING
                or attempt.attempt_number != job.attempts
                or attempt.output_path is None
            ):
                raise FinalizationError("rejected artifact context missing")  # noqa: TRY301
            current_job_id = await session.scalar(
                update(Job)
                .where(Job.id == job.id, Job.status == JobStatus.VERIFYING, Job.attempts == attempt.attempt_number)
                .values(status=Job.status)
                .returning(Job.id)
            )
            if current_job_id is None:
                raise FinalizationError("rejected artifact record changed")  # noqa: TRY301
            root = workspace_dir / "jobs" / str(job.id) / "attempts" / str(attempt.attempt_number)
            async with library_mutation_lock(root, wait=True, workspace=True):
                await storage_io(_discard_artifact, workspace_dir, attempt.output_path, root)
            await session.commit()
    except Exception as exc:
        LOG.exception("post-gate artifact cleanup failed for attempt %s", attempt_id)
        return f"post-gate artifact cleanup failed ({type(exc).__name__}): {exc or type(exc).__name__}"
    return None


def _discard_artifact(workspace: Path, relative: str, attempt_root: Path) -> None:
    artifact = _artifact(workspace, relative)
    if artifact.parent != attempt_root:
        raise FinalizationError("rejected artifact is outside the attempt output")
    artifact.unlink()


class _RequiredArtifact(TypedDict):
    source: str
    destination: str
    copy: str
    size: int | None


def _artifact(root: Path, relative: str) -> Path:
    parts = relative.replace("\\", "/").split("/")
    if any(
        part and part not in {".", ".."} and (root.joinpath(*parts[: i + 1])).is_symlink()
        for i, part in enumerate(parts)
    ):
        raise FinalizationError("workspace artifact must not contain symlinks")
    try:
        path = resolve_workspace_path(root, relative)
    except ValueError as exc:
        raise FinalizationError(str(exc)) from exc
    if path.is_symlink() or not path.is_file():
        raise FinalizationError("workspace artifact must be a regular file")
    return path


def validate_artifact(raw: dict, path: Path, input_probe: ProbeData, plan: FFmpegPlan) -> ProbeData:
    probe = probe_from_raw(path, path.stat().st_size, raw)
    if probe.size_bytes > input_probe.size_bytes * plan.expected.size_ceiling_ratio:
        raise FinalizationError("artifact exceeds the plan size ceiling")
    rendered = render(
        plan,
        input_probe,
        input_probe.file_path,
        str(path),
        subtitle_path=plan.burn_subtitle.filename if plan.burn_subtitle is not None else None,
    )
    try:
        validate_duration(input_probe, probe, plan, rendered.stream_log)
        validate_streams(input_probe, probe, rendered.stream_log)
    except ValueError as exc:
        raise FinalizationError(str(exc)) from exc
    return probe


def _check_destination(path: Path) -> None:
    if any(parent.is_symlink() for parent in path.parents) or path.is_symlink() or not path.parent.is_dir():
        raise FinalizationError("destination parent must be a directory without symlinks")


async def _authorize_publication(
    session: AsyncSession,
    library_id: int,
    source: Path,
    destination: Path,
    roots: list[str],
    *,
    attempt_id: int | None = None,
    recovery: bool = False,
) -> None:
    # Shared library, source, and job locks remain held until the journal commit.
    library = await session.get(
        Library, library_id, populate_existing=True, with_for_update={"read": True, "of": Library}
    )
    if library is None or library.roots != roots:
        raise FinalizationError("library roots changed during finalization")
    if library.deleted and not recovery:
        raise FinalizationError("Library is being deleted. Publication was withheld.")
    if attempt_id is not None:
        attempt = await session.get(JobAttempt, attempt_id)
        job = await lock_job_source(session, attempt.job_id, attempt_id=attempt_id) if attempt is not None else None
        if job is None or attempt is None or job.library_id != library_id:
            raise FinalizationError("Publication job is unavailable.")
        if not recovery and (
            attempt.attempt_number != job.attempts or job.status not in {JobStatus.FINALIZING, JobStatus.SUCCEEDED}
        ):
            raise FinalizationError("Publication attempt no longer owns this job.")
        await session.scalar(
            select(FinalizationJournal)
            .where(FinalizationJournal.attempt_id == attempt_id)
            .with_for_update(of=FinalizationJournal)
            .execution_options(populate_existing=True)
        )

    def check_paths() -> None:
        if any(
            path.is_symlink() or any(parent.is_symlink() for parent in path.parents) for path in (source, destination)
        ):
            raise FinalizationError("publication paths must not contain symlinks")
        if sum(source.absolute().is_relative_to(Path(root).absolute()) for root in roots) != 1 or not any(
            destination.absolute().is_relative_to(Path(root).absolute())
            and source.absolute().is_relative_to(Path(root).absolute())
            for root in roots
        ):
            raise FinalizationError("publication paths no longer belong to the library")

    await wait_workspace_io(check_paths)


async def _publication(session: AsyncSession, journal: FinalizationJournal) -> JobEvent:
    event = await session.scalar(
        select(JobEvent).where(JobEvent.attempt_id == journal.attempt_id, JobEvent.type == "publication")
    )
    if event is None:
        raise FinalizationError("Publication settings are missing.")
    return event


def _move_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (
        any(parent.is_symlink() for parent in destination.parents)
        or destination.parent.is_symlink()
        or not destination.parent.is_dir()
    ):
        raise FinalizationError("destination parent must be a directory without symlinks")
    try:
        effects.move_file(
            source, destination, overwrite=False, sync_destination=lambda path: _fsync_directory(path.parent)
        )
    except FileExistsError as exc:
        error = f"artifact destination already exists: {destination}"
        raise FinalizationError(error) from exc
    except OSError as exc:
        error = f"artifact publication failed: {exc}"
        LOG.warning("artifact publication failed for %s to %s", source, destination, extra={"error": str(exc)})
        raise FinalizationError(error) from exc


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_synced(source: Path, destination: Path) -> None:
    effects.publish(source, destination)
    _fsync_directory(destination.parent)


async def _verify_media(
    ffprobe_bin: str | Path,
    path: Path,
    input_probe: ProbeData,
    plan: FFmpegPlan,
    *,
    source: Path | None = None,
    dates: dict[str, int] | None = None,
) -> ProbeData:
    if source is not None:
        # A no-overwrite failure does not permit removing an existing
        # partial. Only a completed copy may be removed on probe failure.
        await wait_workspace_io(lambda: effects.copy_file(source, path))
    try:
        if source is not None:
            await wait_workspace_io(lambda: _fsync_directory(path.parent))
        raw = await run_ffprobe(ffprobe_bin, path)
        verified = validate_artifact(raw, path, input_probe, plan)
        if dates is not None:
            os.utime(path, ns=(dates["atime_ns"], dates["mtime_ns"]))
        return verified
    except Exception:
        if source is not None:
            path.unlink(missing_ok=True)
        raise


def _verify_media_process(
    ffprobe_bin: str | Path,
    path: Path,
    input_probe: ProbeData,
    plan: FFmpegPlan,
    *,
    source: Path | None = None,
    dates: dict[str, int] | None = None,
) -> ProbeData:
    # ProcessPoolExecutor runs synchronous entry points. The verifier owns its
    # own event loop and media-tool children inside the storage process.
    return asyncio.run(
        _verify_media(
            ffprobe_bin,
            path,
            input_probe,
            plan,
            source=source,
            dates=dates,
        )
    )


def _publish_output(partial: Path, destination: Path, discard: Path | None) -> None:
    _publish_synced(partial, destination)
    if discard is not None:
        discard.unlink()
        _fsync_directory(destination.parent)


def _recover_prepared_files(
    source: Path, trash: Path, partial: Path, claim: Path | None, source_size: int, discard: bool
) -> str:
    staged = trash.parent.with_name(trash.parent.name + ".partial") / trash.name
    if not discard and staged.exists():
        if (
            source.exists()
            or trash.exists()
            or staged.is_symlink()
            or any(parent.is_symlink() for parent in staged.parents)
        ):
            raise FinalizationError("cannot recover finalization")
        if claim is None or not claim.exists():
            if staged.stat().st_size != source_size:
                raise FinalizationError("Original size changed.")
            effects.move_file(staged, source, overwrite=False)
    if not discard and claim is not None and claim.exists():
        if source.exists():
            raise FinalizationError("cannot recover finalization")
        if trash.exists():
            if trash.stat().st_size != source_size:
                raise FinalizationError("Original size changed.")
            claim.unlink()
        else:
            if claim.stat().st_size != source_size:
                raise FinalizationError("Original size changed.")
            effects.move_file(claim, source, overwrite=False)
    if not source.exists() and trash.exists():
        return "discard_original_moved" if discard else "original_moved"
    if not source.exists() or trash.exists():
        raise FinalizationError("Original location is missing or conflicting.")
    partial.unlink(missing_ok=True)
    return "discard_rolled_back" if discard else "rolled_back"


def _recover_output_files(
    source: Path, destination: Path, partial: Path, trash: Path, source_size: int, output_size: int, discard: bool
) -> bool:
    if destination.exists():
        if destination.stat().st_size != output_size:
            raise FinalizationError("Published output size changed.")
    elif partial.exists():
        if partial.stat().st_size != output_size:
            raise FinalizationError("Partial output size changed.")
        _check_destination(destination)
        _publish_synced(partial, destination)
    elif trash.exists() and not source.exists():
        if trash.stat().st_size != source_size:
            raise FinalizationError("Original size changed.")
        return False
    else:
        raise FinalizationError("cannot recover finalization")
    if discard:
        trash.unlink()
    return True


def _claim(source: Path, trash: Path) -> Path | None:
    leaf = trash.parent
    if len(leaf.name) != 12 or len(leaf.parents[0].name) != 2 or len(leaf.parents[1].name) != 2:
        return None
    identifier = leaf.parents[1].name + leaf.parents[0].name + leaf.name
    if len(identifier) != 16 or any(char not in "0123456789abcdef" for char in identifier):
        return None
    return source.with_name(f".{identifier}.claim")


async def _finalize_locked(
    session: AsyncSession,
    attempt: JobAttempt,
    source_path: Path,
    destination_path: Path,
    workspace_dir: Path,
    artifact_relative_path: str,
    trash_root: Path | None,
    input_probe: ProbeData,
    plan: FFmpegPlan,
    ffprobe_bin: str | Path,
    preserve_timestamps: bool = False,
    *,
    library_id: int,
    library_roots: list[str],
    trash_mode: str,
    trash_retention_days: int,
    required_artifacts: list[tuple[str, Path]] | None = None,
) -> tuple[ProbeData, Path]:
    artifact = await wait_workspace_io(lambda: _artifact(workspace_dir, artifact_relative_path))
    source_stat = await wait_workspace_io(source_path.stat)
    dates = (
        await wait_workspace_io(lambda: staged_dates(workspace_dir, attempt.job_id)) if preserve_timestamps else None
    )
    library_root = await wait_workspace_io(lambda: _publication_root(source_path, library_roots))
    verified = await storage_io(
        _verify_media_process,
        ffprobe_bin,
        artifact,
        input_probe,
        plan,
    )
    output_location = {"file_path": str(destination_path), "container": destination_path.suffix.lstrip(".").lower()}
    if not destination_path.absolute().is_relative_to(library_root):
        raise FinalizationError("destination is outside the source library root")
    retain = trash_mode == "all" or (
        trash_mode == "transcode"
        and (plan.video is not None or bool(plan.audio_encode) or plan.burn_subtitle is not None)
    )
    if retain and (
        trash_root is None or not await wait_workspace_io(lambda: trash_root.is_dir() and not trash_root.is_symlink())
    ):
        raise FinalizationError("trash root unavailable")
    if retain:
        assert trash_root is not None
    enabled_root = Path() if trash_root is None else trash_root
    await _authorize_publication(
        session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
    )
    attempt.source_size = source_stat.st_size
    attempt.output_size = verified.size_bytes
    attempt.output_probe = verified.model_copy(update=output_location).model_dump(mode="json")

    def prepare_paths() -> tuple[Path, Path, str | None]:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        _check_destination(destination_path)
        if destination_path != source_path and destination_path.exists():
            raise FileExistsError(destination_path)
        partial = destination_path.with_name(f".{attempt.id}.part")
        _check_destination(partial)
        if partial.exists():
            raise FileExistsError(partial)
        if retain:
            identifier = secrets.token_hex(8)
            planned_trash = trash_path(enabled_root, library_id, identifier, source_path.suffix.lower())
            while planned_trash.exists():
                identifier = secrets.token_hex(8)
                planned_trash = trash_path(enabled_root, library_id, identifier, source_path.suffix.lower())
            return partial, planned_trash, identifier
        trash_destination = source_path.with_name(f".{attempt.id}.discard")
        _check_destination(trash_destination)
        if trash_destination.exists():
            raise FileExistsError(trash_destination)
        return partial, trash_destination, None

    partial, trash_destination, identifier = await wait_workspace_io(prepare_paths)
    journal = FinalizationJournal(
        attempt_id=attempt.id,
        source_path=str(source_path),
        destination_path=str(destination_path),
        partial_path=str(partial),
        trash_path=str(trash_destination),
        state="prepared" if retain else "discard_prepared",
    )
    publication = JobEvent(
        job_id=attempt.job_id,
        attempt_id=attempt.id,
        type="publication",
        payload={
            "library_id": library_id,
            "roots": list(library_roots),
            "preserve_timestamps": preserve_timestamps,
            "dates": dates,
        },
    )
    session.add(journal)
    session.add(publication)
    await session.commit()
    copied = False
    try:
        verified = await storage_io(
            _verify_media_process,
            ffprobe_bin,
            partial,
            input_probe,
            plan,
            source=artifact,
            dates=dates,
        )
        copied = True
        verified = verified.model_copy(update=output_location)
        await _authorize_publication(
            session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
        )
        attempt.output_size = verified.size_bytes
        attempt.output_probe = verified.model_dump(mode="json")
        if not required_artifacts:
            await session.commit()
        if required_artifacts:

            def prepare_sidecars() -> list[_RequiredArtifact]:
                entries: list[_RequiredArtifact] = []
                for index, (relative, destination) in enumerate(required_artifacts):
                    _artifact(workspace_dir, relative)
                    if destination.parent != destination_path.parent or not destination.name.startswith(
                        destination_path.stem + "."
                    ):
                        raise FinalizationError("required artifact destination is outside the media output")  # noqa: TRY301
                    _check_destination(destination)
                    copy = destination.with_name(f".{attempt.id}-{index}.part")
                    _check_destination(copy)
                    if copy.exists():
                        raise FileExistsError(copy)  # noqa: TRY301
                    entries.append(
                        {
                            "source": relative,
                            "destination": str(destination),
                            "copy": str(copy),
                            "size": None,
                        }
                    )
                return entries

            entries = await wait_workspace_io(prepare_sidecars)
            required_event = JobEvent(
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                type="required_artifacts",
                payload={"entries": [dict(item) for item in entries]},
            )
            session.add(required_event)
            journal.state = "artifacts_pending" if retain else "discard_artifacts_pending"
            await session.commit()
            for entry in entries:
                sidecar = await wait_workspace_io(lambda relative=entry["source"]: _artifact(workspace_dir, relative))
                copy, destination = Path(entry["copy"]), Path(entry["destination"])
                await storage_io(effects.copy_file, sidecar, copy)
                await _authorize_publication(
                    session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
                )
                entry["size"] = (await wait_workspace_io(copy.stat)).st_size
                required_event.payload = {"entries": [dict(item) for item in entries]}
                await session.commit()
                await _authorize_publication(
                    session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
                )

                await storage_io(_publish_synced, copy, destination)
            # Sidecars are published_results before the original can enter trash or a discard claim.
            journal.state = "prepared" if retain else "discard_prepared"
            await session.commit()
        await _authorize_publication(
            session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
        )
        if retain:
            await wait_workspace_io(lambda: verify_staged_source(workspace_dir, attempt.job_id, source_path))
            await store_trash(
                session,
                enabled_root,
                library_id or 1,
                source_path,
                library_root,
                trash_retention_days,
                identifier=identifier,
            )
            journal.state = "original_moved"
        else:
            claimed = False

            def claim_original() -> None:
                nonlocal claimed
                verify_staged_source(workspace_dir, attempt.job_id, source_path)
                effects.claim(source_path, trash_destination)
                claimed = True

            try:
                await wait_workspace_io(claim_original)
            except asyncio.CancelledError:
                if claimed:
                    journal.state = "discard_original_moved"
                    await session.commit()
                raise
            journal.state = "discard_original_moved"
        await session.commit()
        await session.refresh(journal)
        await _authorize_publication(
            session, library_id, source_path, destination_path, library_roots, attempt_id=attempt.id
        )
        try:
            await storage_io(_publish_output, partial, destination_path, None if retain else trash_destination)
        except asyncio.CancelledError:
            if await wait_workspace_io(
                lambda: (
                    destination_path.is_file()
                    and destination_path.stat().st_size == verified.size_bytes
                    and (retain or not trash_destination.exists())
                )
            ):
                journal.state = "completed" if retain else "discard_completed"
                await session.commit()
            raise
        journal.state = "completed" if retain else "discard_completed"
        await session.commit()
        await session.refresh(journal)
        await _recover_required_copies(session, journal, remove=True)
        return verified, trash_destination
    except Exception:
        if copied and journal.state in {"prepared", "discard_prepared"} and await wait_workspace_io(source_path.exists):
            await wait_workspace_io(lambda: partial.unlink(missing_ok=True))
        raise


async def recover(session: AsyncSession, journal: FinalizationJournal, *, library_root: Path | None = None) -> None:
    root = library_root if library_root is not None else Path(journal.source_path).parent
    async with library_mutation_lock(root, wait=True):
        await _recover_locked(session, journal)


async def _recover_locked(session: AsyncSession, journal: FinalizationJournal) -> None:
    source, destination = Path(journal.source_path), Path(journal.destination_path)
    partial, trash = Path(journal.partial_path), Path(journal.trash_path)

    def check_paths() -> None:
        _check_destination(source)
        _check_destination(destination)
        _check_destination(partial)

    await wait_workspace_io(check_paths)
    publication = await _publication(session, journal)
    attempt = await session.get(JobAttempt, journal.attempt_id)
    if attempt is None or attempt.source_size is None or attempt.output_size is None:
        raise FinalizationError("Finalization file sizes are missing.")
    await _authorize_publication(
        session,
        publication.payload["library_id"],
        source,
        destination,
        publication.payload["roots"],
        attempt_id=journal.attempt_id,
        recovery=True,
    )
    if journal.state in {"artifacts_pending", "discard_artifacts_pending"}:
        await _recover_required_copies(session, journal)
        journal.state = "discard_prepared" if journal.state.startswith("discard_") else "prepared"
        await session.commit()
        await _authorize_publication(
            session,
            publication.payload["library_id"],
            source,
            destination,
            publication.payload["roots"],
            attempt_id=journal.attempt_id,
            recovery=True,
        )
    claim = None if journal.state.startswith("discard_") else _claim(source, trash)
    if claim is not None:
        try:
            await wait_workspace_io(lambda: _confined(trash, trash.parents[4]))
        except ValueError as exc:
            raise FinalizationError("cannot recover finalization") from exc
    if (
        destination == source
        and claim is not None
        and await wait_workspace_io(lambda: claim.exists() and source.exists())
    ):
        raise FinalizationError("cannot recover finalization")
    if journal.state in {"completed", "discard_completed"}:

        def check_completed() -> None:
            if not destination.is_file():
                raise FinalizationError("completed finalization destination is missing or conflicting")
            if destination.stat().st_size != attempt.output_size:
                raise FinalizationError("Published output size changed.")
            if journal.state == "discard_completed" and trash.exists():
                trash.unlink()

        await wait_workspace_io(check_completed)
        await _recover_required_copies(session, journal, remove=True)
        return
    discard = journal.state.startswith("discard_")
    if journal.state in {"prepared", "discard_prepared"}:
        journal.state = await storage_io(
            _recover_prepared_files, source, trash, partial, claim, attempt.source_size, discard
        )
    if journal.state in {"original_moved", "discard_original_moved"}:
        await _recover_required_copies(session, journal)
        try:
            completed = await storage_io(
                _recover_output_files,
                source,
                destination,
                partial,
                trash,
                attempt.source_size,
                attempt.output_size,
                discard,
            )
        except asyncio.CancelledError:
            if await wait_workspace_io(
                lambda: (
                    destination.is_file()
                    and destination.stat().st_size == attempt.output_size
                    and (not discard or not trash.exists())
                )
            ):
                journal.state = "discard_completed" if discard else "completed"
                await session.commit()
            raise
        if completed:
            journal.state = "discard_completed" if discard else "completed"
        else:
            entry = None
            if not discard:
                entry = await _pending_trash_entry(session, trash, source)
            await storage_io(effects.move_file, trash, source, overwrite=False)
            if entry is not None:
                await session.delete(entry)
                await wait_workspace_io(lambda: shutil.rmtree(trash.parent))
            journal.state = "discard_rolled_back" if discard else "rolled_back"
    await session.commit()
    await session.refresh(journal)
    await _authorize_publication(
        session,
        publication.payload["library_id"],
        source,
        destination,
        publication.payload["roots"],
        attempt_id=journal.attempt_id,
        recovery=True,
    )
    await _recover_required_copies(session, journal, remove=True)


async def _recover_required_copies(
    session: AsyncSession, journal: FinalizationJournal, *, remove: bool = False
) -> None:
    event = await session.scalar(
        select(JobEvent).where(JobEvent.attempt_id == journal.attempt_id, JobEvent.type == "required_artifacts")
    )
    if event is None:
        if journal.state in {"artifacts_pending", "discard_artifacts_pending"}:
            raise FinalizationError("Required sidecar records are missing.")
        return

    await storage_io(
        _recover_copies,
        Path(journal.destination_path),
        journal.attempt_id,
        journal.state,
        event.payload["entries"],
        remove=remove,
    )


def _recover_copies(
    output: Path, attempt_id: int, state: str, entries: list[_RequiredArtifact], *, remove: bool
) -> None:
    for index, entry in enumerate(entries):
        destination, copy = Path(entry["destination"]), Path(entry["copy"])
        if (
            destination.parent != output.parent
            or not destination.name.startswith(output.stem + ".")
            or copy.parent != output.parent
            or copy.name != f".{attempt_id}-{index}.part"
        ):
            raise FinalizationError("required sidecar is outside the media output")
        _check_destination(destination)
        _check_destination(copy)
        if remove:
            copy.unlink(missing_ok=True)
            continue
        if entry["size"] is None:
            raise FinalizationError("Required sidecar size is missing.")
        if state in {"artifacts_pending", "discard_artifacts_pending"} and copy.is_file() and not destination.exists():
            if copy.stat().st_size != entry["size"]:
                raise FinalizationError("Required sidecar size changed.")
            _publish_synced(copy, destination)
        if not destination.is_file():
            raise FinalizationError(
                "Required sidecar publication incomplete or conflicting. Source and publication recovery records retained."
            )
        if destination.stat().st_size != entry["size"]:
            raise FinalizationError("Required sidecar size changed.")
        _fsync_directory(destination.parent)


async def _pending_trash_entry(session: AsyncSession, trash: Path, source: Path) -> TrashEntry | None:
    def check_trash() -> tuple[int, str]:
        leaf = trash.parent
        if trash.name != f"file{source.suffix.lower()}" or not leaf.is_dir() or leaf.is_symlink():
            raise FinalizationError("cannot recover finalization")
        try:
            library_id = int(leaf.parents[2].name)
            identifier = leaf.parents[1].name + leaf.parents[0].name + leaf.name
            root = leaf.parents[3]
            _confined(trash, root)
            expected = trash_path(root, library_id, identifier, source.suffix.lower())
        except IndexError, ValueError:
            raise FinalizationError("cannot recover finalization") from None
        if expected != trash or not expected.is_file():
            raise FinalizationError("cannot recover finalization")
        return library_id, identifier

    library_id, identifier = await wait_workspace_io(check_trash)
    entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == identifier))
    if entry is None or entry.library_id != library_id or entry.original_path != str(source):
        raise FinalizationError("cannot recover finalization")
    if entry.suffix != source.suffix.lower():
        raise FinalizationError("cannot recover finalization")
    return entry


async def recover_incomplete(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    completed = failed = last_id = 0
    while True:
        async with sessionmaker() as session:
            result = await session.execute(
                select(FinalizationJournal.id)
                .join(JobAttempt, FinalizationJournal.attempt_id == JobAttempt.id)
                .join(Job, JobAttempt.job_id == Job.id)
                .where(
                    FinalizationJournal.id > last_id,
                    or_(
                        FinalizationJournal.state.in_(
                            (
                                "prepared",
                                "original_moved",
                                "discard_prepared",
                                "discard_original_moved",
                                "artifacts_pending",
                                "discard_artifacts_pending",
                            )
                        ),
                        (Job.status.in_([JobStatus.VERIFYING, JobStatus.FINALIZING]))
                        & (JobAttempt.attempt_number == Job.attempts),
                    ),
                )
                .order_by(FinalizationJournal.id)
                .limit(QUERY_BATCH)
            )
            journal_ids = list(result.scalars())
        if not journal_ids:
            break
        last_id = journal_ids[-1]
        for journal_id in journal_ids:
            journal = None
            try:
                async with sessionmaker() as session:
                    journal = await session.get(FinalizationJournal, journal_id)
                    if journal is None:
                        continue
                    await recover(session, journal, library_root=await _recovery_root(session, journal))
                    await _record_recovery_result(session, journal)
                    await session.commit()
                    if journal.state in {"completed", "discard_completed"}:
                        await wait_workspace_io(lambda path=Path(journal.partial_path): path.unlink(missing_ok=True))
                completed += 1
            except Exception as exc:
                failed += 1
                LOG.exception(
                    "Finalization recovery failed for journal %s attempt %s",
                    journal_id,
                    journal.attempt_id if journal else None,
                    extra={
                        "error": str(exc) or type(exc).__name__,
                        "error_class": type(exc).__name__,
                        "operation": "finalization_recovery",
                        "outcome": "failed",
                        "journal_id": journal_id,
                        "attempt_id": journal.attempt_id if journal else None,
                    },
                )
    failed += await _recover_unprepared(sessionmaker)
    return completed, failed


async def _recover_unprepared(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    failed = last_id = 0
    while True:
        async with sessionmaker() as session:
            missing = list(
                (
                    await session.scalars(
                        select(Job.id)
                        .where(
                            Job.id > last_id,
                            Job.status.in_([JobStatus.VERIFYING, JobStatus.FINALIZING]),
                            ~exists(
                                select(FinalizationJournal.id)
                                .join(JobAttempt, FinalizationJournal.attempt_id == JobAttempt.id)
                                .where(
                                    JobAttempt.job_id == Job.id,
                                    JobAttempt.attempt_number == Job.attempts,
                                )
                            ),
                        )
                        .order_by(Job.id)
                        .limit(QUERY_BATCH)
                    )
                ).all()
            )
            if not missing:
                break
            last_id = missing[-1]
            for job_id in missing:
                job = await lock_job_source(session, job_id)
                if job is None or job.status not in {JobStatus.VERIFYING, JobStatus.FINALIZING}:
                    await session.rollback()
                    continue
                journaled = await session.scalar(
                    select(FinalizationJournal.id)
                    .join(JobAttempt, FinalizationJournal.attempt_id == JobAttempt.id)
                    .where(JobAttempt.job_id == job.id, JobAttempt.attempt_number == job.attempts)
                )
                if journaled is not None:
                    await session.rollback()
                    continue
                attempt = (
                    await session.execute(
                        select(JobAttempt)
                        .where(JobAttempt.job_id == job.id, JobAttempt.attempt_number == job.attempts)
                        .with_for_update(of=JobAttempt)
                    )
                ).scalar_one_or_none()
                error = "Finalization interrupted before journal preparation. Source preserved."
                await finish(session, job.id, JobStatus.FAILED, error=error)
                session.add(
                    JobEvent(
                        job_id=job.id,
                        attempt_id=attempt.id if attempt else None,
                        type="finalization_unprepared",
                        payload={"error": error},
                    )
                )
                failed += 1
                await session.commit()
            await session.commit()
    return failed


async def _recovery_root(session: AsyncSession, journal: FinalizationJournal) -> Path:
    attempt = await session.get(JobAttempt, journal.attempt_id)
    job = await session.get(Job, attempt.job_id) if attempt is not None else None
    library = await session.get(Library, job.library_id) if job is not None else None
    if library is None:
        raise FinalizationError("finalization library unavailable")

    def resolve_root() -> Path:
        source = Path(journal.source_path).resolve()
        roots = [Path(root).resolve() for root in library.roots]
        matching = [root for root in roots if root in source.parents]
        if len(matching) != 1:
            raise FinalizationError("cannot recover finalization")
        return matching[0]

    return await wait_workspace_io(resolve_root)


async def _record_recovery_result(session: AsyncSession, journal: FinalizationJournal) -> None:
    attempt = await session.get(JobAttempt, journal.attempt_id)
    if attempt is None:
        raise FinalizationError("cannot recover finalization")
    job = await session.get(Job, attempt.job_id)
    media = await session.get(MediaFile, job.media_file_id) if job is not None else None
    if job is None or media is None:
        raise FinalizationError("cannot recover finalization")
    if job.status in {JobStatus.VERIFYING, JobStatus.FINALIZING} and attempt.attempt_number == job.attempts:
        now = utcnow()
        attempt.finished_at = now
        job.finished_at = now
        job.warning = None
        if journal.state in {"completed", "discard_completed"}:
            attempt.status = job.status = JobStatus.SUCCEEDED
            attempt.result = "succeeded"
            media.path = journal.destination_path
            output_stat = await wait_workspace_io(lambda: Path(journal.destination_path).stat())
            media.size = attempt.output_size if attempt.output_size is not None else output_stat.st_size
            media.mtime_ns = output_stat.st_mtime_ns
            # Discovery must recognize the recovered output as our publication.
            media.source_identity = source_identity(output_stat)
            media.probe_id = None
            media.health_status = "pending"
            await session.flush()
            await session.execute(delete(ProbeRow).where(ProbeRow.media_file_id == media.id))
            if attempt.output_probe is not None:
                attempt.output_probe = {
                    **attempt.output_probe,
                    "file_path": journal.destination_path,
                    "container": Path(journal.destination_path).suffix.lstrip(".").lower(),
                }
            if job.kind == "transcode":
                media.transcode_status = TranscodeStatus.TRANSCODED.value
                await record_publication_notifications(session, job, attempt.id)
            event_type = "finalization_recovered"
        else:
            attempt.status = job.status = JobStatus.FAILED
            attempt.result = "failed"
            attempt.error = job.error = "cannot recover finalization"
            if job.kind == "transcode":
                await set_transcode_result(session, media.id, TranscodeStatus.FAILED)
            elif job.kind == "mkv_stats":
                media.health_status = "probe_failed"
                media.health_source = None
            media.path = journal.source_path
            event_type = "finalization_rolled_back"
        session.add(JobEvent(job_id=job.id, attempt_id=attempt.id, type=event_type, payload={"state": journal.state}))
