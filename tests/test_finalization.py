from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from common.fs import effects
from common.actions import NotifyExternal
from common.media.probe import ProbeData, ProbeStream
from common.plan.models import FFmpegPlan, StreamPolicy, StreamSelection, SubtitleBurn, VideoTarget
from common.plan.renderer import render
from common.enums import JobStatus, StreamOutcome
from server.settings import ScannerConfig
from server.finalization import (
    FinalizationError,
    _finalize_locked,
    finalize,
    publish_artifacts,
    recover,
    recover_incomplete,
    validate_artifact,
)
from server.fs.source import capture_source
from server.fs.library import LibraryFS
from server.models import (
    FinalizationJournal,
    Job,
    JobAttempt,
    JobEvent,
    Library,
    MediaFile,
    ProbeRow,
    TrashEntry,
    NotificationDelivery,
)
from server.trash import delete_trash_entry, library_mutation_lock, purge_trash, trash_path
from server.workspace import _stage_source, cleanup_workspace
from server.staging import StagingService
from tests.helpers import database_context, fd_path, make_test_temp_dir, path_size
from tests.test_staging import add_job

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
pytestmark = pytest.mark.usefixtures("local_storage")
FILE_TIME_NS = 1_700_000_000_000_000_000


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with database_context("finalization") as factory, factory() as current:
        yield current


async def seed_attempt(
    session: AsyncSession, source: Path, root: Path, identifier: int = 1, *, size: int
) -> JobAttempt:
    library = Library(name="fixture", roots=[str(root)])
    session.add(library)
    await session.flush()
    media = MediaFile(
        library_id=library.id,
        path=str(source),
        size=size,
        mtime_ns=1,
        container="mkv",
        medium="movie",
        last_scanned_at=T0,
    )
    session.add(media)
    await session.flush()
    job = Job(media_file_id=media.id, library_id=library.id, attempts=1, status=JobStatus.FINALIZING)
    session.add(job)
    await session.flush()
    current = JobAttempt(id=identifier, job_id=job.id, attempt_number=1, status=JobStatus.FINALIZING)
    session.add(current)
    await session.commit()
    return current


def input_probe(path: Path, size: int = 4) -> ProbeData:
    return ProbeData(
        file_path=str(path),
        duration_s=10,
        size_bytes=size,
        streams=[ProbeStream(index=0, codec="h264", codec_type="video")],
    )


def raw(duration: str = "10") -> dict:
    return {"format": {"duration": duration}, "streams": [{"index": 0, "codec_name": "h264", "codec_type": "video"}]}


async def probe_raw(_: object, path: Path) -> dict:
    return raw()


def stage_fixture(workspace: Path, job_id: int, source: Path) -> str:
    captured = capture_source(source)
    return _stage_source(workspace, job_id, source, expected_source=captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True], ids=["intermediate", "original"])
async def test_original_publication_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool, session: AsyncSession
) -> None:
    source = tmp_path / "library" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"original")
    workspace = tmp_path / "workspace"
    relative = stage_fixture(workspace, 1, source)
    staged = workspace / relative
    staged.write_bytes(b"intermediate")
    artifact = staged.parent / "output.mkv"
    artifact.write_bytes(b"output")
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    attempt_row = await seed_attempt(session, source, source.parent, size=source.stat().st_size)
    if changed:
        source.write_bytes(b"larger original")
        with pytest.raises(ValueError):
            await _finalize_locked(
                session,
                attempt_row,
                source,
                source,
                workspace,
                "jobs/1/output.mkv",
                None,
                input_probe(source, 20),
                FFmpegPlan(),
                "ffprobe",
                library_id=1,
                library_roots=[str(source.parent)],
                trash_mode="none",
                trash_retention_days=0,
            )
        assert source.read_bytes() == b"larger original" and artifact.exists()
        await session.rollback()
        row = (await session.scalars(select(FinalizationJournal))).one()
        assert row.state == "discard_prepared"
    else:
        await _finalize_locked(
            session,
            attempt_row,
            source,
            source,
            workspace,
            "jobs/1/output.mkv",
            None,
            input_probe(source, 20),
            FFmpegPlan(),
            "ffprobe",
            library_id=1,
            library_roots=[str(source.parent)],
            trash_mode="none",
            trash_retention_days=0,
        )
        row = (await session.scalars(select(FinalizationJournal))).one()
        await session.refresh(row)
        assert source.read_bytes() == b"output" and row.state == "discard_completed"


@pytest.mark.asyncio
async def test_replacement_rechecks_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    source = tmp_path / "library" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"original")
    workspace = tmp_path / "workspace"
    relative = stage_fixture(workspace, 1, source)
    artifact = (workspace / relative).parent / "output.mkv"
    artifact.write_bytes(b"output")

    async def probe(_binary, path):
        if path.suffix == ".part":
            source.write_bytes(b"larger original")
        return raw()

    monkeypatch.setattr("server.finalization.run_ffprobe", probe)
    attempt_row = await seed_attempt(session, source, source.parent, size=source.stat().st_size)
    with pytest.raises(ValueError):
        await _finalize_locked(
            session,
            attempt_row,
            source,
            source,
            workspace,
            "jobs/1/output.mkv",
            None,
            input_probe(source, 20),
            FFmpegPlan(),
            "ffprobe",
            library_id=1,
            library_roots=[str(source.parent)],
            trash_mode="none",
            trash_retention_days=0,
        )
    assert source.read_bytes() == b"larger original" and artifact.exists()
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_prepared"


@pytest.mark.parametrize("cross_device", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_promotion_sync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_device: bool, failure: bool
) -> None:
    from server.finalization import _move_artifact

    workspace, library = tmp_path / "workspace", tmp_path / "library"
    workspace.mkdir()
    library.mkdir()
    source, destination = workspace / "subtitle.srt", library / "movie.eng.srt"
    source.write_bytes(b"subtitle")
    link = os.link
    synced = []

    def cross_link(src: Path, dst: Path, **kwargs) -> None:
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if cross_device and src_path == source and dst_path == destination:
            raise OSError(errno.EXDEV, "cross device")
        link(src, dst, **kwargs)

    def sync(path: Path) -> None:
        assert path == library
        assert source.read_bytes() == destination.read_bytes() == b"subtitle"
        synced.append(path)
        if failure:
            raise OSError(errno.EXDEV, "sync failed")

    monkeypatch.setattr("server.finalization.os.link", cross_link)
    monkeypatch.setattr("server.finalization._fsync_directory", sync)
    if failure:
        with pytest.raises(FinalizationError):
            _move_artifact(source, destination)
    else:
        _move_artifact(source, destination)
    assert synced == [library]
    assert destination.read_bytes() == b"subtitle" and source.exists() == failure


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["subtitle", "last_audio", "real_video"])
async def test_rejects_lost_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str, session: AsyncSession
) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    workspace = tmp_path / "workspace"
    stage_fixture(workspace, 1, source)
    artifact = workspace / "output.mkv"
    artifact.write_bytes(b"output")
    streams = [ProbeStream(index=0, codec="h264", codec_type="video")]
    policy = None
    output = raw()
    if missing == "subtitle":
        streams.append(ProbeStream(index=1, codec="subrip", codec_type="subtitle", language="eng"))
    elif missing == "last_audio":
        streams.append(ProbeStream(index=1, codec="aac", codec_type="audio", language="eng"))
        policy = StreamPolicy(audio=StreamSelection(keep_languages=[]))
    else:
        streams.extend(
            [
                ProbeStream(index=1, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
                ProbeStream(index=2, codec="aac", codec_type="audio", language="eng"),
            ]
        )
        output["streams"][0]["codec_name"] = "mjpeg"
        output["streams"][0]["disposition"] = {"attached_pic": 1}
        output["streams"].append({"index": 1, "codec_name": "aac", "codec_type": "audio"})
    probe = input_probe(source, size=8).model_copy(update={"streams": streams})
    plan = FFmpegPlan(streams=policy)
    assert all(entry.outcome != StreamOutcome.DROP for entry in render(plan, probe, "source", "output").stream_log)

    async def probe_output(_: object, path: Path) -> dict:
        return output

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_output)
    attempt_row = await seed_attempt(session, source, tmp_path, size=source.stat().st_size)
    rejected = False
    try:
        await _finalize_locked(
            session,
            attempt_row,
            source,
            source,
            workspace,
            "output.mkv",
            None,
            probe,
            plan,
            "ffprobe",
            library_id=1,
            library_roots=[str(tmp_path)],
            trash_mode="none",
            trash_retention_days=30,
        )
    except FinalizationError:
        rejected = True
    assert source.read_bytes() == b"original"
    assert rejected


@pytest.mark.parametrize("kind", ["video", "cover_art", "audio", "subtitle", "data", "attachment"])
def test_rejects_retained_counts(tmp_path: Path, kind: str) -> None:
    artifact = tmp_path / "output.mkv"
    artifact.write_bytes(b"output")
    stream = ProbeStream(
        index=0,
        codec="mjpeg" if kind == "cover_art" else "codec",
        codec_type="video" if kind == "cover_art" else kind,
        disposition_attached_pic=kind == "cover_art",
    )
    source = input_probe(tmp_path / "source.mkv", size=8).model_copy(
        update={"streams": [stream, stream.model_copy(update={"index": 1})]}
    )
    output = {
        "format": {"duration": "10"},
        "streams": [
            {
                "index": 0,
                "codec_name": stream.codec,
                "codec_type": stream.codec_type,
                "disposition": {"attached_pic": int(stream.disposition_attached_pic)},
            }
        ],
    }
    with pytest.raises(FinalizationError):
        validate_artifact(output, artifact, source, FFmpegPlan())


@pytest.mark.parametrize("encode", [False, True])
def test_cleanup_duration(tmp_path: Path, encode: bool) -> None:
    artifact = tmp_path / "output.mkv"
    artifact.write_bytes(b"output")
    source = input_probe(tmp_path / "source.mkv", size=8)
    source.duration_s = 3047
    source.streams.append(ProbeStream(index=1, codec="ass", codec_type="subtitle", language="ger"))
    plan = FFmpegPlan(
        streams=StreamPolicy(subtitle=StreamSelection(keep_languages=["eng"])),
        video=VideoTarget(encoder="libx265") if encode else None,
    )
    if encode:
        with pytest.raises(FinalizationError):
            validate_artifact(raw("1391.06"), artifact, source, plan)
    else:
        assert validate_artifact(raw("1391.06"), artifact, source, plan).duration_s == 1391.06


