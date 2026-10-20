from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from server.models import Job, Library, MediaFile
from server.queue.store import list_jobs
from server.search import count_files, list_files, search_files
from tests.helpers import database_context
from tests.test_scanner import ScanEnv, build_tree, run_scan, scan_env  # noqa: F401


@pytest.mark.asyncio
async def test_search_paging(scan_env: ScanEnv) -> None:
    paths = build_tree(scan_env.lib_dir)
    await run_scan(scan_env)

    async with scan_env.factory() as session:
        hevc = await search_files(session, "hevc")
        assert [row.path for row in hevc] == [str(paths["a"])]
        token = await search_files(session, "s1e1")
        assert [row.path for row in token] == [str(paths["c"])]

        hits = await search_files(session, "h264")
        assert {row.path for row in hits} == {str(paths["b"]), str(paths["c"])}
        assert [row.id for row in hits] == sorted((row.id for row in hits), reverse=True)

        first = await search_files(session, "h264", limit=1, offset=0)
        assert [row.id for row in first] == [hits[0].id]
        second = await search_files(session, "h264", limit=1, offset=1)
        assert [row.id for row in second] == [hits[1].id]
        assert await search_files(session, "h264", limit=10, offset=5) == []
        assert await search_files(session, "   ") == []
        assert await search_files(session, "zzz_no_match") == []

        assert await count_files(session, "h264") == 2
        assert await count_files(session, "h264", library_id=999) == 0


