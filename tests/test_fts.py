from __future__ import annotations

import pytest
from sqlalchemy import select, text

from server.models import Library, MediaFile, utcnow
from tests.helpers import database_context


@pytest.mark.asyncio
async def test_search_vector_updates() -> None:
    async with database_context("search-vector") as factory, factory() as session:
        library = Library(name="library", roots=[])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path="/media/original.mkv",
            size=10,
            mtime_ns=1,
            container="mkv",
            video_codec="h264",
            medium="movie",
            last_scanned_at=utcnow(),
        )
        session.add(media)
        await session.commit()

        async def matches(token: str) -> list[int]:
            return list(
                await session.scalars(
                    text("SELECT id FROM media_files WHERE search_vector @@ plainto_tsquery('simple', :token)")
                    .bindparams(token=token)
                    .execution_options(read_only=True)
                )
            )

        before = await session.scalar(
            text("SELECT search_vector::text FROM media_files").execution_options(read_only=True)
        )
        media.size = 20
        await session.commit()
        assert (
            await session.scalar(text("SELECT search_vector::text FROM media_files").execution_options(read_only=True))
            == before
        )
        for column, old, value, new in (
            ("path", "original", "/media/replacement.mp4", "replacement"),
            ("container", "mkv", "mp4", "mp4"),
            ("video_codec", "h264", None, None),
            ("video_codec", None, "hevc", "hevc"),
            ("resolution_bucket", None, "fhd", "fhd"),
            ("resolution_bucket", "fhd", None, None),
            ("medium", "movie", "tv", "tv"),
        ):
            if old is not None:
                assert await matches(old) == [media.id]
            setattr(media, column, value)
            await session.commit()
            if old is not None:
                assert await matches(old) == []
            if new is not None:
                assert await matches(new) == [media.id]
        await session.delete(media)
        await session.commit()
        assert await session.scalar(select(MediaFile.id)) is None