@pytest.mark.parametrize("tagged", [False, True])
def test_rejects_video_substitution(tmp_path: Path, tagged: bool) -> None:
    artifact = tmp_path / "output.mkv"
    artifact.write_bytes(b"output")
    source = input_probe(tmp_path / "source.mkv", size=8).model_copy(
        update={
            "streams": [
                ProbeStream(index=0, codec="h264", codec_type="video"),
                ProbeStream(index=1, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
            ]
        }
    )
    output = {
        "format": {"duration": "10"},
        "streams": [
            {
                "index": n,
                "codec_name": "mjpeg",
                "codec_type": "video",
                "disposition": {"attached_pic": int(not tagged)},
                "tags": {"MIMETYPE": "image/jpeg"} if tagged else {},
            }
            for n in range(2)
        ],
    }
    with pytest.raises(FinalizationError):
        validate_artifact(output, artifact, source, FFmpegPlan())


@pytest.mark.parametrize(
    ("policy", "kept"),
    [
        (None, list(range(10))),
        (StreamPolicy(drop_data=True, drop_cover_art=True), [0, 2, 3, 4, 5, 6, 7, 9]),
        (
            StreamPolicy(
                audio=StreamSelection(keep_languages=["eng"], max_per_language=1),
                subtitle=StreamSelection(keep_languages=["eng"]),
            ),
            [0, 1, 2, 6, 7, 8, 9],
        ),
        (StreamPolicy(audio=StreamSelection(drop_descriptive=True)), [0, 1, 2, 4, 5, 6, 7, 8, 9]),
        (
            StreamPolicy(audio=StreamSelection(keep_languages=["eng"], retag_und_as="eng")),
            [0, 1, 2, 3, 5, 6, 7, 8, 9],
        ),
        (StreamPolicy(audio=StreamSelection(keep_languages=[])), [0, 1, 2, 6, 7, 8, 9]),
        (StreamPolicy(audio=StreamSelection(keep_languages=[], keep_all_if_empty=True)), list(range(10))),
        (StreamPolicy(subtitle=StreamSelection(keep_languages=[])), [0, 1, 2, 3, 4, 5, 7, 8, 9]),
        (
            StreamPolicy(subtitle=StreamSelection(keep_languages=[], keep_forced=False)),
            [0, 1, 2, 3, 4, 5, 8, 9],
        ),
    ],
)
@pytest.mark.parametrize("burn", [False, True])
@pytest.mark.parametrize("container", ["mkv", "mp4", "original"])
def test_validates_selected_streams(
    tmp_path: Path, policy: StreamPolicy | None, kept: list[int], burn: bool, container: str
) -> None:
    artifact = tmp_path / "output.part"
    artifact.write_bytes(b"output")
    source = input_probe(tmp_path / "source.mp4", size=8).model_copy(
        update={
            "container": "mp4",
            "streams": [
                ProbeStream(index=0, codec="h264", codec_type="video"),
                ProbeStream(index=1, codec="mjpeg", codec_type="video", disposition_attached_pic=True),
                ProbeStream(index=2, codec="aac", codec_type="audio", language="eng"),
                ProbeStream(index=3, codec="aac", codec_type="audio", language="eng", is_descriptive=True),
                ProbeStream(index=4, codec="aac", codec_type="audio", language="jpn"),
                ProbeStream(index=5, codec="aac", codec_type="audio"),
                ProbeStream(
                    index=6, codec="mov_text" if container == "mkv" else "subrip", codec_type="subtitle", language="eng"
                ),
                ProbeStream(index=7, codec="ass", codec_type="subtitle", language="jpn", disposition_forced=True),
                ProbeStream(index=8, codec="bin_data", codec_type="data"),
                ProbeStream(index=9, codec="ttf", codec_type="attachment"),
            ],
        }
    )
    plan = FFmpegPlan(
        container=container,
        streams=policy,
        video=VideoTarget(encoder="libx265") if burn else None,
        burn_subtitle=SubtitleBurn(filename="captions.srt") if burn else None,
    )
    output = {
        "format": {"duration": "10"},
        "streams": [
            {
                "index": n,
                "codec_name": "subrip"
                if stream.index == 6 and container == "mkv"
                else "mov_text"
                if stream.index == 6
                else stream.codec,
                "codec_type": stream.codec_type,
                "disposition": {"attached_pic": int(stream.disposition_attached_pic and container != "mkv")},
                "tags": {"MIMETYPE": "image/jpeg"} if stream.disposition_attached_pic and container == "mkv" else {},
            }
            for n, stream in enumerate(stream for stream in source.streams if stream.index in kept)
        ],
    }
    assert len(validate_artifact(output, artifact, source, plan).streams) == len(kept)
    # Every selected track matters even when another track of the same type remains.
    for n in range(len(output["streams"])):
        incomplete = {**output, "streams": output["streams"][:n] + output["streams"][n + 1 :]}
        with pytest.raises(FinalizationError):
            validate_artifact(incomplete, artifact, source, plan)


async def run_finalize(
    tmp_path: Path,
    session: AsyncSession,
    attempt_id: int,
    source: Path,
    destination: Path,
    workspace: Path,
    artifact: str,
    trash: Path,
    *,
    preserve_timestamps: bool = False,
    trash_mode: str = "none",
    required_artifacts: list[tuple[str, Path]] | None = None,
) -> tuple[ProbeData, Path]:
    attempt_row = await seed_attempt(session, source, tmp_path, attempt_id, size=source.stat().st_size)
    if not (workspace / "jobs" / str(attempt_row.job_id) / "manifest.json").exists():
        stage_fixture(workspace, attempt_row.job_id, source)
    async with library_mutation_lock(tmp_path, wait=True, shared=True):
        return await _finalize_locked(
            session,
            attempt_row,
            source,
            destination,
            workspace,
            artifact,
            trash,
            input_probe(source),
            FFmpegPlan(),
            "ffprobe",
            preserve_timestamps,
            library_id=1,
            library_roots=[str(tmp_path)],
            trash_mode=trash_mode,
            trash_retention_days=30,
            required_artifacts=required_artifacts,
        )


async def publication_db(tmp_path: Path, factory, *, retain: bool = False):
    engine = factory.kw["bind"]
    source = tmp_path / "source.mkv"
    source.write_bytes(b"old")
    os.utime(source, ns=(FILE_TIME_NS, FILE_TIME_NS))
    workspace = tmp_path / "workspace"
    trash = tmp_path / "trash"
    trash.mkdir()
    async with factory() as session:
        library = Library(name="publication", roots=[str(tmp_path)], trash_mode="all" if retain else "none")
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(source),
            size=3,
            mtime_ns=source.stat().st_mtime_ns,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(media_file_id=media.id, library_id=library.id, attempts=1, status=JobStatus.FINALIZING)
        session.add(job)
        await session.flush()
        relative = f"jobs/{job.id}/attempts/1/output.mkv"
        attempt_row = JobAttempt(job_id=job.id, attempt_number=1, status=JobStatus.FINALIZING, output_path=relative)
        session.add(attempt_row)
        await session.flush()
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt_row.id,
                type="stage",
                payload={
                    "number": 1,
                    "plan": FFmpegPlan().model_dump(mode="json"),
                    "probe": input_probe(source).model_dump(mode="json"),
                    "output_path": relative,
                    "result": "succeeded",
                    "action_results": [],
                },
            )
        )
        await session.commit()
        identifier = attempt_row.id
        stage_fixture(workspace, job.id, source)
        artifact = workspace / relative
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"done")
    return engine, factory, identifier, workspace, trash, source


async def publication_pair(
    tmp_path: Path, factory, *, competing: bool
) -> tuple[list[tuple[int, Path, Path, Path]], Path, Path]:
    root, workspace, trash = (tmp_path / name for name in ("library", "workspace", "trash"))
    root.mkdir()
    trash.mkdir()
    records = []
    async with factory() as session:
        library = Library(name="concurrent", roots=[str(root)], trash_mode="all")
        session.add(library)
        await session.flush()
        for index, name in enumerate(("movie.mkv", "movie.avi") if competing else ("first.mkv", "second.mkv")):
            source = root / name
            source.write_bytes(f"old{index}".encode())
            media = MediaFile(
                library_id=library.id,
                path=str(source),
                size=4,
                mtime_ns=source.stat().st_mtime_ns,
                container=source.suffix[1:],
                medium="movie",
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            job = Job(media_file_id=media.id, library_id=library.id, attempts=1, status="finalizing")
            session.add(job)
            await session.flush()
            relative = f"jobs/{job.id}/attempts/1/output.mp4"
            attempt = JobAttempt(job_id=job.id, attempt_number=1, status="finalizing", output_path=relative)
            session.add(attempt)
            await session.flush()
            session.add(
                JobEvent(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    type="stage",
                    payload={
                        "number": 1,
                        "plan": FFmpegPlan(container="mp4").model_dump(mode="json"),
                        "probe": input_probe(source).model_dump(mode="json"),
                        "output_path": relative,
                        "result": "succeeded",
                        "action_results": [],
                    },
                )
            )
            stage_fixture(workspace, job.id, source)
            artifact = workspace / relative
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(f"new{index}".encode())
            records.append((attempt.id, source, artifact, source.with_suffix(".mp4")))
        await session.commit()
    return records, workspace, trash


@pytest.mark.asyncio
async def test_independent_finalizers_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_factory
) -> None:
    records, workspace, trash = await publication_pair(tmp_path, database_factory, competing=False)
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def probe(_binary, path: Path) -> dict:
        for index, (_, _, artifact, _) in enumerate(records):
            if path == artifact:
                entered[index].set()
                await release.wait()
        return raw()

    monkeypatch.setattr("server.finalization.run_ffprobe", probe)
    tasks = [
        asyncio.create_task(
            finalize(database_factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        )
        for identifier, *_ in records
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in entered)), 3)
        assert all(not task.done() for task in tasks)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert all(result.committed and not result.pending for result in results)
        async with database_factory() as session:
            entries = list(await session.scalars(select(TrashEntry).order_by(TrashEntry.original_path)))
        assert len(entries) == 2
        for index, (_, source, _, destination) in enumerate(records):
            assert destination.read_bytes() == f"new{index}".encode() and not source.exists()
            entry = next(entry for entry in entries if entry.original_path == str(source))
            assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == f"old{index}".encode()
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 7)


@pytest.mark.asyncio
async def test_competing_publication_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_factory
) -> None:
    records, workspace, trash = await publication_pair(tmp_path, database_factory, competing=True)
    entered, competing = asyncio.Event(), asyncio.Event()
    releases = [asyncio.Event(), asyncio.Event()]

    async def probe(_binary, path: Path) -> dict:
        for index, (_, _, artifact, _) in enumerate(records):
            if path == artifact:
                if index == 0:
                    entered.set()
                await releases[index].wait()
        return raw()

    async def publish_second():
        competing.set()
        return await finalize(
            database_factory, records[1][0], workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe"
        )

    monkeypatch.setattr("server.finalization.run_ffprobe", probe)
    first = asyncio.create_task(
        finalize(database_factory, records[0][0], workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
    )
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(publish_second())
        await asyncio.wait_for(competing.wait(), 2)
        releases[0].set()
        assert (await asyncio.wait_for(first, 5)).committed
        releases[1].set()
        with pytest.raises(FileExistsError):
            await asyncio.wait_for(second, 5)
        destination = records[0][3]
        assert destination == records[1][3] and destination.read_bytes() == b"new0"
        assert records[1][1].read_bytes() == b"old1"
        async with database_factory() as session:
            entry = (await session.scalars(select(TrashEntry))).one()
            assert entry.original_path == str(records[0][1])
            assert trash_path(trash, entry.library_id, entry.id, entry.suffix).read_bytes() == b"old0"
    finally:
        for release in releases:
            release.set()
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_publication_complete(tmp_path: Path, monkeypatch, retain: bool, database_factory) -> None:
    engine, factory, identifier, workspace, trash, source = await publication_db(
        tmp_path, database_factory, retain=retain
    )
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    try:
        result = await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        assert result.committed and not result.pending and result.warning is None
        assert result.probe is not None and result.probe.file_path == str(source)
        assert source.read_bytes() == b"done" and not (tmp_path / f".{identifier}.part").exists()
        async with factory() as session:
            row = (await session.scalars(select(FinalizationJournal))).one()
            assert row.state == ("completed" if retain else "discard_completed")
            assert Path(row.trash_path).exists() == retain
            if retain:
                assert Path(row.trash_path).read_bytes() == b"old"
        # Finalization removes temporary copies before the hub records completion.
        assert await recover_incomplete(factory) == (1, 0)
        async with factory() as session:
            current = (await session.scalars(select(Job))).one()
            assert current.status == JobStatus.SUCCEEDED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subtitle_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_factory) -> None:
    engine, factory, identifier, workspace, trash, source = await publication_db(tmp_path, database_factory)

    async def converted_probe(_binary, _path):
        payload = raw()
        payload["streams"].append({"index": 1, "codec_type": "subtitle", "codec_name": "subrip"})
        return payload

    monkeypatch.setattr("server.finalization.run_ffprobe", converted_probe)
    try:
        async with factory() as session:
            library = (await session.scalars(select(Library))).one()
            library.trash_mode = "transcode"
            stage = (await session.scalars(select(JobEvent).where(JobEvent.type == "stage"))).one()
            probe = input_probe(source).model_copy(update={"container": "mkv"})
            probe.streams.append(ProbeStream(index=1, codec="", codec_type="subtitle"))
            stage.payload = {**stage.payload, "probe": probe.model_dump(mode="json")}
            await session.commit()
        result = await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        assert result.committed and source.read_bytes() == b"done"
        async with factory() as session:
            journal = (await session.scalars(select(FinalizationJournal))).one()
            assert journal.state == "completed" and Path(journal.trash_path).read_bytes() == b"old"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("retain", "defer_stats"), [(False, False), (True, False), (False, True)])