@pytest.mark.asyncio
async def test_file_id_search() -> None:
    async with database_context("file-id-search") as factory, factory() as session:
        library = Library(name="Movies", roots=["/media"])
        other = Library(name="Other", roots=["/other"])
        session.add_all([library, other])
        await session.flush()
        target, named = [
            MediaFile(
                id=identifier,
                library_id=owner.id,
                path=path,
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for identifier, owner, path in (
                (12001, library, "/media/alpha.mkv"),
                (12002, other, "/other/movie-12001.mkv"),
            )
        ]
        session.add_all([target, named])
        await session.commit()

        assert await count_files(session, "12001") == 2
        assert await search_files(session, "12001", limit=1) == [named]
        assert await search_files(session, "12001", limit=1, offset=1) == [target]
        assert await count_files(session, "12001", library_id=library.id) == 1
        assert await search_files(session, "12001", library_id=library.id) == [target]
        assert await search_files(session, " file_id:12001 ") == [target]
        assert await count_files(session, "file_id:12001") == 1
        assert await search_files(session, "file_id:12001", library_id=other.id) == []
        assert await count_files(session, "file_id:12001", library_id=other.id) == 0
        assert await search_files(session, "file_id:99999") == []


@pytest.mark.asyncio
async def test_job_id_search() -> None:
    async with database_context("job-id-search") as factory, factory() as session:
        library = Library(name="Movies", roots=["/media"])
        session.add(library)
        await session.flush()
        target, named = [
            MediaFile(
                library_id=library.id,
                path=path,
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for path in ("/media/alpha.mkv", "/media/movie-12001.mkv")
        ]
        session.add_all([target, named])
        await session.flush()
        history, exact, text, orphan = [
            Job(id=identifier, media_file_id=file_id, library_id=library.id, kind=kind, status=status)
            for identifier, file_id, kind, status in (
                (12000, target.id, "probe", "succeeded"),
                (12001, target.id, "transcode", "failed"),
                (12002, named.id, "healthcheck", "failed"),
                (12003, 99999, "transcode", "failed"),
            )
        ]
        session.add_all([history, exact, text, orphan])
        await session.commit()

        assert await list_jobs(session, query="12001", sort_by="id", limit=1) == ([text], 2)
        assert await list_jobs(session, query="12001", sort_by="id", limit=1, offset=1) == ([exact], 2)
        assert await list_jobs(session, query=" job_id:12001 ", sort_by="path") == ([exact], 1)
        assert await list_jobs(session, query="job_id:12001", statuses=["succeeded"]) == ([], 0)
        assert await list_jobs(session, query="12003") == ([orphan], 1)
        assert await list_jobs(session, query=f"file_id:{target.id}", sort_by="id") == ([exact, history], 2)
        assert await list_jobs(session, query=f"file_id:{target.id}", kind="probe") == ([history], 1)
        assert await list_jobs(session, query="job_id:99998") == ([], 0)


@pytest.mark.asyncio
async def test_search_metacharacters(scan_env: ScanEnv) -> None:
    build_tree(scan_env.lib_dir)
    await run_scan(scan_env)

    async with scan_env.factory() as session:
        for query in ['"', "*", "a*b", 'he"vc', "(*", "OR", "NEAR(a b)", "h264 OR hevc"]:
            assert isinstance(await search_files(session, query), list)

        movie_rows = await search_files(session, "movie")
        assert {row.path for row in movie_rows} == {
            str(scan_env.lib_dir / "movies" / "movie_a.mkv"),
            str(scan_env.lib_dir / "movies" / "movie_b.mp4"),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "h264"], ids=["list", "search"])
async def test_file_status_filters(tmp_path: Path, query: str | None) -> None:
    async with database_context("file-status-filters") as factory, factory() as session:
        library = Library(name="Movies", roots=[str(tmp_path)])
        other = Library(name="Other", roots=[str(tmp_path / "other")])
        session.add_all([library, other])
        await session.flush()
        states = [
            (library.id, "pending", "healthy", "h264"),
            (library.id, "transcoded", "corrupt", "h264"),
            (library.id, "pending", "healthy", "h264"),
            (library.id, "skipped", "healthy", "h264"),
            (library.id, "pending", "pending", "h264"),
            (library.id, "transcoded", "healthy", "hevc"),
            (library.id, "pending", "corrupt", "h264"),
            (other.id, "pending", "healthy", "h264"),
        ]
        files = [
            MediaFile(
                library_id=library_id,
                path=str(tmp_path / f"movie-{index}.mkv"),
                size=1,
                mtime_ns=1,
                container="mkv",
                medium="video",
                last_scanned_at=datetime(2026, 1, 1, tzinfo=UTC),
                transcode_status=transcode,
                health_status=health,
                video_codec=codec,
            )
            for index, (library_id, transcode, health, codec) in enumerate(states)
        ]
        session.add_all(files)
        await session.flush()
        session.add_all(
            [
                Job(media_file_id=files[0].id, library_id=library.id, kind="transcode", status="queued"),
                Job(media_file_id=files[6].id, library_id=library.id, kind="healthcheck", status="running"),
            ]
        )
        await session.commit()

        cases = [
            (None, None, None, list(range(8))),
            ("", "", None, list(range(8))),
            (" , ", " , ", library.id, list(range(7))),
            ("queued", "", None, [0]),
            ("queued, transcoded,", None, None, [0, 1, 5]),
            ("pending,transcoded", None, None, [1, 2, 4, 5, 6, 7]),
            ("", "healthy,corrupt", None, [0, 1, 2, 3, 5, 6, 7]),
            ("queued,transcoded,pending", "healthy,corrupt", library.id, [0, 1, 2, 5, 6]),
            ("queued,transcoded,pending", "healthy,corrupt", other.id, [7]),
        ]
        for transcode, health, library_id, indices in cases:
            expected = [
                files[index] for index in reversed(indices) if query is None or files[index].video_codec == query
            ]
            assert await count_files(
                session, query, library_id=library_id, transcode_status=transcode, health_status=health
            ) == len(expected)
            for offset in range(0, len(expected) + 2, 2):
                if query is None:
                    page = await list_files(
                        session,
                        limit=2,
                        offset=offset,
                        library_id=library_id,
                        transcode_status=transcode,
                        health_status=health,
                    )
                else:
                    page = await search_files(
                        session,
                        query,
                        limit=2,
                        offset=offset,
                        library_id=library_id,
                        transcode_status=transcode,
                        health_status=health,
                    )
                assert page == expected[offset : offset + 2]
