from __future__ import annotations

import asyncio

import pytest

from server.models import Library, MediaFile
from server.queue.store import update_library
from tests.test_api import ApiEnv, api_env, seed_media


@pytest.mark.asyncio
async def test_delete_accepts_request(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    source = api_env.lib_dir / "movie.avi"
    service = api_env.app.state.library_deletion
    await service.start()
    api_env.app.state.services_running = True
    events = api_env.app.state.events
    subscription = events.subscribe()
    try:
        response = await api_env.client.delete(f"/api/libraries/{library_id}")
        async with asyncio.timeout(5):
            while event := await subscription.next():
                if event.type == "library.deletion" and event.payload["status"] == "deleted":
                    assert event.payload["library_id"] == library_id
                    break
            else:
                pytest.fail("Library deletion did not complete.")
    finally:
        events.unsubscribe(subscription)
    assert response.status_code == 202
    assert response.json() == {"library_id": library_id, "status": "deleting"}
    assert response.headers["Location"] == f"/api/libraries/{library_id}"
    async with api_env.factory() as session:
        assert await session.get(Library, library_id) is None
        assert await session.get(MediaFile, media_id) is None
    assert source.read_bytes() == b"x" * 2048
    assert (await api_env.client.delete(f"/api/libraries/{library_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_requires_services(api_env: ApiEnv) -> None:
    library_id, media_id = await seed_media(api_env)
    for running in (False, True):
        api_env.app.state.services_running = running
        response = await api_env.client.delete(f"/api/libraries/{library_id}")
        assert response.status_code == 409
        async with api_env.factory() as session:
            library = await session.get(Library, library_id)
            assert library is not None and not library.deleted and not library.suspended
            assert await session.get(MediaFile, media_id) is not None


@pytest.mark.asyncio
async def test_deleted_blocks_resume(api_env: ApiEnv) -> None:
    async with api_env.factory() as session:
        library = Library(name="Movies", roots=[str(api_env.lib_dir)], suspended=True, deleted=True)
        session.add(library)
        await session.commit()
        identifier = library.id
    response = await api_env.client.patch(f"/api/libraries/{identifier}", json={"suspended": False})
    assert response.status_code == 409
    async with api_env.factory() as session:
        with pytest.raises(ValueError):
            await update_library(session, identifier, {"name": "Changed"})


@pytest.mark.asyncio
async def test_delete_status_persists(api_env: ApiEnv) -> None:
    async with api_env.factory() as session:
        library = Library(
            name="Movies",
            roots=[str(api_env.lib_dir)],
            suspended=True,
            deleted=True,
            delete_error="Unresolved publication recovery.",
        )
        session.add(library)
        await session.commit()
        identifier = library.id
    response = await api_env.client.get(f"/api/libraries/{identifier}")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["delete_error"] == "Unresolved publication recovery."


@pytest.mark.asyncio
async def test_delete_blocks_stale(api_env: ApiEnv) -> None:
    async with api_env.factory() as session:
        library = Library(name="Movies", roots=[str(api_env.lib_dir)])
        session.add(library)
        await session.commit()
        identifier = library.id
    async with api_env.factory() as stale:
        reviewed = await stale.get(Library, identifier)
        assert reviewed is not None and not reviewed.deleted
        async with api_env.factory() as current:
            deleting = await current.get(Library, identifier)
            assert deleting is not None
            deleting.deleted = True
            deleting.suspended = True
            await current.commit()
        with pytest.raises(ValueError):
            await update_library(stale, identifier, {"suspended": False})