async def test_refresh_without_workspace(
    tmp_path: Path, monkeypatch, retain: bool, defer_stats: bool, database_factory
) -> None:
    from server.kv import KeyValueStore

    engine, factory, identifier, workspace, trash, source = await publication_db(
        tmp_path, database_factory, retain=retain
    )
    dates = {"atime_ns": FILE_TIME_NS, "mtime_ns": FILE_TIME_NS}
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)

    async def probe_published(_binary, path, **_kwargs):
        return await probe_raw(_binary, path)

    monkeypatch.setattr("server.fs.library.run_ffprobe", probe_published)
    try:
        result = await finalize(
            factory,
            identifier,
            workspace_dir=workspace,
            trash_root=trash,
            ffprobe_bin="ffprobe",
            preserve_timestamps=True,
        )
        assert result.committed
        assert await recover_incomplete(factory) == (1, 0)
        async with factory() as session:
            event = await session.scalar(select(JobEvent).where(JobEvent.type == "publication"))
            job = await session.scalar(select(Job))
            assert event is not None and event.payload["dates"] == dates
            assert job is not None
            job_id, library_id = job.id, job.library_id
            library = await session.get(Library, library_id)
            assert library is not None
            library.healthcheck_enabled = False
            library.mkv_stats = defer_stats
            await session.commit()
        if defer_stats:
            await KeyValueStore(factory).put("operational_settings", {"mkv_stats_location": "node"})
        assert (await StagingService(factory, workspace, min_free_gb=1).prune_job(job_id))["outcome"] == "removed"
        assert not (workspace / "jobs" / str(job_id)).exists()
        refreshed = await LibraryFS(
            library_id, sessionmaker=factory, ffprobe_bin="ffprobe", mkvpropedit_bin="unused", workspace_dir=workspace
        ).refresh_published(identifier)
        assert refreshed.media.path == str(source)
        assert source.stat().st_mtime_ns == dates["mtime_ns"]
        if defer_stats:
            assert refreshed.media.probe_id is None
            async with factory() as session:
                statistics = await session.scalar(select(Job).where(Job.kind == "mkv_stats"))
                assert statistics is not None and statistics.status in {"queued", "hold"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_rollback(tmp_path: Path, monkeypatch, database_factory) -> None:
    engine, factory, identifier, workspace, trash, source = await publication_db(tmp_path, database_factory)

    async def invalid_partial(_binary, path):
        return raw("1" if path.name.endswith(".part") else "10")

    monkeypatch.setattr("server.finalization.run_ffprobe", invalid_partial)
    try:
        with pytest.raises(FinalizationError):
            await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        assert source.read_bytes() == b"old" and not (tmp_path / f".{identifier}.part").exists()
        async with factory() as session:
            row = (await session.scalars(select(FinalizationJournal))).one()
            assert row.state == "discard_rolled_back"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_cleanup_warning(tmp_path: Path, monkeypatch, database_factory) -> None:
    engine, factory, identifier, workspace, trash, source = await publication_db(
        tmp_path, database_factory, retain=True
    )
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    unlink = Path.unlink
    copy = tmp_path / f".{identifier}.part"

    def fail_cleanup(path, *args, **kwargs):
        if path == copy:
            raise OSError("copy cleanup failed")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    try:
        result = await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        assert result.committed and result.pending and result.warning is not None
        assert source.read_bytes() == b"done" and copy.read_bytes() == b"done"
        async with factory() as session:
            row = (await session.scalars(select(FinalizationJournal))).one()
            assert row.state == "completed" and Path(row.trash_path).read_bytes() == b"old"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["stage", "attempt", "artifact"])
async def test_refuses_changed_attempt(tmp_path: Path, monkeypatch, changed: str, database_factory) -> None:
    engine, factory, identifier, workspace, trash, source = await publication_db(tmp_path, database_factory)
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    try:
        async with factory() as session:
            current = (await session.scalars(select(Job))).one()
            attempt_row = await session.get(JobAttempt, identifier)
            stage = (await session.scalars(select(JobEvent).where(JobEvent.type == "stage"))).one()
            if changed == "attempt":
                current.attempts += 1
            elif changed == "stage":
                stage.payload = {**stage.payload, "result": "failed"}
            else:
                relative = "jobs/999/attempts/1/output.mkv"
                artifact = workspace / relative
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"foreign output")
                attempt_row.output_path = relative
                stage.payload = {**stage.payload, "output_path": relative}
            await session.commit()
        with pytest.raises(FinalizationError):
            await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        assert source.read_bytes() == b"old"
        assert (workspace / "jobs/1/attempts/1/output.mkv").read_bytes() == b"done"
        async with factory() as session:
            assert not (await session.scalars(select(FinalizationJournal))).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_artifact_failure_isolated(tmp_path: Path, monkeypatch, database_factory) -> None:
    from common.actions import ActionOutcome

    engine, factory, identifier, workspace, trash, source = await publication_db(tmp_path, database_factory)
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    try:
        await finalize(factory, identifier, workspace_dir=workspace, trash_root=trash, ffprobe_bin="ffprobe")
        async with factory() as session:
            current = (await session.scalars(select(Job))).one()
            attempt_row = await session.get(JobAttempt, identifier)
            current.status = attempt_row.status = JobStatus.SUCCEEDED
            await session.commit()
        sidecars = [workspace / f"jobs/1/attempts/1/output.{language}.srt" for language in ("eng", "jpn")]
        for sidecar in sidecars:
            sidecar.write_bytes(b"subtitle")
        existing_file = source.with_name("source.eng.srt")
        existing_file.write_bytes(b"old-bytes")
        outcomes = await publish_artifacts(
            factory,
            identifier,
            [
                ActionOutcome(kind="extract_subtitles", ok=True, artifacts=[str(sidecar.relative_to(workspace))])
                for sidecar in sidecars
            ],
            workspace_dir=workspace,
        )
        assert not outcomes[0].ok and outcomes[0].artifacts == []
        assert existing_file.read_bytes() == b"old-bytes" and sidecars[0].read_bytes() == b"subtitle"
        destination = source.with_name("source.jpn.srt")
        assert outcomes[1].ok and outcomes[1].artifacts == [str(destination)]
        assert destination.read_bytes() == b"subtitle" and not sidecars[1].exists()
    finally:
        await engine.dispose()


def journal(source: Path, partial: Path, destination: Path, trash: Path, state: str) -> FinalizationJournal:
    return FinalizationJournal(
        attempt_id=1,
        source_path=str(source),
        destination_path=str(destination),
        partial_path=str(partial),
        trash_path=str(trash),
        state=state,
    )


async def prepare_recovery(
    session: AsyncSession,
    row: FinalizationJournal,
    source: dict,
    output: dict | None = None,
) -> None:
    root = Path(row.source_path).parent
    attempt_row = await seed_attempt(session, Path(row.source_path), root, row.attempt_id, size=source["size"])
    attempt_row.source_size = source["size"]
    attempt_row.output_size = output["size"] if output else 0
    session.add(row)
    session.add(
        JobEvent(
            job_id=1,
            attempt_id=row.attempt_id,
            type="publication",
            payload={
                "library_id": 1,
                "roots": [str(root)],
                "preserve_timestamps": False,
            },
        )
    )
    await session.commit()


