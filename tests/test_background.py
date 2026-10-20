from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from server.models import Library
from server.scanner import scan
from tests.test_playback import PlaybackEnv, playback_env, seed_media
from tests.test_scan_discovery import fake_fd


@pytest_asyncio.fixture
async def background_env(playback_env: PlaybackEnv, fake_fd, monkeypatch):
    which = scan.shutil.which
    monkeypatch.setattr(scan.shutil, "which", lambda name: str(fake_fd) if name == "fd" else which(name))

    async def prepare(_request):
        roots = [playback_env.lib_dir, playback_env.lib_dir / "elsewhere"]
        monkeypatch.setenv(
            "FAKE_FD_RESULTS",
            json.dumps(
                {
                    str(root): {"paths": [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()]}
                    for root in roots
                    if root.is_dir()
                }
            ),
        )

    playback_env.client.event_hooks["request"].append(prepare)
    return playback_env


@pytest.mark.asyncio
async def test_background_cache(background_env: PlaybackEnv, fake_fd) -> None:
    env = background_env
    await seed_media(env)
    first = b"\xff\xd8\xfffirst"
    (env.lib_dir / "fanart.jpg").write_bytes(first)
    assert (await env.client.get("/api/background")).content == first
    log = fake_fd.parent / "fd-log.jsonl"
    searches = log.read_text()
    other = env.lib_dir / "other"
    other.mkdir()
    await seed_media(env, "other/movie.mkv")
    second = b"\xff\xd8\xffsecond"
    (other / "fanart.jpg").write_bytes(second)
    cached = await env.client.get("/api/background")
    assert cached.content == first
    assert log.read_text() == searches
    assert cached.headers["cache-control"] == "private, no-store"
    assert (await env.client.get("/api/background?force=true")).content == second
    env.app.state.background_image.expires_at = 0
    assert (await env.client.get("/api/background")).content == first


@pytest.mark.asyncio
async def test_background_empty(background_env: PlaybackEnv) -> None:
    env = background_env
    await seed_media(env)
    response = await env.client.get("/api/background")
    assert response.status_code == 204 and response.content == b""
    (env.lib_dir / "fanart.GIF").write_bytes(b"artwork")
    response = await env.client.get("/api/background")
    assert response.status_code == 200 and response.headers["content-type"] == "image/gif"
    assert response.content == b"artwork"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://testserver") as anonymous:
        assert (await anonymous.get("/api/background")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "roots", "deleted"])
async def test_background_invalidated(background_env: PlaybackEnv, change: str) -> None:
    env = background_env
    library_id, _ = await seed_media(env)
    image = env.lib_dir / "fanart.jpg"
    image.write_bytes(b"\xff\xd8\xffartwork")
    assert (await env.client.get("/api/background")).status_code == 200
    if change == "missing":
        image.unlink()
    else:
        async with env.factory() as session:
            library = await session.get(Library, library_id)
            assert library is not None
            if change == "roots":
                (env.lib_dir / "elsewhere").mkdir()
                library.roots = [str(env.lib_dir / "elsewhere")]
            else:
                library.deleted = True
            await session.commit()
    assert (await env.client.get("/api/background")).status_code == 204


@pytest.mark.asyncio
async def test_background_confined(background_env: PlaybackEnv, tmp_path) -> None:
    env = background_env
    await seed_media(env)
    image = tmp_path / "fanart.jpg"
    image.write_bytes(b"\xff\xd8\xffoutside")
    (env.lib_dir / "fanart.jpg").symlink_to(image)
    assert (await env.client.get("/api/background")).status_code == 204


@pytest.mark.asyncio
async def test_background_parent_artwork(background_env: PlaybackEnv) -> None:
    env = background_env
    season = env.lib_dir / "season"
    season.mkdir()
    await seed_media(env, "season/episode.mkv")
    background = env.lib_dir / "fanart.jpg"
    background.write_bytes(b"\xff\xd8\xffbackground")
    (season / "poster.png").write_bytes(b"\x89PNG\r\n\x1a\nposter")
    assert (await env.client.get("/api/background")).content == b"\xff\xd8\xffbackground"
    background.unlink()
    response = await env.client.get("/api/background")
    assert response.content == b"\x89PNG\r\n\x1a\nposter" and response.headers["content-type"] == "image/png"