async def recovery_db(
    tmp_path: Path, factory, state: str, status: str = JobStatus.FINALIZING
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession], int]:
    engine = factory.kw["bind"]
    source, destination, partial, trash = (
        tmp_path / name for name in ("source.mkv", "destination", "partial", "trash")
    )
    source.write_bytes(b"source")
    partial.write_bytes(b"output")
    trash = (
        tmp_path / ".recovery-original"
        if state == "completed"
        else trash_path(tmp_path / "trash-root", 1, "0123456789abcdef", ".mkv")
    )
    trash.parent.mkdir(parents=True, exist_ok=True)
    source.rename(trash)
    if state == "completed":
        os.link(partial, destination)
    else:
        partial.unlink()
    async with factory() as session:
        library = Library(name="library", roots=[str(tmp_path)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(source),
            size=6,
            mtime_ns=1,
            container="mkv",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        job = Job(media_file_id=media.id, library_id=library.id, status=status, attempts=1)
        session.add(job)
        await session.flush()
        attempt_row = JobAttempt(job_id=job.id, attempt_number=1, status="running", source_size=6, output_size=6)
        session.add(attempt_row)
        await session.flush()
        if state != "completed":
            session.add(
                TrashEntry(
                    id="0123456789abcdef",
                    library_id=1,
                    original_path=str(source),
                    library_root=str(tmp_path),
                    suffix=".mkv",
                    size_bytes=6,
                    mtime_ns=1,
                    trashed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    purge_at=datetime(2026, 2, 1, tzinfo=UTC),
                )
            )
            await session.flush()
        session.add(
            FinalizationJournal(
                attempt_id=attempt_row.id,
                source_path=str(source),
                destination_path=str(destination),
                partial_path=str(partial),
                trash_path=str(trash),
                state="original_moved",
            )
        )
        session.add(
            JobEvent(
                job_id=job.id,
                attempt_id=attempt_row.id,
                type="publication",
                payload={
                    "library_id": library.id,
                    "roots": library.roots,
                    "preserve_timestamps": False,
                },
            )
        )
        await session.commit()
        journal_id = (await session.execute(select(FinalizationJournal.id))).scalar_one()
    return engine, factory, journal_id


@pytest.mark.asyncio
async def test_recovery_completed(tmp_path: Path, database_factory) -> None:
    engine, factory, journal_id = await recovery_db(tmp_path, database_factory, "completed")
    async with factory() as session:
        journal = await session.get(FinalizationJournal, journal_id)
        assert journal is not None
        attempt = await session.get(JobAttempt, journal.attempt_id)
        assert attempt is not None
        action = NotifyExternal(application="sonarr", url="https://sonarr.test", api_key="private-api-key")
        session.add(
            JobEvent(
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                type="stage",
                payload={"publication_actions": [action.model_dump(mode="json")] * 2},
            )
        )
        await session.commit()
    assert await recover_incomplete(factory) == (1, 0)
    assert await recover_incomplete(factory) == (0, 0)
    async with factory() as session:
        journal_row = await session.get(FinalizationJournal, journal_id)
        assert journal_row is not None
        attempt_row = await session.get(JobAttempt, journal_row.attempt_id)
        assert attempt_row is not None
        job = await session.get(Job, attempt_row.job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        event = (await session.execute(select(JobEvent).where(JobEvent.type == "finalization_recovered"))).scalar_one()
        assert journal_row.state == "completed" and job.status == attempt_row.status == JobStatus.SUCCEEDED
        assert media.path == journal_row.destination_path and media.transcode_status == "transcoded"
        assert job.finished_at == attempt_row.finished_at and event.type == "finalization_recovered"
        assert str(tmp_path) not in str(event.payload)
        delivery = (await session.scalars(select(NotificationDelivery))).one()
        notification = await session.get(JobEvent, delivery.job_event_id)
        assert notification is not None
        assert notification.payload["path"] == journal_row.destination_path
        assert notification.payload["original_filename"] == Path(journal_row.source_path).name
        assert delivery.status == "pending"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["expiry", "delete"])
async def test_purge_preserves_recovery(tmp_path: Path, operation: str, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "original_moved")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            original = Path(row.trash_path)
            assert not Path(row.source_path).exists()
            assert original.read_bytes() == b"source"
            if operation == "expiry":
                removed = await purge_trash(factory, tmp_path / "trash-root", now=datetime(2026, 3, 1, tzinfo=UTC))
            else:
                entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == "0123456789abcdef"))
                assert entry is not None
                try:
                    await delete_trash_entry(factory, tmp_path / "trash-root", entry.id)
                except ValueError:
                    removed = 0
                else:
                    removed = 1
            await session.commit()
        assert removed == 0, "unfinished finalization still needs its original for rollback"
        assert original.read_bytes() == b"source"
        assert await recover_incomplete(factory) == (1, 0)
        assert (tmp_path / "source.mkv").read_bytes() == b"source"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["expiry", "delete"])
@pytest.mark.parametrize(
    ("state", "active", "protected"),
    [
        ("prepared", False, True),
        ("original_moved", False, True),
        ("artifacts_pending", False, True),
        ("discard_prepared", False, True),
        ("discard_original_moved", False, True),
        ("discard_artifacts_pending", False, True),
        ("unknown", False, True),
        ("completed", True, False),
        ("completed", False, False),
        ("discard_completed", False, False),
        ("rolled_back", False, False),
        ("discard_rolled_back", False, False),
    ],
)
async def test_trash_journal_protection(
    tmp_path: Path, operation: str, state: str, active: bool, protected: bool, database_factory
) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "original_moved")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            row.state = state
            attempt_row = await session.get(JobAttempt, row.attempt_id)
            assert attempt_row is not None
            job = await session.get(Job, attempt_row.job_id)
            assert job is not None
            if not active:
                attempt_row.status = job.status = JobStatus.SUCCEEDED
            await session.commit()
            entry = await session.scalar(select(TrashEntry).where(TrashEntry.id == "0123456789abcdef"))
            assert entry is not None
            media = Path(row.trash_path)
            if operation == "expiry":
                assert await purge_trash(factory, tmp_path / "trash-root", now=datetime(2026, 3, 1, tzinfo=UTC)) == int(
                    not protected
                )
            elif protected:
                with pytest.raises(ValueError):
                    await delete_trash_entry(factory, tmp_path / "trash-root", entry.id)
            else:
                await delete_trash_entry(factory, tmp_path / "trash-root", entry.id)
            await session.commit()
            assert media.exists() == protected
            assert (
                await session.scalar(
                    select(TrashEntry).where(TrashEntry.id == entry.id).execution_options(populate_existing=True)
                )
                is not None
            ) == protected
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_preserves_recovery(tmp_path: Path, database_factory) -> None:
    from server.retention import delete_eligible_job

    engine, factory, identifier = await recovery_db(
        tmp_path, database_factory, "original_moved", status=JobStatus.FAILED
    )
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            original = Path(row.trash_path)
            source = Path(row.source_path)
            attempt_row = await session.get(JobAttempt, row.attempt_id)
            assert attempt_row is not None
            attempt_row.status = JobStatus.FAILED
            attempt_row.result = "failed"
            await session.commit()
            assert not source.exists() and original.read_bytes() == b"source"
            deleted = await delete_eligible_job(session, attempt_row.job_id)
            await session.commit()
            await purge_trash(factory, tmp_path / "trash-root", now=datetime(2026, 3, 1, tzinfo=UTC))
            await session.commit()
            assert original.is_file(), "history deletion exposed the recovery original to retention purge"
            assert not deleted, "deleting history must keep the job needed to recover its original file"
        assert await recover_incomplete(factory) == (1, 0)
        assert source.read_bytes() == b"source"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "discard_completed"])
@pytest.mark.parametrize("container", ["mkv", "mp4"])
async def test_committed_recovery(tmp_path: Path, state: str, container: str, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "completed")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            destination = tmp_path / f"movie.{container}"
            Path(row.destination_path).rename(destination)
            row.destination_path = str(destination)
            row.state = state
            attempt_row = await session.get(JobAttempt, row.attempt_id)
            assert attempt_row is not None
            attempt_row.source_size = 10
            attempt_row.output_size = 6
            attempt_row.output_probe = {"file_path": "artifact", "container": "partial", "size_bytes": 6}
            await session.commit()
        assert await recover_incomplete(factory) == (1, 0)
        async with factory() as session:
            job = (await session.execute(select(Job))).scalar_one()
            media = await session.get(MediaFile, job.media_file_id)
            attempt_row = await session.get(JobAttempt, row.attempt_id)
            assert attempt_row is not None and media is not None
            assert attempt_row.output_probe is not None
            assert job.status == attempt_row.status == JobStatus.SUCCEEDED
            assert media.path == row.destination_path and media.size == 6
            assert attempt_row.source_size == 10 and attempt_row.output_probe["file_path"] == row.destination_path
            assert attempt_row.output_probe["container"] == container
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("suspended", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_recovery_refreshes_probe(
    tmp_path: Path, monkeypatch, suspended: bool, enabled: bool, database_factory
) -> None:
    from server.nodehub.hub import probe_for_file
    from server.probing import ProbeService
    from server.scanner.scan import scan_library
    from server.staging import StagingService

    now = T0 + timedelta(hours=3)
    for module in ("server.scanner.scan", "server.fs.library", "server.queue.store"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: now)
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "completed")
    staging = StagingService(factory, tmp_path / "scan-workspace", min_free_gb=1)
    service = ProbeService(
        factory,
        Path(__file__).parent / "fakes" / "ffprobe.py",
        "unused-mkvpropedit",
        workspace_dir=tmp_path,
    )
    try:
        async with factory() as session:
            journal_row = await session.get(FinalizationJournal, identifier)
            assert journal_row is not None
            destination = tmp_path / "output.mp4"
            Path(journal_row.destination_path).rename(destination)
            journal_row.destination_path = str(destination)
            output = raw()
            output["streams"][0]["codec_name"] = "hevc"
            Path(f"{destination}.probe.json").write_text(json.dumps(output), encoding="utf-8")
            media = (await session.scalars(select(MediaFile))).one()
            media.video_codec = "h264"
            media.health_status = "healthy"
            media.first_seen = T0
            probe = ProbeRow(media_file_id=media.id, raw_json=json.dumps(raw()))
            session.add(probe)
            await session.flush()
            media.probe_id = probe.id
            library = await session.get(Library, media.library_id)
            assert library is not None
            library.suspended = suspended
            library.healthcheck_enabled = enabled
            library.cooldown_s = 7200
            await session.commit()
            media_id, old_probe = media.id, probe.id
        assert await recover_incomplete(factory) == (1, 0)
        async with factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.probe_id is None and media.health_status == "pending"
            assert await session.get(ProbeRow, old_probe) is None
            library = await session.get(Library, media.library_id)
            assert library is not None
            await scan_library(session, library, config=ScannerConfig(), staging=staging)
        await service._fill()
        if suspended:
            assert not service._tasks
            async with factory() as session:
                library = await session.get(Library, library.id)
                assert library is not None
                library.suspended = False
                await session.commit()
            await service._fill()
        assert service._tasks
        await asyncio.wait_for(asyncio.gather(*service._tasks.values()), 3)
        async with factory() as session:
            media = await session.get(MediaFile, media_id)
            assert media is not None and media.video_codec == "hevc" and media.container == "mp4"
            assert media.first_seen.replace(tzinfo=UTC) == T0
            probe = await probe_for_file(session, media)
            assert probe is not None and probe.video[0].codec == "hevc" and probe.file_path == str(destination)
            jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
            assert len(jobs) == 2 + int(enabled) and jobs[0].status == JobStatus.SUCCEEDED.value
            assert jobs[1].kind == "probe" and jobs[1].status == JobStatus.SUCCEEDED.value
            if enabled:
                assert jobs[2].kind == "healthcheck" and jobs[2].status == JobStatus.QUEUED.value
                assert media.health_status == "pending" and media.health_source is None
            else:
                assert media.health_status == "healthy"
                assert media.source_identity is not None
                assert media.health_source == {"path": media.path, **media.source_identity}
            library = await session.get(Library, media.library_id)
            assert library is not None
            report = await scan_library(session, library, config=ScannerConfig(), staging=staging)
            assert report.unchanged == 1 and report.updated == report.pruned == 0
            assert media.video_codec == "hevc" and media.container == "mp4" and media.probe_id is not None
    finally:
        await asyncio.wait_for(service.close(), 10)
        await engine.dispose()


@pytest.mark.asyncio
async def test_unprepared_preserves_source(tmp_path: Path, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "completed")
    workspace = tmp_path / "workspace"
    artifact = workspace / "jobs/1/attempts/1/output.mkv"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"unpublished output")
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            await session.delete(row)
            attempt_row = await session.get(JobAttempt, row.attempt_id)
            assert attempt_row is not None
            attempt_row.output_path = "jobs/1/attempts/1/output.mkv"
            await session.commit()
        assert await recover_incomplete(factory) == (0, 1)
        await cleanup_workspace(factory, workspace)
        async with factory() as session:
            job = (await session.execute(select(Job))).scalar_one()
            assert job.status == JobStatus.FAILED and job.error is not None
            event = (
                await session.execute(select(JobEvent).where(JobEvent.type == "finalization_unprepared"))
            ).scalar_one()
            assert event.attempt_id == attempt_row.id
        assert not artifact.exists() and source.read_bytes() == b"original"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "conflicting"])
@pytest.mark.parametrize("state", ["completed", "discard_completed"])
async def test_completed_blocks_conflicts(tmp_path: Path, failure: str, state: str, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "completed")
    saved = tmp_path / "saved-original"
    saved.write_bytes(b"original")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            row.state, row.trash_path = state, str(saved)
            destination = Path(row.destination_path)
            if failure == "missing":
                destination.unlink()
            elif failure == "conflicting":
                replacement = tmp_path / "existing"
                replacement.write_bytes(b"conflicting output size")
                replacement.replace(destination)
            await session.commit()
        assert await recover_incomplete(factory) == (0, 1)
        async with factory() as session:
            job = (await session.execute(select(Job))).scalar_one()
            assert job.status == JobStatus.FINALIZING
        assert saved.read_bytes() == b"original"
        assert Path(row.partial_path).read_bytes() == b"output"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_rollback(tmp_path: Path, database_factory) -> None:
    engine, factory, journal_id = await recovery_db(tmp_path, database_factory, "rolled_back")
    assert await recover_incomplete(factory) == (1, 0)
    async with factory() as session:
        row = await session.get(FinalizationJournal, journal_id)
        assert row is not None
        attempt_row = await session.get(JobAttempt, row.attempt_id)
        assert attempt_row is not None
        job = await session.get(Job, attempt_row.job_id)
        assert job is not None
        media = await session.get(MediaFile, job.media_file_id)
        assert media is not None
        assert row.state == "rolled_back" and job.status == attempt_row.status == JobStatus.FAILED
        assert media.path == row.source_path and Path(row.source_path).read_bytes() == b"source"
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_deletes_trash(tmp_path: Path, database_factory) -> None:
    engine, factory, _ = await recovery_db(tmp_path, database_factory, "rolled_back")
    other = trash_path(tmp_path / "trash-root", 1, "fedcba9876543210", ".mkv")
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    async with factory() as session:
        session.add(
            TrashEntry(
                id="fedcba9876543210",
                library_id=1,
                original_path=str(tmp_path / "source.mkv"),
                library_root=str(tmp_path),
                suffix=".mkv",
                size_bytes=5,
                mtime_ns=1,
                trashed_at=datetime(2026, 1, 1, tzinfo=UTC),
                purge_at=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    assert await recover_incomplete(factory) == (1, 0)
    async with factory() as session:
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == "0123456789abcdef")) is None
        assert await session.scalar(select(TrashEntry).where(TrashEntry.id == "fedcba9876543210")) is not None
    assert other.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_job_unchanged(tmp_path: Path, database_factory) -> None:
    engine, factory, journal_id = await recovery_db(tmp_path, database_factory, "rolled_back", JobStatus.SUCCEEDED)
    assert await recover_incomplete(factory) == (1, 0)
    async with factory() as session:
        row = await session.get(FinalizationJournal, journal_id)
        assert row is not None
        attempt_row = await session.get(JobAttempt, row.attempt_id)
        assert attempt_row is not None
        job = await session.get(Job, attempt_row.job_id)
        assert job is not None
        assert job.status == JobStatus.SUCCEEDED
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_logs_failure(caplog: pytest.LogCaptureFixture, database_factory) -> None:
    root = make_test_temp_dir("finalization-recovery-logging")
    engine, factory, journal_id = await recovery_db(root, database_factory, "failed")
    async with factory() as session:
        row = await session.get(FinalizationJournal, journal_id)
        assert row is not None
        attempt_id = row.attempt_id
        Path(row.trash_path).unlink()

    caplog.set_level(logging.ERROR, logger="server.finalization")
    assert await recover_incomplete(factory) == (0, 1)
    record = caplog.records[-1]
    assert record.__dict__["journal_id"] == journal_id and record.__dict__["attempt_id"] == attempt_id
    assert record.exc_info is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr("server.finalization.QUERY_BATCH", 2)
    async with database_context("journal-batches") as factory:
        job_ids = []
        for index in range(5):
            source = tmp_path / f"source-{index}.mkv"
            source.write_bytes(b"original")
            job_id = await add_job(factory, source, status="finalizing")
            job_ids.append(job_id)
            async with factory() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                job.attempts = 1
                attempt = JobAttempt(
                    job_id=job_id,
                    attempt_number=1,
                    status="finalizing",
                    source_size=None if index == 0 else 8,
                    output_size=6,
                )
                session.add(attempt)
                await session.flush()
                session.add_all(
                    [
                        FinalizationJournal(
                            attempt_id=attempt.id,
                            source_path=str(source),
                            destination_path=str(tmp_path / f"destination-{index}.mkv"),
                            partial_path=str(tmp_path / f"partial-{index}"),
                            trash_path=str(tmp_path / f"discard-{index}"),
                            state="discard_prepared",
                        ),
                        JobEvent(
                            job_id=job_id,
                            attempt_id=attempt.id,
                            type="publication",
                            payload={"library_id": job.library_id, "roots": [str(tmp_path)]},
                        ),
                    ]
                )
                await session.commit()
        caplog.set_level(logging.ERROR, logger="server.finalization")
        assert await asyncio.wait_for(recover_incomplete(factory), 3) == (4, 1)
        failures = [record for record in caplog.records if record.name == "server.finalization"]
        assert len(failures) == 1
        async with factory() as session:
            jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
            assert [job.status for job in jobs] == ["finalizing", *(["failed"] * 4)]
            journals = (await session.scalars(select(FinalizationJournal).order_by(FinalizationJournal.id))).all()
            assert [row.state for row in journals] == ["discard_prepared", *(["discard_rolled_back"] * 4)]
            events = (
                await session.scalars(
                    select(JobEvent).where(JobEvent.type == "finalization_rolled_back").order_by(JobEvent.job_id)
                )
            ).all()
            assert [event.job_id for event in events] == job_ids[1:]
        assert all((tmp_path / f"source-{index}.mkv").read_bytes() == b"original" for index in range(5))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_unprepared_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool) -> None:
    from server.queue.store import finish

    monkeypatch.setattr("server.finalization.QUERY_BATCH", 2)
    async with database_context("unprepared-batches") as factory:
        job_ids = []
        for index in range(5):
            path = tmp_path / f"source-{index}.mkv"
            path.write_bytes(b"source")
            job_ids.append(await add_job(factory, path, status="verifying" if index % 2 else "finalizing"))
        async with factory() as session:
            await session.execute(update(Job).values(attempts=1))
            session.add_all(
                [JobAttempt(job_id=job_id, attempt_number=1, status="finalizing") for job_id in job_ids[1:]]
            )
            await session.commit()
        commit = AsyncSession.commit
        checkpoints = []

        async def check_commit(session):
            await commit(session)
            async with factory() as writer:
                connection = await writer.connection()
                await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                await writer.execute(update(Library).values(updated_at=Library.updated_at))
                completed = list(
                    await writer.scalars(
                        select(JobEvent.job_id)
                        .where(JobEvent.type == "finalization_unprepared")
                        .order_by(JobEvent.job_id)
                    )
                )
                if not checkpoints or checkpoints[-1] != completed:
                    checkpoints.append(completed)
                await commit(writer)

        async def cancel_finish(session, job_id, *args, **kwargs):
            result = await finish(session, job_id, *args, **kwargs)
            if job_id == job_ids[2]:
                raise asyncio.CancelledError
            return result

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", check_commit)
            if cancelled:
                patch.setattr("server.finalization.finish", cancel_finish)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(recover_incomplete(factory), 3)
                assert checkpoints == [job_ids[:1], job_ids[:2]]
            else:
                assert await asyncio.wait_for(recover_incomplete(factory), 3) == (0, 5)
                assert checkpoints == [job_ids[:index] for index in range(1, 6)]
        if cancelled:
            async with factory() as session:
                rows = (await session.scalars(select(Job).order_by(Job.id))).all()
                assert [job.status for job in rows] == ["failed", "failed", "finalizing", "verifying", "finalizing"]
                pending = (
                    await session.scalars(
                        select(JobEvent.job_id)
                        .where(JobEvent.type == "finalization_unprepared")
                        .order_by(JobEvent.job_id)
                    )
                ).all()
                assert list(pending) == job_ids[:2]
            assert await asyncio.wait_for(recover_incomplete(factory), 3) == (0, 3)
        async with factory() as session:
            jobs = (await session.scalars(select(Job).order_by(Job.id))).all()
            assert all(job.status == "failed" and job.error is not None for job in jobs)
            attempts = (await session.scalars(select(JobAttempt).order_by(JobAttempt.id))).all()
            assert all(attempt.status == attempt.result == "failed" for attempt in attempts)
            events = (
                await session.scalars(
                    select(JobEvent).where(JobEvent.type == "finalization_unprepared").order_by(JobEvent.job_id)
                )
            ).all()
            assert [event.job_id for event in events] == job_ids
            assert events[0].attempt_id is None
            assert [event.attempt_id for event in events[1:]] == [attempt.id for attempt in attempts]
        assert all((tmp_path / f"source-{index}.mkv").read_bytes() == b"source" for index in range(5))


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["mkv", "mp4"])
async def test_finalize_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: str, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "out.bin"
    artifact.write_bytes(b"done")
    source = tmp_path / "source.mkv"
    source.write_bytes(b"old")
    mtime = 1_600_000_000_000_000_000
    os.utime(source, ns=(mtime, mtime))
    destination = tmp_path / "library" / f"movie.{container}"
    trash = tmp_path / "trash"
    trash.mkdir()
    paths: list[Path] = []
    original_probe = raw

    async def fake_probe(_: object, path: Path) -> dict:
        paths.append(path)
        return original_probe()

    original_replace = os.replace
    moves: list[tuple[Path, Path]] = []

    def track_replace(
        source_path: str | Path,
        destination_path: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        moves.append((fd_path(source_path, src_dir_fd), fd_path(destination_path, dst_dir_fd)))
        original_replace(source_path, destination_path, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr("server.finalization.run_ffprobe", fake_probe)
    monkeypatch.setattr("server.finalization.os.replace", track_replace)
    result, moved = await run_finalize(
        tmp_path,
        session,
        7,
        source,
        destination,
        workspace,
        "out.bin",
        trash,
        preserve_timestamps=True,
        trash_mode="all",
    )

    assert [path.name for path in paths] == ["out.bin", paths[1].name]
    assert paths[1] == destination.with_name(".7.part")
    assert destination.read_bytes() == b"done" and not source.exists() and moved.exists()
    assert destination.stat().st_mtime_ns == mtime
    assert result.file_path == str(destination)
    assert result.container == container
    await session.rollback()
    attempt_row = await session.get(JobAttempt, 7)
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert attempt_row is not None and attempt_row.output_probe == result.model_dump(mode="json")
    assert row.state == "completed"
    entry = (await session.scalars(select(TrashEntry))).one()
    assert entry.original_path == str(source) and entry.size_bytes == 3
    assert moves[-1][1] == moved.parent


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["media", "sidecar"])
@pytest.mark.parametrize("failure", [False, True])
async def test_cancelled_copy_locks(
    tmp_path: Path, monkeypatch, phase: str, failure: bool, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "out"
    artifact.write_bytes(b"done")
    sidecar = workspace / "subtitle"
    sidecar.write_bytes(b"subtitle")
    source, destination = tmp_path / "source.mkv", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    blocked = artifact if phase == "media" else sidecar
    started, completed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_copy = effects.copy_file

    def copy(src, dst, **kwargs):
        if src != blocked:
            return original_copy(src, dst, **kwargs)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            if failure:
                raise OSError("copy failed")
            return original_copy(src, dst, **kwargs)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(effects, "copy_file", copy)
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    task = asyncio.create_task(
        run_finalize(
            tmp_path,
            session,
            7,
            source,
            destination,
            workspace,
            "out",
            tmp_path / "trash",
            required_artifacts=[("subtitle", tmp_path / "movie.eng.srt")] if phase == "sidecar" else None,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done() and not completed.is_set()
            with pytest.raises(ValueError):
                async with library_mutation_lock(tmp_path, wait=False):
                    pytest.fail("copy worker released library locks")
        assert source.read_bytes() == b"old" and not destination.exists()
        assert not (tmp_path / "movie.eng.srt").exists()
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)

    assert isinstance(outcomes[0], OSError if failure else asyncio.CancelledError)
    assert source.read_bytes() == b"old" and artifact.read_bytes() == b"done"
    assert not destination.exists() and not (tmp_path / ".7.discard").exists()
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == ("discard_prepared" if phase == "media" else "discard_artifacts_pending")
    if phase == "sidecar":
        event = await session.scalar(select(JobEvent).where(JobEvent.type == "required_artifacts"))
        assert event is not None and event.payload["entries"][0]["size"] is None
        assert (tmp_path / ".7.part").read_bytes() == b"done"
        if failure:
            with pytest.raises(FinalizationError):
                await recover(session, row, library_root=tmp_path)
            return
        assert (tmp_path / ".7-0.part").read_bytes() == b"subtitle"
        with pytest.raises(FinalizationError):
            await recover(session, row, library_root=tmp_path)
        assert source.read_bytes() == b"old" and not destination.exists()
        return
    elif not failure:
        assert (tmp_path / ".7.part").read_bytes() == b"done"
    await recover(session, row, library_root=tmp_path)
    await session.refresh(row)
    assert row.state == "discard_rolled_back"
    assert source.read_bytes() == b"old" and not (tmp_path / ".7.part").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["staged", "claim", "discard"])
async def test_cancelled_recovery_locks(tmp_path: Path, monkeypatch, phase: str, session: AsyncSession) -> None:
    source, destination = tmp_path / "source.mkv", tmp_path / "movie.mkv"
    partial = tmp_path / ".7.part"
    trash = trash_path(tmp_path / "trash", 1, "0123456789abcdef", ".mkv")
    state = "prepared"
    if phase == "staged":
        original = trash.parent.with_name(trash.parent.name + ".partial") / trash.name
    elif phase == "claim":
        original = source.with_name(".0123456789abcdef.claim")
    else:
        trash = tmp_path / ".7.discard"
        original = trash
        state = "discard_original_moved"
    original.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"old")
    source_identity = path_size(source)
    source.rename(original)
    row = journal(source, partial, destination, trash, state)
    await prepare_recovery(session, row, source_identity)
    started, completed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_restore = effects.move_file

    def restore(src, dst, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            original_restore(src, dst, **kwargs)
        finally:
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(effects, "move_file", restore)
    task = asyncio.create_task(recover(session, row, library_root=tmp_path))
    try:
        await asyncio.wait_for(started.wait(), 5)
        for _ in range(2):
            task.cancel()
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 5)
            assert not task.done() and not completed.is_set()
            with pytest.raises(ValueError):
                async with library_mutation_lock(tmp_path, wait=False):
                    pytest.fail("recovery worker released library locks")
        assert not source.exists() and original.read_bytes() == b"old"
    finally:
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        await asyncio.wait_for(completed.wait(), 5)

    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert source.read_bytes() == b"old" and not original.exists()
    await session.rollback()
    await session.refresh(row)
    assert row.state == state
    async with library_mutation_lock(tmp_path, wait=False):
        assert completed.is_set()
    if phase == "discard":
        # No claim remains to verify the restored source. Keep the unresolved journal.
        with pytest.raises((FinalizationError, FileNotFoundError)):
            await recover(session, row, library_root=tmp_path)
        assert row.state == state
    else:
        await recover(session, row, library_root=tmp_path)
        assert row.state == "rolled_back"
    assert source.read_bytes() == b"old" and not destination.exists()


@pytest.mark.asyncio
async def test_second_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source = tmp_path / "source"
    source.write_bytes(b"old")
    calls = 0

    async def failing_probe(_: object, path: Path) -> dict:
        nonlocal calls
        calls += 1
        return raw() if calls == 1 else raw("1")

    monkeypatch.setattr("server.finalization.run_ffprobe", failing_probe)
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            tmp_path / "dest",
            workspace,
            "out",
            tmp_path / "trash",
        )
    assert source.exists() and not list(tmp_path.glob(".*.part"))
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_prepared"
    assert source.read_bytes() == b"old" and (workspace / "out").read_bytes() == b"done"


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_long_name_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retain: bool, session: AsyncSession
) -> None:
    workspace, trash = tmp_path / "workspace", tmp_path / "trash"
    workspace.mkdir()
    trash.mkdir()
    source = tmp_path / ("m" * (os.pathconf(tmp_path, "PC_NAME_MAX") - 4) + ".mkv")
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    original_link = os.link

    def link(src: Path, dst: Path, **kwargs) -> None:
        if fd_path(dst, kwargs.get("dst_dir_fd")) == source:
            raise OSError("publication failed")
        original_link(src, dst, **kwargs)

    def cross_link(src, dst, **kwargs):
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if retain and src_path.name.endswith(".claim") and dst_path.name == "file.mkv":
            raise OSError(errno.EXDEV, "cross device")
        return link(src, dst, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.os.link", cross_link)
    with pytest.raises(OSError):
        await run_finalize(
            tmp_path,
            session,
            7,
            source,
            source,
            workspace,
            "out",
            trash,
            trash_mode="all" if retain else "none",
        )
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert Path(row.partial_path) == tmp_path / ".7.part"
    assert Path(row.partial_path).read_bytes() == b"done" and not source.exists()
    assert Path(row.trash_path).read_bytes() == b"old"
    monkeypatch.setattr("server.finalization.os.link", original_link)
    await recover(session, row)
    assert source.read_bytes() == b"done"
    assert row.state == ("completed" if retain else "discard_completed")
    assert Path(row.trash_path).exists() == retain


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".7.part", ".7.discard", ".7-0.part"])
async def test_preserves_temporary_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, destination = tmp_path / "source.mkv", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    (workspace / "subtitle").write_bytes(b"subtitle")
    existing_file = tmp_path / name
    existing_file.write_bytes(b"old-bytes")
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    with pytest.raises(FileExistsError):
        await run_finalize(
            tmp_path,
            session,
            7,
            source,
            destination,
            workspace,
            "out",
            tmp_path / "trash",
            required_artifacts=[("subtitle", tmp_path / "movie.eng.srt")],
        )
    assert source.read_bytes() == b"old" and existing_file.read_bytes() == b"old-bytes"
    assert not destination.exists() and not (tmp_path / "movie.eng.srt").exists()


@pytest.mark.asyncio
async def test_promotion_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source, destination = tmp_path / "source", tmp_path / "dest"
    source.write_bytes(b"old")
    real_link = os.link

    def fail_promotion(source_path: os.PathLike[str] | str, destination_path: os.PathLike[str] | str, **kwargs) -> None:
        if fd_path(Path(destination_path), kwargs.get("dst_dir_fd")) == destination:
            raise OSError("injected")
        real_link(source_path, destination_path, **kwargs)

    async def good_probe(_: object, path: Path) -> dict:
        return raw()

    monkeypatch.setattr("server.finalization.run_ffprobe", good_probe)
    monkeypatch.setattr("server.finalization.os.link", fail_promotion)
    with pytest.raises(OSError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            destination,
            workspace,
            "out",
            tmp_path / "trash",
        )
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert not source.exists() and Path(row.trash_path).exists() and Path(row.partial_path).exists()
    assert row.state == "discard_original_moved"
    monkeypatch.setattr("server.finalization.os.link", real_link)
    await recover(session, row)
    assert destination.read_bytes() == b"done" and row.state == "discard_completed"


@pytest.mark.asyncio
async def test_prepared_rolls_back(tmp_path: Path, session: AsyncSession) -> None:
    source, partial = tmp_path / "source", tmp_path / "partial"
    source.write_bytes(b"source")
    partial.write_bytes(b"partial")
    row = journal(source, partial, tmp_path / "destination", tmp_path / "trash", "prepared")
    await prepare_recovery(session, row, path_size(source), path_size(partial))
    await recover(session, row)
    await session.refresh(row)
    assert row.state == "rolled_back" and source.exists() and not partial.exists()


@pytest.mark.asyncio
async def test_prepared_detects_move(tmp_path: Path, session: AsyncSession) -> None:
    trash = tmp_path / "trash"
    source = tmp_path / "source"
    source.write_bytes(b"source")
    source_identity = path_size(source)
    source.rename(trash)
    partial = tmp_path / "partial"
    partial.write_bytes(b"output")
    row = journal(tmp_path / "source", partial, tmp_path / "destination", trash, "prepared")
    await prepare_recovery(session, row, source_identity, path_size(partial))
    await recover(session, row)
    await session.refresh(row)
    assert row.state == "completed" and Path(row.destination_path).read_bytes() == b"output"


@pytest.mark.asyncio
async def test_claim_recovers(tmp_path: Path, session: AsyncSession) -> None:
    source = tmp_path / "source.mkv"
    trash = trash_path(tmp_path / "root", 1, "0123456789abcdef", ".mkv")
    claim = source.with_name(".0123456789abcdef.claim")
    source.write_bytes(b"source")
    source_identity = path_size(source)
    effects.claim(source, claim)
    row = journal(source, tmp_path / "partial", tmp_path / "destination", trash, "prepared")
    await prepare_recovery(session, row, source_identity)
    await recover(session, row)
    assert source.read_bytes() == b"source" and not claim.exists()


@pytest.mark.asyncio
async def test_in_place_claim_recovers(tmp_path: Path, session: AsyncSession) -> None:
    source = tmp_path / "source.mkv"
    trash = trash_path(tmp_path / "root", 1, "0123456789abcdef", ".mkv")
    claim = source.with_name(".0123456789abcdef.claim")
    source.write_bytes(b"source")
    source_identity = path_size(source)
    effects.claim(source, claim)
    row = journal(source, tmp_path / "partial", source, trash, "prepared")
    await prepare_recovery(session, row, source_identity)
    await recover(session, row)
    assert source.read_bytes() == b"source" and not claim.exists()


@pytest.mark.asyncio
async def test_rejects_claim_mismatch(tmp_path: Path, session: AsyncSession) -> None:
    source = tmp_path / "source.mkv"
    trash = trash_path(tmp_path / "root", 1, "0123456789abcdef", ".mkv")
    trash.parent.mkdir(parents=True)
    trash.write_bytes(b"different")
    claim = source.with_name(".0123456789abcdef.claim")
    source.write_bytes(b"source")
    source_identity = path_size(source)
    effects.claim(source, claim)
    row = journal(source, tmp_path / "partial", tmp_path / "destination", trash, "prepared")
    await prepare_recovery(session, row, source_identity)
    with pytest.raises(FinalizationError):
        await recover(session, row)
    assert claim.exists() and trash.exists()


@pytest.mark.asyncio
async def test_missing_partial_rollback(tmp_path: Path, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "rolled_back")
    try:
        async with factory() as session:
            row = await session.get(FinalizationJournal, identifier)
            assert row is not None
            await recover(session, row)
            assert row.state == "rolled_back" and Path(row.source_path).read_bytes() == b"source"
            assert await session.scalar(select(TrashEntry).where(TrashEntry.id == "0123456789abcdef")) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_promoted_completes(tmp_path: Path, session: AsyncSession) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    source_identity = path_size(source)
    source.rename(tmp_path / "trash")
    destination.write_bytes(b"output")
    os.link(destination, tmp_path / "partial")
    row = journal(source, tmp_path / "partial", destination, tmp_path / "trash", "original_moved")
    await prepare_recovery(session, row, source_identity, path_size(destination))
    await recover(session, row)
    await session.refresh(row)
    assert row.state == "completed"


@pytest.mark.asyncio
async def test_recovers_incomplete(tmp_path: Path, caplog: pytest.LogCaptureFixture, database_factory) -> None:
    engine, factory, identifier = await recovery_db(tmp_path, database_factory, "failed")
    destination, partial = tmp_path / "second-destination", tmp_path / "second-partial"
    partial.write_bytes(b"output")
    trash = tmp_path / "second-trash"
    source = tmp_path / "second-source"
    source.write_bytes(b"original")
    source.rename(trash)
    try:
        async with factory() as session:
            failed = await session.get(FinalizationJournal, identifier)
            assert failed is not None
            Path(failed.trash_path).unlink()
            media = MediaFile(
                library_id=1,
                path=str(tmp_path / "second-source"),
                size=6,
                mtime_ns=1,
                container="mkv",
                medium="movie",
                last_scanned_at=T0,
            )
            session.add(media)
            await session.flush()
            job = Job(media_file_id=media.id, library_id=1, status=JobStatus.FINALIZING, attempts=1)
            session.add(job)
            await session.flush()
            attempt_row = JobAttempt(job_id=job.id, attempt_number=1, status="running", source_size=8, output_size=6)
            session.add(attempt_row)
            await session.flush()
            row = journal(Path(media.path), partial, destination, trash, "original_moved")
            row.attempt_id = attempt_row.id
            session.add(row)
            session.add(
                JobEvent(
                    job_id=job.id,
                    attempt_id=attempt_row.id,
                    type="publication",
                    payload={
                        "library_id": 1,
                        "roots": [str(tmp_path)],
                        "preserve_timestamps": False,
                    },
                )
            )
            await session.commit()
            second_id, job_id = row.id, job.id
        assert await recover_incomplete(factory) == (1, 1)
        async with factory() as session:
            failed = await session.get(FinalizationJournal, identifier)
            recovered = await session.get(FinalizationJournal, second_id)
            job = await session.get(Job, job_id)
            assert failed is not None and failed.state == "original_moved"
            assert recovered is not None and recovered.state == "completed"
            assert job is not None and job.status == JobStatus.SUCCEEDED
        assert destination.read_bytes() == b"output" and not partial.exists()
        assert trash.read_bytes() == b"original"
        record = caplog.records[-1]
        assert record.__dict__["journal_id"] == identifier and record.__dict__["attempt_id"] == failed.attempt_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_extension_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source, destination = tmp_path / "source.mkv", tmp_path / "destination.mp4"
    source.write_bytes(b"old")

    async def good_probe(_: object, path: Path) -> dict:
        return raw()

    monkeypatch.setattr("server.finalization.run_ffprobe", good_probe)
    await run_finalize(
        tmp_path,
        session,
        1,
        source,
        destination,
        workspace,
        "out",
        tmp_path / "trash",
    )
    assert destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["/tmp/out", "../out"])
async def test_rejects_path(tmp_path: Path, artifact: str, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"source")
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            tmp_path / "dest",
            workspace,
            artifact,
            tmp_path / "trash",
        )


@pytest.mark.asyncio
async def test_rejects_symlinks(tmp_path: Path, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source"
    source.write_bytes(b"source")
    (workspace / "out").symlink_to(source)
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            tmp_path / "dest",
            workspace,
            "out",
            tmp_path / "trash",
        )


@pytest.mark.asyncio
async def test_rejects_symlink_parent(tmp_path: Path, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "dir").symlink_to(tmp_path, target_is_directory=True)
    source = tmp_path / "source"
    source.write_bytes(b"source")
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            tmp_path / "dest",
            workspace,
            "dir/out",
            tmp_path / "trash",
        )


@pytest.mark.asyncio
async def test_rejects_destination_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source = tmp_path / "source"
    source.write_bytes(b"source")
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            parent / "dest",
            workspace,
            "out",
            tmp_path / "trash",
        )
    assert source.exists()


@pytest.mark.asyncio
async def test_destination_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source, destination = tmp_path / "movie.mkv", tmp_path / "movie.mp4"
    source.write_bytes(b"old")
    destination.write_bytes(b"old-bytes")
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    with pytest.raises(FileExistsError):
        await run_finalize(tmp_path, session, 1, source, destination, workspace, "out", tmp_path / "trash")
    assert source.read_bytes() == b"old" and destination.read_bytes() == b"old-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("in_place", [False, True])
async def test_recovery_collision(tmp_path: Path, in_place: bool, session: AsyncSession) -> None:
    source, partial, trash = (tmp_path / name for name in ("source", "partial", "trash"))
    destination = source if in_place else tmp_path / "destination"
    partial.write_bytes(b"output")
    source.write_bytes(b"original")
    source_identity = path_size(source)
    source.rename(trash)
    row = journal(source, partial, destination, trash, "discard_original_moved")
    await prepare_recovery(session, row, source_identity, path_size(partial))
    destination.write_bytes(b"old-bytes")
    with pytest.raises(FinalizationError):
        await recover(session, row)
    assert destination.read_bytes() == b"old-bytes"
    assert partial.read_bytes() == b"output" and trash.read_bytes() == b"original"


@pytest.mark.asyncio
async def test_stale_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    stage_fixture(workspace, 1, source)
    stamp = source.stat().st_mtime_ns
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"larger source")
    os.utime(replacement, ns=(stamp, stamp))
    replacement.replace(source)
    (workspace / "out").write_bytes(b"done")
    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    with pytest.raises(ValueError):
        await run_finalize(
            tmp_path,
            session,
            1,
            source,
            source,
            workspace,
            "out",
            tmp_path / "trash",
        )
    assert source.read_bytes() == b"larger source"


@pytest.mark.asyncio
async def test_partial_trash_recovers(tmp_path: Path, session: AsyncSession) -> None:
    source = tmp_path / "movie.mkv"
    trash = trash_path(tmp_path / "trash", 1, "0123456789abcdef", ".mkv")
    staged = trash.parent.with_name(trash.parent.name + ".partial") / trash.name
    staged.parent.mkdir(parents=True)
    source.write_bytes(b"only original")
    source_identity = path_size(source)
    source.rename(staged)
    partial = tmp_path / "output.partial"
    partial.write_bytes(b"output")
    row = journal(source, partial, source, trash, "prepared")
    await prepare_recovery(session, row, source_identity, path_size(partial))
    await recover(session, row)
    await session.refresh(row)
    assert source.read_bytes() == b"only original" and row.state == "rolled_back"


@pytest.mark.asyncio
@pytest.mark.parametrize("in_place", [False, True])
async def test_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_place: bool, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source = tmp_path / "movie.mkv"
    destination = source if in_place else tmp_path / "movie.mp4"
    source.write_bytes(b"old")
    original_link = os.link

    def race(src: Path, dst: Path, **kwargs) -> None:
        if fd_path(dst, kwargs.get("dst_dir_fd")) == destination:
            destination.write_bytes(b"racing old-bytes")
        original_link(src, dst, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.os.link", race)
    with pytest.raises(FileExistsError):
        await run_finalize(tmp_path, session, 1, source, destination, workspace, "out", tmp_path / "trash")
    assert destination.read_bytes() == b"racing old-bytes"
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_original_moved"
    assert Path(row.trash_path).read_bytes() == b"old"
    assert Path(row.partial_path).read_bytes() == b"done"
    with pytest.raises(FinalizationError):
        await recover(session, row)
    assert Path(row.trash_path).read_bytes() == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize("in_place", [False, True])
async def test_published_crash_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_place: bool, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out").write_bytes(b"done")
    source = tmp_path / "movie.mkv"
    destination = source if in_place else tmp_path / "movie.mp4"
    source.write_bytes(b"old")
    original_link = os.link

    def crash(src: Path, dst: Path, **kwargs) -> None:
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        original_link(src, dst, **kwargs)
        if dst_path == destination:
            raise asyncio.CancelledError

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.os.link", crash)
    with pytest.raises(asyncio.CancelledError):
        await run_finalize(tmp_path, session, 1, source, destination, workspace, "out", tmp_path / "trash")
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_original_moved"
    assert destination.samefile(row.partial_path)
    await recover(session, row)
    await session.refresh(row)
    assert row.state == "discard_completed" and destination.read_bytes() == b"done"
    assert not Path(row.trash_path).exists() and Path(row.partial_path).exists()


@pytest.mark.asyncio
async def test_trash_rename_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    workspace, trash = tmp_path / "workspace", tmp_path / "trash"
    workspace.mkdir()
    trash.mkdir()
    (workspace / "out").write_bytes(b"done")
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    original_publish = effects.publish

    def crash(src: Path, dst: Path, **kwargs):
        result = original_publish(src, dst, **kwargs)
        if src.name.endswith(".claim") and dst.parent.name.endswith(".partial"):
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr(effects, "publish", crash)
    with pytest.raises(asyncio.CancelledError):
        await run_finalize(tmp_path, session, 1, source, source, workspace, "out", trash, trash_mode="all")
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "prepared" and not source.exists()
    assert not list(trash.rglob("metadata.json"))
    await recover(session, row)
    await session.refresh(row)
    assert source.read_bytes() == b"old" and row.state == "rolled_back"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["collision", "copy", "link", "crash"])
async def test_required_sidecar_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    sidecars = [(workspace / f"out.{language}.srt", tmp_path / f"movie.{language}.srt") for language in ("eng", "fra")]
    for sidecar, _ in sidecars:
        sidecar.write_bytes(b"subtitle")
    if failure == "collision":
        sidecars[1][1].write_bytes(b"old-bytes")
    original_link, original_copy = os.link, effects.copy_file

    def link(src, dst, **kwargs):
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if dst_path == sidecars[1][1] and failure == "link":
            raise OSError("ENOSPC")
        original_link(src, dst, **kwargs)
        if dst_path == sidecars[1][1] and failure == "crash":
            raise asyncio.CancelledError

    def copy(src, dst, **kwargs):
        if src == sidecars[1][0] and failure == "copy":
            raise OSError("ENOSPC")
        return original_copy(src, dst, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.os.link", link)
    monkeypatch.setattr(effects, "copy_file", copy)
    with pytest.raises(asyncio.CancelledError if failure == "crash" else OSError):
        await asyncio.wait_for(
            run_finalize(
                tmp_path,
                session,
                1,
                source,
                destination,
                workspace,
                "out",
                tmp_path / "trash",
                required_artifacts=[(str(sidecar.relative_to(workspace)), target) for sidecar, target in sidecars],
            ),
            2,
        )
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_artifacts_pending"
    assert source.read_bytes() == b"old" and not destination.exists()
    assert sidecars[0][1].read_bytes() == b"subtitle"
    assert all(sidecar.read_bytes() == b"subtitle" for sidecar, _ in sidecars)
    monkeypatch.setattr("server.finalization.os.link", original_link)
    if failure in {"collision", "copy"}:
        with pytest.raises(FinalizationError):
            await asyncio.wait_for(recover(session, row), 2)
        await session.rollback()
        await session.refresh(row)
        assert row.state == "discard_artifacts_pending" and Path(row.partial_path).is_file()
        if failure == "collision":
            assert sidecars[1][1].read_bytes() == b"old-bytes"
    else:
        await asyncio.wait_for(recover(session, row), 2)
        await session.refresh(row)
        assert row.state == "discard_rolled_back"
        assert sidecars[1][1].read_bytes() == b"subtitle"
    assert source.read_bytes() == b"old" and not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("long_name", [False, True])
async def test_sidecars_before_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_name: bool, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stem = "m" * (os.pathconf(tmp_path, "PC_NAME_MAX") - 8) if long_name else "movie"
    source, destination, subtitle = (tmp_path / f"{stem}{suffix}" for suffix in (".avi", ".mkv", ".eng.srt"))
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    (workspace / "out.eng.srt").write_bytes(b"subtitle")
    original_unlink = os.unlink
    discarded = []

    def unlink(path, *args, **kwargs):
        current = fd_path(path, kwargs.get("dir_fd"))
        if current.suffix == ".discard":
            assert subtitle.read_bytes() == b"subtitle"
            assert destination.read_bytes() == b"done"
            discarded.append(current)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.os.unlink", unlink)
    await asyncio.wait_for(
        run_finalize(
            tmp_path,
            session,
            1,
            source,
            destination,
            workspace,
            "out",
            tmp_path / "trash",
            required_artifacts=[("out.eng.srt", subtitle)],
        ),
        2,
    )
    assert not source.exists() and subtitle.read_bytes() == b"subtitle"
    assert discarded == [tmp_path / ".1.discard"]
    assert (workspace / "out.eng.srt").read_bytes() == b"subtitle"
    await session.rollback()
    event = (await session.scalars(select(JobEvent).where(JobEvent.type == "required_artifacts"))).one()
    assert event.payload["entries"][0]["copy"] == str(tmp_path / ".1-0.part")
    assert event.payload["entries"][0]["size"] == len(b"subtitle")
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "write"])
@pytest.mark.parametrize("phase", ["copy", "probe"])
async def test_partial_quality_rechecked(
    tmp_path: Path, monkeypatch, change: str, phase: str, session: AsyncSession
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    partial = tmp_path / ".7.part"
    changed = False
    copy = effects.copy_file

    def mutate() -> None:
        nonlocal changed
        assert partial.read_bytes() == b"done"
        if change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"larger invalid output")
            replacement.replace(partial)
        else:
            partial.write_bytes(b"larger invalid output")
        changed = True

    async def probe(_binary, path):
        if phase == "probe" and path == partial:
            mutate()
        return raw()

    def changed_copy(src, dst, **kwargs):
        result = copy(src, dst, **kwargs)
        if phase == "copy" and dst == partial:
            mutate()
        return result

    monkeypatch.setattr("server.finalization.run_ffprobe", probe)
    monkeypatch.setattr(effects, "copy_file", changed_copy)
    with pytest.raises(FinalizationError):
        await run_finalize(
            tmp_path,
            session,
            7,
            source,
            destination,
            workspace,
            "out",
            tmp_path / "trash",
        )
    assert changed
    assert (workspace / "out").read_bytes() == b"done"
    assert not partial.exists()
    assert source.read_bytes() == b"old" and not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".7.part", ".7.discard"])
async def test_late_claim_collision(tmp_path: Path, monkeypatch, name: str, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    existing_file = tmp_path / name
    copy, claim = effects.copy_file, effects.claim

    def collide_copy(src, dst, **kwargs):
        if dst == existing_file:
            existing_file.write_bytes(b"old-bytes")
        return copy(src, dst, **kwargs)

    def collide_claim(src, dst):
        if dst == existing_file:
            existing_file.write_bytes(b"old-bytes")
        return claim(src, dst)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr(effects, "copy_file", collide_copy)
    monkeypatch.setattr(effects, "claim", collide_claim)
    with pytest.raises((OSError, ValueError)):
        await run_finalize(tmp_path, session, 7, source, destination, workspace, "out", tmp_path / "trash")
    assert existing_file.exists(), "finalization deleted a temporary path it never acquired"
    assert existing_file.read_bytes() == b"old-bytes"
    assert source.read_bytes() == b"old" and not destination.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("source_state", ["recreated", "missing"])
async def test_prepared_source_checked(
    tmp_path: Path, monkeypatch, retain: bool, source_state: str, session: AsyncSession
) -> None:
    workspace, trash = tmp_path / "workspace", tmp_path / "trash"
    workspace.mkdir()
    trash.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    replace, claim = os.replace, effects.claim
    identifier = "0123456789abcdef"

    def interrupted_claim(src, dst):
        claim(src, dst)
        if not retain and dst.name == ".7.discard":
            raise asyncio.CancelledError

    def interrupted_replace(src, dst, **kwargs):
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        replace(src, dst, **kwargs)
        if retain and src_path.name.endswith(".partial") and dst_path.parent.parent.parent.parent == trash:
            raise asyncio.CancelledError

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.secrets.token_hex", lambda _size: identifier)
    with monkeypatch.context() as patch:
        patch.setattr("server.finalization.os.replace", interrupted_replace)
        patch.setattr(effects, "claim", interrupted_claim)
        with pytest.raises(asyncio.CancelledError):
            await run_finalize(
                tmp_path,
                session,
                7,
                source,
                destination,
                workspace,
                "out",
                trash,
                trash_mode="all" if retain else "none",
            )
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == ("prepared" if retain else "discard_prepared")
    assert not source.exists() and Path(row.trash_path).read_bytes() == b"old"
    if source_state == "recreated":
        source.write_bytes(b"unrelated source")
    else:
        Path(row.trash_path).unlink()
        if retain:
            source.with_name(f".{identifier}.claim").unlink()
    blocked = False
    try:
        await recover(session, row, library_root=tmp_path)
    except OSError, ValueError:
        blocked = True
    assert blocked, "recovery completed without checking the original file's path and size"
    await session.rollback()
    await session.refresh(row)
    assert row.state == ("prepared" if retain else "discard_prepared")
    assert Path(row.partial_path).read_bytes() == b"done"
    if source_state == "recreated":
        assert source.read_bytes() == b"unrelated source" and Path(row.trash_path).read_bytes() == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "write"])
async def test_recovery_size_rejected(tmp_path: Path, monkeypatch, change: str, session: AsyncSession) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    link = os.link

    def interrupted_link(src, dst, **kwargs):
        if fd_path(dst, kwargs.get("dst_dir_fd")) == destination:
            raise asyncio.CancelledError
        link(src, dst, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    with monkeypatch.context() as patch:
        patch.setattr("server.finalization.os.link", interrupted_link)
        with pytest.raises(asyncio.CancelledError):
            await run_finalize(tmp_path, session, 7, source, destination, workspace, "out", tmp_path / "trash")
    await session.rollback()
    row = (await session.scalars(select(FinalizationJournal))).one()
    assert row.state == "discard_original_moved"
    original, partial = Path(row.trash_path), Path(row.partial_path)
    assert original.read_bytes() == b"old" and partial.read_bytes() == b"done"
    if change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"larger replacement")
        replacement.replace(partial)
    else:
        partial.write_bytes(b"larger replacement")
    blocked = False
    try:
        await recover(session, row, library_root=tmp_path)
    except OSError, ValueError:
        blocked = True
    assert blocked, "recovery accepted an output with a changed size"
    await session.rollback()
    await session.refresh(row)
    assert row.state == "discard_original_moved" and original.read_bytes() == b"old"
    assert partial.read_bytes() == b"larger replacement"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_retention_claim_collision(tmp_path: Path, monkeypatch, session: AsyncSession) -> None:
    workspace, trash = tmp_path / "workspace", tmp_path / "trash"
    workspace.mkdir()
    trash.mkdir()
    source, destination = tmp_path / "movie.avi", tmp_path / "movie.mkv"
    source.write_bytes(b"old")
    (workspace / "out").write_bytes(b"done")
    identifier = "0123456789abcdef"
    existing_file = tmp_path / f".{identifier}.claim"
    link = os.link
    collided = False

    def collide(src, dst, **kwargs):
        nonlocal collided
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        dst_path = fd_path(dst, kwargs.get("dst_dir_fd"))
        if src_path == source and dst_path == existing_file:
            existing_file.write_bytes(b"old-bytes")
            collided = True
        link(src, dst, **kwargs)

    monkeypatch.setattr("server.finalization.run_ffprobe", probe_raw)
    monkeypatch.setattr("server.finalization.secrets.token_hex", lambda _size: identifier)
    monkeypatch.setattr("server.trash.os.link", collide)
    try:
        await run_finalize(tmp_path, session, 7, source, destination, workspace, "out", trash, trash_mode="all")
    except OSError, ValueError:
        pass
    assert collided, "the late collision did not reach claim acquisition"
    assert existing_file.exists(), "retention deleted an unexpected source claim"
    assert existing_file.read_bytes() == b"old-bytes"
    assert source.read_bytes() == b"old" and not destination.exists()


def test_promotion_rejects_symlink(tmp_path: Path) -> None:
    from server.finalization import _move_artifact

    workspace, library, outside = tmp_path / "workspace", tmp_path / "library", tmp_path / "outside"
    for directory in (workspace, library, outside):
        directory.mkdir()
    folder = library / "folder"
    folder.symlink_to(outside, target_is_directory=True)
    source = workspace / "subtitle"
    source.write_bytes(b"subtitle")
    with pytest.raises(FinalizationError):
        _move_artifact(source, folder / "movie.eng.srt")
    assert not (outside / "movie.eng.srt").exists()
    assert source.read_bytes() == b"subtitle"


@pytest.mark.asyncio
async def test_recovery_claim_parent(tmp_path: Path, monkeypatch, session: AsyncSession) -> None:
    library, outside, sealed = (tmp_path / name for name in ("library", "outside", "sealed"))
    library.mkdir()
    outside.mkdir()
    source = library / "movie.mkv"
    source.write_bytes(b"original")
    source_identity = path_size(source)
    claim = source.with_name(".0123456789abcdef.claim")
    effects.claim(source, claim)
    trash = trash_path(tmp_path / "trash", 1, "0123456789abcdef", ".mkv")
    trash.parent.mkdir(parents=True)
    effects.copy_file(claim, trash)
    partial = library / ".7.part"
    partial.write_bytes(b"output")
    row = journal(source, partial, library / "movie.mp4", trash, "prepared")
    await prepare_recovery(session, row, source_identity, path_size(partial))
    existing_file = outside / claim.name
    existing_file.write_bytes(b"old-bytes")
    library.rename(sealed)
    library.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        await recover(session, row)
    assert (sealed / claim.name).read_bytes() == trash.read_bytes() == b"original"
    assert existing_file.exists(), "prepared recovery unlinked an unexpected claim outside the configured root"
    assert existing_file.read_bytes() == b"old-bytes"
