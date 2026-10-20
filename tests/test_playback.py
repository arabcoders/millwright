from __future__ import annotations

from tests.helpers import create_user_session

import asyncio
import contextlib
import hashlib
import json
import os
import struct
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from starlette.applications import Starlette
from starlette.requests import Request

from common.fs.identity import source_identity
from tests.helpers import api_client
from tests.helpers import create_test_app as create_app
from server.core.auth import Principal, WsTickets, create_session
from server.core.config import ServerConfig
from server.settings import PlaybackConfig
from server.models import Job, Library, MediaFile, utcnow
from server.playback.artifacts import PlaybackFiles, PlaybackFilesError, validate_playback_dir
from server.playback.grants import PLAYBACK_CONTRACT, PlaybackGrant
from server.playback.posters import POSTER_MAX_BYTES
from server.playback.responses import GuardedPlaybackResponse, PlaybackStreamClosed
from server.playback.segments import MediaSummary, bandwidth_bps, media_playlist
from server.playback.service import PlaybackSession, PlaybackService
from server.playback.subtitles import font_names, srt_to_vtt
from tests.helpers import database_context, make_test_temp_dir

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FAKE_FFPROBE = Path(__file__).parent / "fakes" / "ffprobe.py"
FAKE_FFMPEG = Path(__file__).parent / "fakes" / "ffmpeg.py"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class PlaybackEnv:
    factory: async_sessionmaker[AsyncSession]
    client: httpx.AsyncClient
    app: FastAPI
    lib_dir: Path
    files_dir: Path


@asynccontextmanager
async def playback_context() -> AsyncIterator[PlaybackEnv]:
    lib_dir = make_test_temp_dir("playback-lib")
    files_dir = make_test_temp_dir("playback-files")
    workspace = make_test_temp_dir("playback-workspace")
    async with database_context("playback") as factory:
        app = create_app(
            factory,
            ffprobe_bin=str(FAKE_FFPROBE),
            ffmpeg_bin=str(FAKE_FFMPEG),
            playback_root=files_dir,
        )
        app.state.workspace_dir = workspace
        transport = httpx.ASGITransport(app=app)
        try:
            async with api_client(transport=transport, base_url="http://testserver") as client:
                yield PlaybackEnv(factory=factory, client=client, app=app, lib_dir=lib_dir, files_dir=files_dir)
        finally:
            await app.state.playback.aclose()
            tasks = tuple(app.state.scanner._scan_tasks.values())
            if tasks:
                closing = asyncio.create_task(
                    asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
                )
                cancelled = False
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                for result in closing.result():
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
                if cancelled:
                    raise asyncio.CancelledError


@pytest_asyncio.fixture
async def playback_env() -> AsyncIterator[PlaybackEnv]:
    async with playback_context() as env:
        yield env


async def seed_media(env: PlaybackEnv, name: str = "movie.mkv") -> tuple[int, int]:
    path = env.lib_dir / name
    path.write_bytes(b"x" * 2048)
    identity = source_identity(path.stat())
    async with env.factory() as session:
        library = Library(name=f"lib-{uuid.uuid4().hex}", roots=[str(env.lib_dir)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="mkv",
            video_codec="h264",
            medium="movie",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.flush()
        ids = (library.id, media.id)
        await session.commit()
        return ids


def write_probe(env: PlaybackEnv, name: str, raw: dict) -> None:
    (env.lib_dir / f"{name}.probe.json").write_text(json.dumps(raw), encoding="utf-8")


async def open_playback(
    env: PlaybackEnv,
    file_id: int,
    *,
    mode: str = "direct",
    playback_id: str | None = None,
    audio_stream_index: int | None = None,
    subtitle_track_id: str | None = None,
) -> httpx.Response:
    return await env.client.post(
        f"/api/files/{file_id}/playback",
        json={
            "mode": mode,
            "playback_id": playback_id,
            "audio_stream_index": audio_stream_index,
            "subtitle_track_id": subtitle_track_id,
        },
    )


@pytest.mark.asyncio
async def test_grant_scopes(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)

    direct = await open_playback(playback_env, file_id)
    assert direct.status_code == 200, direct.text
    payload = direct.json()
    playback_id = payload["playback_id"]
    assert playback_id.startswith("pl_")
    assert payload["mode"] == "direct"
    assert payload["media"]["duration_s"] == 600.0
    assert payload["media"]["width"] == 1920 and payload["media"]["height"] == 1080
    assert payload["generation"]["size"] == 2048
    assert payload["urls"]["media"] == f"/api/playback/{playback_id}/media"

    media = await playback_env.client.get(f"/api/playback/{playback_id}/media")
    assert media.status_code == 200
    assert len(media.content) == 2048

    assert (await playback_env.client.get(f"/api/playback/{playback_id}/master.m3u8")).status_code == 200
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/segments/0.ts")).status_code == 200

    hls = await open_playback(playback_env, file_id, mode="hls")
    assert hls.status_code == 200
    hls_id = hls.json()["playback_id"]
    assert hls_id == playback_id
    assert (await playback_env.client.get(f"/api/playback/{hls_id}/media")).status_code == 200
    master = await playback_env.client.get(f"/api/playback/{hls_id}/master.m3u8")
    assert master.status_code == 200
    assert master.text.startswith("#EXTM3U")
    assert master.text.endswith(f"stream.m3u8?audio_stream_index={hls.json()['audio_stream_index']}")

    assert (await playback_env.client.get(f"/api/playback/{hls_id}/stream.m3u8")).status_code == 200
    assert (await playback_env.client.get("/api/playback/pl_missing/stream.m3u8")).status_code == 404


@pytest.mark.asyncio
async def test_select_audio(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import encoding as playback

    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"title": "Commentary"}},
                {"index": 5, "codec_type": "audio", "codec_name": "eac3", "disposition": {"default": 1}},
            ],
            "format": {"duration": "600", "format_name": "matroska"},
        },
    )
    _library_id, file_id = await seed_media(playback_env)
    original = (await open_playback(playback_env, file_id)).json()
    token = original["playback_id"]
    assert original["mode"] == "direct" and original["audio_stream_index"] == 5
    assert original["media"]["video_codec"] == "h264"
    assert [track["stream_index"] for track in original["audio_tracks"]] == [2, 5]
    assert original["audio_tracks"][0]["name"] == "Commentary"
    selected = (await open_playback(playback_env, file_id, playback_id=token, audio_stream_index=2)).json()
    assert selected["mode"] == "hls" and selected["audio_stream_index"] == 2
    assert selected["playback_id"] == token
    assert selected["urls"]["media"] == original["urls"]["media"]
    assert selected["urls"]["poster"] == original["urls"]["poster"]
    assert selected["urls"]["master"] != original["urls"]["master"]

    master = await playback_env.client.get(selected["urls"]["master"])
    stream_uri = master.text.splitlines()[-1]
    stream_url = urljoin(selected["urls"]["master"], stream_uri)
    assert stream_url == f"/api/playback/{token}/stream.m3u8?audio_stream_index=2"
    stream = await playback_env.client.get(stream_url)
    segment_uris = [line for line in stream.text.splitlines() if line and not line.startswith("#")]
    assert all(uri.endswith("?audio_stream_index=2") for uri in segment_uris)
    selected_segment = urljoin(stream_url, segment_uris[0])

    maps = []
    capture = playback.spawn_child

    async def record_capture(*argv, **kwargs):
        maps.append([argv[index + 1] for index, arg in enumerate(argv) if arg == "-map"])
        return await capture(*argv, **kwargs)

    monkeypatch.setattr(playback, "spawn_child", record_capture)
    assert (await playback_env.client.get(selected_segment)).status_code == 200
    assert maps[-1] == ["0:0", "0:2"]
    renewed = (await open_playback(playback_env, file_id, mode="hls", playback_id=token, audio_stream_index=2)).json()
    assert renewed["urls"] == selected["urls"]
    restored = (await open_playback(playback_env, file_id, mode="hls", playback_id=token, audio_stream_index=5)).json()
    assert restored["urls"] == original["urls"]
    # Explicit URLs keep their captured track even after another selection.
    assert (await playback_env.client.get(selected_segment)).status_code == 200
    assert maps[-1] == ["0:0", "0:2"]
    assert (
        await playback_env.client.get(f"/api/playback/{token}/segments/0.ts?audio_stream_index=5")
    ).status_code == 200
    assert maps[-1] == ["0:0", "0:5"]
    default_segment = await playback_env.client.get(f"/api/playback/{token}/segments/0.ts")
    assert default_segment.status_code == 200
    assert default_segment.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert maps[-1] == ["0:0", "0:5"]


@pytest.mark.asyncio
async def test_invalid_audio(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    original = (await open_playback(playback_env, file_id)).json()
    token = original["playback_id"]
    for index in (0, 99, -1, "invalid", True):
        opened = await playback_env.client.post(
            f"/api/files/{file_id}/playback", json={"playback_id": token, "audio_stream_index": index}
        )
        assert opened.status_code == 422
        if not isinstance(index, bool):
            for resource in ("master.m3u8", "stream.m3u8", "segments/0.ts"):
                response = await playback_env.client.get(f"/api/playback/{token}/{resource}?audio_stream_index={index}")
                assert response.status_code == 422
    session = playback_env.app.state.playback.session(token)
    assert session is not None
    assert session.payload() == original


@pytest.mark.asyncio
async def test_select_bitmap(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import encoding as playback

    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                {"index": 2, "codec_type": "audio", "codec_name": "aac"},
                {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
                {"index": 7, "codec_type": "subtitle", "codec_name": "dvd_subtitle"},
                {"index": 9, "codec_type": "subtitle", "codec_name": "subrip"},
                {"index": 11, "codec_type": "subtitle", "codec_name": "dvb_teletext"},
            ],
            "format": {"duration": "600", "format_name": "matroska"},
        },
    )
    _library_id, file_id = await seed_media(playback_env)
    original = (await open_playback(playback_env, file_id)).json()
    token = original["playback_id"]
    assert original["subtitle_track_id"] is None
    assert [track["renderer"] for track in original["subtitles"]] == ["bitmap", "bitmap", "native", "unsupported"]
    selected = (await open_playback(playback_env, file_id, playback_id=token, subtitle_track_id="e5")).json()
    assert selected["mode"] == "hls" and selected["subtitle_track_id"] == "e5"
    assert selected["playback_id"] == token
    assert selected["urls"]["poster"] == original["urls"]["poster"]
    assert selected["subtitles"] == original["subtitles"]
    master = await playback_env.client.get(selected["urls"]["master"])
    stream_url = urljoin(selected["urls"]["master"], master.text.splitlines()[-1])
    assert stream_url.endswith("?audio_stream_index=2&subtitle_track_id=e5")
    stream = await playback_env.client.get(stream_url)
    segment_uris = [line for line in stream.text.splitlines() if line and not line.startswith("#")]
    assert all(uri.endswith("?audio_stream_index=2&subtitle_track_id=e5") for uri in segment_uris)
    selected_segment = urljoin(stream_url, segment_uris[0])
    graphs: list[str | None] = []
    capture = playback.spawn_child

    async def record_capture(*argv, **kwargs):
        graphs.append(argv[argv.index("-filter_complex") + 1] if "-filter_complex" in argv else None)
        return await capture(*argv, **kwargs)

    monkeypatch.setattr(playback, "spawn_child", record_capture)
    assert (await playback_env.client.get(selected_segment)).status_code == 200
    assert graphs[-1] is not None and graphs[-1].startswith("[0:0][0:5]overlay=")
    renewed = (await open_playback(playback_env, file_id, mode="hls", playback_id=token, subtitle_track_id="e5")).json()
    assert renewed["urls"] == selected["urls"]
    other = (await open_playback(playback_env, file_id, mode="hls", playback_id=token, subtitle_track_id="e7")).json()
    assert other["urls"]["master"] != selected["urls"]["master"]
    assert (
        await playback_env.client.get(f"/api/playback/{token}/segments/0.ts?subtitle_track_id=e7")
    ).status_code == 200
    assert graphs[-1] is not None and graphs[-1].startswith("[0:0][0:7]overlay=")
    cleared = (await open_playback(playback_env, file_id, mode="hls", playback_id=token)).json()
    assert cleared["subtitle_track_id"] is None and cleared["urls"] == original["urls"]
    assert (await playback_env.client.get(f"/api/playback/{token}/segments/0.ts")).status_code == 200
    assert graphs[-1] is None
    # Segment URLs keep their subtitle choice after the session changes.
    assert (await playback_env.client.get(selected_segment)).status_code == 200
    assert graphs[-1] is not None and graphs[-1].startswith("[0:0][0:5]overlay=")
    for track_id in ("e2", "e9", "e11", "e999", "../../outside"):
        rejected = await open_playback(playback_env, file_id, playback_id=token, subtitle_track_id=track_id)
        assert rejected.status_code == 422
        for resource in ("master.m3u8", "stream.m3u8", "segments/0.ts"):
            response = await playback_env.client.get(
                f"/api/playback/{token}/{resource}", params={"subtitle_track_id": track_id}
            )
            assert response.status_code == 422
    assert (await playback_env.client.get(f"/api/playback/{token}/subtitles/e5.vtt")).status_code == 404


@pytest.mark.asyncio
async def test_resolved_playlist_urls(playback_env: PlaybackEnv) -> None:
    (playback_env.lib_dir / "movie.en.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
    _library_id, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    master_url = f"/api/playback/{playback_id}/master.m3u8"
    master = await playback_env.client.get(master_url)
    assert master.status_code == 200
    assert "BANDWIDTH=" in master.text
    expected_bandwidth = bandwidth_bps(6.0)
    assert f"BANDWIDTH={expected_bandwidth}" in master.text

    for line in master.text.splitlines():
        if line.startswith("#") or not line:
            continue
        stream_url = urljoin(str(playback_env.client.base_url) + master_url, line)
        stream = await playback_env.client.get(stream_url)
        assert stream.status_code == 200
        media_url = urljoin(str(playback_env.client.base_url) + master_url, "stream.m3u8")
        segment_line = next(l for l in stream.text.splitlines() if l and not l.startswith("#"))
        segment = await playback_env.client.get(urljoin(media_url, segment_line))
        assert segment.status_code == 200
        assert segment.content == b"\0" * 100

        subtitle_line = next(l for l in master.text.splitlines() if "URI=" in l and "subtitles/" in l)
        subtitle_uri = subtitle_line.split('URI="')[1].rstrip('"')
        subtitle_playlist = await playback_env.client.get(urljoin(master_url, subtitle_uri))
        assert subtitle_playlist.status_code == 200
        playlist_url = urljoin(master_url, subtitle_uri)
        cue_uri = next(l for l in subtitle_playlist.text.splitlines() if l and not l.startswith("#"))
        cue = await playback_env.client.get(urljoin(playlist_url, cue_uri))
        assert cue.status_code == 200
        assert cue.text.startswith("WEBVTT")


@pytest.mark.asyncio
async def test_segment_fallback(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _library_id, file_id = await seed_media(playback_env)
    # The capability test passes but the real encode fails: runtime fallback.
    monkeypatch.setenv("FAKE_FFMPEG_RUNTIME_FAIL_ENCODERS", "h264_nvenc")
    playback_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    async with playback_env.factory() as session:
        before = await session.get(MediaFile, file_id)
        assert before is not None
        before_state = (before.updated_at, before.health_status, before.probe_id)
        jobs_before = await session.scalar(select(func.count()).select_from(Job)) or 0

    segment = await playback_env.client.get(f"/api/playback/{playback_id}/segments/5.ts")
    assert segment.status_code == 200
    assert segment.content == b"\0" * 100

    service: PlaybackService = playback_env.app.state.playback
    assert await service.catalog.chain() == ["libx264"]
    session_obj = service.session(playback_id)
    assert session_obj is not None
    assert 1 <= len(session_obj.files._files) <= 3

    async with playback_env.factory() as session:
        after = await session.get(MediaFile, file_id)
        assert after is not None
        assert (after.updated_at, after.health_status, after.probe_id) == before_state
        jobs_after = await session.scalar(select(func.count()).select_from(Job)) or 0
    assert jobs_after == jobs_before
    assert list(playback_env.lib_dir.iterdir()) == [playback_env.lib_dir / "movie.mkv"]


@pytest.mark.asyncio
async def test_segment_failure_rejected(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _library_id, file_id = await seed_media(playback_env)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    playback_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    segment = await playback_env.client.get(f"/api/playback/{playback_id}/segments/0.ts")
    assert segment.status_code == 502
    assert "fake ffmpeg failure" in segment.json()["detail"]
    service: PlaybackService = playback_env.app.state.playback
    session_obj = service.session(playback_id)
    assert session_obj is not None
    assert list(session_obj.files._files) == []


@pytest.mark.asyncio
async def test_concurrent_segments(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    results = await asyncio.gather(
        *(playback_env.client.get(f"/api/playback/{playback_id}/segments/0.ts") for _ in range(4))
    )
    assert [response.status_code for response in results] == [200] * 4
    assert all(response.content == b"\0" * 100 for response in results)
    service: PlaybackService = playback_env.app.state.playback
    session_obj = service.session(playback_id)
    assert session_obj is not None
    assert 1 <= len(session_obj.files._files) <= 3


@pytest.mark.asyncio
async def test_continuous_seeks(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import encoding

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    spawned = []
    spawn = encoding.spawn_child

    async def record(*argv, **kwargs):
        proc = await spawn(*argv, **kwargs)
        spawned.append((argv, proc))
        return proc

    monkeypatch.setattr(encoding, "spawn_child", record)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    base = f"/api/playback/{token}/segments"
    for index in (0, 1, 0):
        response = await playback_env.client.get(f"{base}/{index}.ts")
        assert response.status_code == 200 and len(response.content) == 100000
    assert len(spawned) == 1
    assert (await playback_env.client.get(f"{base}/50.ts")).status_code == 200
    assert len(spawned) == 2
    argv, previous = spawned[0]
    assert previous.returncode is not None
    argv = spawned[1][0]
    assert argv[argv.index("-ss") + 1] == "300.000000"
    assert argv[argv.index("-t") + 1] == "300.000000"
    await playback_env.app.state.playback.aclose()
    assert all(proc.returncode is not None for _, proc in spawned)


async def wait_prepared(run, count: int) -> None:
    async with asyncio.timeout(2):
        while run.next_index < count:
            run.changed.clear()
            await run.changed.wait()


@pytest.mark.asyncio
async def test_encoding_bounds(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback.encoding import CACHE_SEGMENTS
    from server.playback.segments import ENCODE_AHEAD

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    base = f"/api/playback/{token}/segments"
    assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 200
    session = playback_env.app.state.playback.session(token)
    run = session.encoding
    await wait_prepared(run, ENCODE_AHEAD + 1)
    assert run.next_index == ENCODE_AHEAD + 1
    assert len(run.cache) == ENCODE_AHEAD + 1
    task = run.task
    for index in range(1, 13):
        assert (await playback_env.client.get(f"{base}/{index}.ts")).status_code == 200
        await wait_prepared(run, index + ENCODE_AHEAD + 1)
        assert len(session.files._files) <= CACHE_SEGMENTS
        assert run.task is task
    await wait_prepared(run, 13 + ENCODE_AHEAD)
    assert len(run.cache) == CACHE_SEGMENTS
    assert sum(artifact.stat().st_size for artifact in run.cache.values()) == CACHE_SEGMENTS * 100000
    assert len(run.durations) == CACHE_SEGMENTS


@pytest.mark.asyncio
async def test_idle_reaps(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import encoding

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    monkeypatch.setattr(encoding, "IDLE_TIMEOUT_S", 0)
    children = []
    spawn = encoding.spawn_child

    async def record(*argv, **kwargs):
        child = await spawn(*argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(encoding, "spawn_child", record)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    base = f"/api/playback/{token}/segments"
    assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 200
    run = playback_env.app.state.playback.session(token).encoding
    await asyncio.wait_for(run.task, 2)
    assert children[0].returncode is not None
    assert (await playback_env.client.get(f"{base}/1.ts")).status_code == 200
    assert len(children) == 1
    assert (await playback_env.client.get(f"{base}/3.ts")).status_code == 200
    await asyncio.wait_for(run.task, 2)
    assert len(children) == 2 and all(child.returncode is not None for child in children)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["revoked", "expired"])
async def test_encoding_authority(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    from server.playback import encoding

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    children = []
    spawn = encoding.spawn_child

    async def record(*argv, **kwargs):
        child = await spawn(*argv, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(encoding, "spawn_child", record)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    service = playback_env.app.state.playback
    session = service.session(token)
    base = f"/api/playback/{token}/segments"
    assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 200
    await wait_prepared(session.encoding, 3)
    if reason == "revoked":
        session.grant.session_hash = "revoked-session"
        service.revoke_session("revoked-session")
        await asyncio.wait_for(service.aclose(), 2)
        assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 404
    else:
        session.grant.expires_at = utcnow() - timedelta(seconds=1)
        await service.purge()
        await asyncio.wait_for(service.aclose(), 2)
        assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 404
    assert children[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("unreaped", [False, True])
async def test_encoding_cancellation(
    playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, unreaped: bool
) -> None:
    from common.process import ChildNotReapedError
    from server.playback import encoding

    entered, release = asyncio.Event(), asyncio.Event()
    child = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), returncode=None)
    child.stdout.feed_data(b"segment")
    child.stderr.feed_data(b"#EXTM3U\n#EXTINF:6.0,\n#EXT-X-BYTERANGE:7@0\npipe:1\n")
    calls = 0

    async def spawn(*_argv, **_kwargs):
        return child

    async def stop(_proc, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        child.stdout.feed_eof()
        child.stderr.feed_eof()
        if unreaped:
            raise ChildNotReapedError("Unconfirmed fake child.")
        child.returncode = -9
        return -9

    monkeypatch.setattr(encoding, "spawn_child", spawn)
    monkeypatch.setattr(encoding, "stop_child", stop)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    service = playback_env.app.state.playback
    response = await playback_env.client.get(f"/api/playback/{token}/segments/0.ts")
    assert response.content == b"segment"
    session = service.session(token)
    run = session.encoding
    task = run.task
    try:
        task.cancel()
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        task.cancel()
        release.set()
        await asyncio.wait_for(task, 2)
        assert calls == 1
        if unreaped:
            with pytest.raises(ChildNotReapedError):
                await service.aclose()
            assert (playback_env.files_dir / session.files.name).exists()
            assert session.files._files
        else:
            assert child.returncode == -9
            await service.aclose()
            assert not (playback_env.files_dir / session.files.name).exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        if unreaped:
            run.error = None
            await run.aclose()
            session.files.close()


@pytest.mark.asyncio
async def test_muxer_durations(playback_env: PlaybackEnv) -> None:
    write_probe(
        playback_env,
        "movie.mkv",
        {"streams": [{"index": 0, "codec_type": "video"}], "format": {"duration": "13"}},
    )
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    original = await playback_env.client.get(f"/api/playback/{token}/stream.m3u8")
    assert (await playback_env.client.get(f"/api/playback/{token}/segments/2.ts")).status_code == 200
    run = playback_env.app.state.playback.session(token).encoding
    assert run.durations[2] == 1.0
    playlist = await playback_env.client.get(f"/api/playback/{token}/stream.m3u8")
    assert playlist.text.endswith("#EXT-X-ENDLIST")
    assert playlist.text.count("segments/") == 3
    assert "#EXTINF:1.000000,\nsegments/2.ts" in playlist.text
    assert playlist.text == original.text
    run.durations[2] = 1.04
    assert (await playback_env.client.get(f"/api/playback/{token}/stream.m3u8")).text == original.text
    run.durations.clear()
    assert (await playback_env.client.get(f"/api/playback/{token}/stream.m3u8")).text == original.text


@pytest.mark.asyncio
async def test_encoding_identity(playback_env: PlaybackEnv) -> None:
    library_id, file_id = await seed_media(playback_env)
    first = (await open_playback(playback_env, file_id, mode="hls")).json()
    repeated = (await open_playback(playback_env, file_id, mode="hls")).json()
    assert repeated["urls"] == first["urls"]
    path = playback_env.lib_dir / "movie.mkv"
    identity = json.dumps(
        [
            PLAYBACK_CONTRACT,
            file_id,
            library_id,
            str(path),
            {"size": path.stat().st_size},
            None,
            None,
            PlaybackConfig().segment_duration_s,
        ],
        sort_keys=True,
    ).encode()
    predicted = "pl_" + hashlib.sha256(identity).hexdigest()[:32]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=playback_env.app), base_url="http://testserver"
    ) as client:
        assert (await client.get(f"/api/playback/{predicted}/media")).status_code == 404
        assert (await client.delete(f"/api/playback/{predicted}/")).status_code == 404
        media = await client.get(first["urls"]["media"])
        assert media.status_code == 200 and media.content == path.read_bytes()
        assert (await client.delete(f"/api/playback/{first['playback_id']}/")).status_code == 204
        assert (await client.get(first["urls"]["media"])).status_code == 404


@pytest.mark.asyncio
async def test_sessions_keep_settings(playback_env: PlaybackEnv) -> None:
    _, file_id = await seed_media(playback_env)
    first = (await open_playback(playback_env, file_id, mode="hls")).json()
    original = await playback_env.client.get(f"/api/playback/{first['playback_id']}/stream.m3u8")
    changed = await playback_env.client.patch(
        "/api/settings",
        json={"playback": {"segment_duration_s": 3, "grant_ttl_s": 120, "max_grants": 1}},
    )
    assert changed.status_code == 200, changed.text
    refreshed = await open_playback(playback_env, file_id, mode="hls", playback_id=first["playback_id"])
    assert refreshed.status_code == 200, refreshed.text
    assert (await playback_env.client.get(f"/api/playback/{first['playback_id']}/stream.m3u8")).text == original.text
    assert (await open_playback(playback_env, file_id, mode="hls")).status_code == 429
    await playback_env.client.patch("/api/settings", json={"playback": {"max_grants": 2}})
    second = await open_playback(playback_env, file_id, mode="hls")
    assert second.status_code == 200, second.text
    current = second.json()
    assert current["playback_id"] != first["playback_id"]
    playlist = await playback_env.client.get(f"/api/playback/{current['playback_id']}/stream.m3u8")
    assert "#EXTINF:3.000000," in playlist.text
    assert datetime.fromisoformat(current["expires_at"]) < datetime.fromisoformat(first["expires_at"])


@pytest.mark.asyncio
async def test_delivery_timeout(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.api import playback

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    base = f"/api/playback/{token}/segments"
    assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 200
    service = playback_env.app.state.playback
    session = service.session(token)
    run = session.encoding
    await wait_prepared(run, 3)
    task = run.task
    session.delivery_semaphore = asyncio.Semaphore(0)
    monkeypatch.setattr(playback, "_SLOT_WAIT_S", 0)
    assert (await playback_env.client.get(f"{base}/1.ts")).status_code == 503
    assert run.task is task and not task.done()
    assert task in session.work.tasks
    session.delivery_semaphore.release()
    monkeypatch.setattr(playback, "_SLOT_WAIT_S", 120)
    assert (await playback_env.client.get(f"{base}/1.ts")).status_code == 200
    assert run.task is task
    await asyncio.wait_for(service.aclose(), 2)
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_closed_pipes_timeout(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    from server.playback import encoding, segments

    waiting, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    child = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), returncode=None)
    child.stdout.feed_eof()
    child.stderr.feed_eof()
    stops = 0

    async def wait():
        waiting.set()
        await asyncio.Event().wait()

    child.wait = wait

    async def spawn(*_args, **_kwargs):
        return child

    async def stop(*_args, **_kwargs):
        nonlocal stops
        stops += 1
        stopping.set()
        await release.wait()
        child.returncode = -9
        return -9

    async def validate():
        pass

    monkeypatch.setattr(encoding, "spawn_child", spawn)
    monkeypatch.setattr(encoding, "stop_child", stop)
    monkeypatch.setattr(segments, "encode_timeout_s", lambda _: 0)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    session = playback_env.app.state.playback.session(token)
    run = encoding.PlaybackEncoding(playback_env.app.state.playback, session)
    plan = segments.plan_playback(index=0, duration_s=600, segment_s=6, encoder="libx264", summary=session.summary)
    task = asyncio.create_task(run.encode(plan, session.grant.path, validate))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await asyncio.wait_for(stopping.wait(), 2)
        if cancel:
            task.cancel()
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await asyncio.wait_for(task, 2)
        assert child.returncode == -9
        assert stops == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_encoder_failure_logged(
    playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    monkeypatch.setenv("FAKE_FFMPEG_ERR", "decoder unavailable for movie.mkv")
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    response = await playback_env.client.get(f"/api/playback/{token}/segments/0.ts")
    assert response.status_code == 502
    records = [record for record in caplog.records if record.name == "server.playback.encoding"]
    assert len(records) == 1
    assert "decoder unavailable for movie.mkv" in getattr(records[0], "error")
    assert "decoder unavailable for movie.mkv" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["nan", "inf", "0", "-1"])
async def test_muxer_duration_validation(duration: str) -> None:
    from server.playback.encoding import read_ranges

    reader = asyncio.StreamReader()
    reader.feed_data(f"#EXTM3U\n#EXTINF:{duration},\n#EXT-X-BYTERANGE:7@0\npipe:1\n".encode())
    reader.feed_eof()
    with pytest.raises(ValueError):
        await asyncio.wait_for(read_ranges(reader, asyncio.Queue(maxsize=4), bytearray()), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["writer", "cleanup"])
@pytest.mark.parametrize("action", ["close", "switch", "switch_close"])
async def test_encoding_transition(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, block, action) -> None:
    from fastapi import HTTPException

    from server.playback import encoding

    entered, closing = asyncio.Event(), asyncio.Event()
    writer_release, cleanup_release = threading.Event(), asyncio.Event()
    children = []
    loop = asyncio.get_running_loop()
    write, aclose = encoding.PlaybackEncoding.write, encoding.PlaybackEncoding.aclose

    async def spawn(*argv, **_kwargs):
        child = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), returncode=None)
        child.stdout.feed_data(b"segment")
        child.stderr.feed_data(b"#EXTM3U\n#EXTINF:6.0,\n#EXT-X-BYTERANGE:7@0\npipe:1\n")
        children.append((argv, child))
        return child

    async def stop(child, **_kwargs):
        if block == "cleanup" and child is children[0][1]:
            entered.set()
            await cleanup_release.wait()
        child.stdout.feed_eof()
        child.stderr.feed_eof()
        child.returncode = -9
        return -9

    def held_write(fd, data):
        if block == "writer" and not writer_release.is_set():
            loop.call_soon_threadsafe(entered.set)
            assert writer_release.wait(5)
        write(fd, data)

    async def closing_run(self):
        closing.set()
        await aclose(self)

    monkeypatch.setattr(encoding, "spawn_child", spawn)
    monkeypatch.setattr(encoding, "stop_child", stop)
    monkeypatch.setattr(encoding.PlaybackEncoding, "write", staticmethod(held_write))
    monkeypatch.setattr(encoding.PlaybackEncoding, "aclose", closing_run)
    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 1, "codec_type": "audio", "disposition": {"default": 1}},
                {"index": 2, "codec_type": "audio"},
            ],
            "format": {"duration": "600"},
        },
    )
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    service = playback_env.app.state.playback
    session = service.session(token)
    base = f"/api/playback/{token}/segments"
    if block == "cleanup":
        assert (await playback_env.client.get(f"{base}/0.ts")).status_code == 200
    requesting = asyncio.create_task(playback_env.client.get(f"{base}/{5 if block == 'cleanup' else 0}.ts"))
    tasks = [requesting]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        run = session.encoding
        assert run.lock.locked()
        if action != "close":
            switching = asyncio.create_task(open_playback(playback_env, file_id, mode="hls", audio_stream_index=2))
            tasks.append(switching)
            await asyncio.wait_for(closing.wait(), 2)
            assert run.closed
        if action != "switch":
            service.close(token)

        async def validate():
            pass

        late = asyncio.create_task(run.get(6, 1, None, validate))
        tasks.append(late)
        writer_release.set()
        cleanup_release.set()
        response = await asyncio.wait_for(requesting, 2)
        assert response.status_code == 409
        with pytest.raises((HTTPException, asyncio.CancelledError)) as caught:
            await asyncio.wait_for(late, 2)
        if isinstance(caught.value, HTTPException):
            assert caught.value.status_code == 409
        assert len(children) == 1 and children[0][1].returncode == -9
        assert run.task is None
        if action != "close":
            selected = await asyncio.wait_for(switching, 2)
            assert selected.status_code == (200 if action == "switch" else 409)
        if action == "switch":
            assert session.encoding is None and session.audio_stream_index == 2
            assert (await playback_env.client.get(f"{base}/0.ts?audio_stream_index=2")).status_code == 200
            assert len(children) == 2
            argv = children[1][0]
            assert [argv[index + 1] for index, arg in enumerate(argv) if arg == "-map"] == ["0:0", "0:2"]
            assert session.encoding is not run and session.encoding.selection == (2, None)
        await asyncio.wait_for(service.aclose(), 2)
        assert not (playback_env.files_dir / session.files.name).exists()
    finally:
        writer_release.set()
        cleanup_release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["close", "switch"])
async def test_catalog_cancellation(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    from psycopg import AsyncConnection
    from server.api import playback
    from server.fs.source import MediaSnapshot
    from server.playback import encoding
    from server.playback.service import WorkSet

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    _, file_id = await seed_media(playback_env)
    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 1, "codec_type": "audio"},
                {"index": 2, "codec_type": "audio"},
            ],
            "format": {"duration": "600"},
        },
    )
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    service = playback_env.app.state.playback
    session = service.session(token)
    async with playback_env.factory() as db_session:
        media = await db_session.get(MediaFile, file_id)
        assert media is not None
        snapshot = MediaSnapshot.from_media(media)
        library = await db_session.get(Library, media.library_id)
        assert library is not None
    principal = Principal(user_id=None, username="API key")
    entered, transitioned, scope_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ready, begin = asyncio.Event(), asyncio.Event()
    gated = ContextVar("playback_sql_gate", default=False)
    lock_key = 0x504C4341
    reads = 0
    captured = []
    revalidate = playback._revalidate_catalog
    cancel_work, aclose = WorkSet.cancel_and_wait, encoding.PlaybackEncoding.aclose
    stop = encoding.stop_child

    def intercept(connection, _cursor, statement, parameters, _context, _many):
        if gated.get() and "FROM media_files" in statement:
            driver = connection.connection.driver_connection
            assert isinstance(driver, AsyncConnection)
            captured.append((driver, asyncio.current_task()))
            entered.set()
            statement = (
                f"SELECT playback_catalog.* FROM ({statement}) AS playback_catalog "
                f"CROSS JOIN (SELECT pg_advisory_xact_lock({lock_key})) AS playback_gate"
            )
        return statement, parameters

    async def held_catalog(request, grant):
        nonlocal reads
        marker = None
        if session.encoding is not None and asyncio.current_task() is session.encoding.task:
            reads += 1
            if reads == 2:
                ready.set()
                await begin.wait()
                marker = gated.set(True)
        try:
            return await revalidate(request, grant)
        finally:
            if marker is not None:
                scope_finished.set()
                gated.reset(marker)

    async def cancel_session_work(self):
        if self is session.work:
            transitioned.set()
        await cancel_work(self)

    async def closing(self):
        if self is session.encoding:
            transitioned.set()
        await aclose(self)

    async def reaping(child, **kwargs):
        assert scope_finished.is_set()
        return await stop(child, **kwargs)

    monkeypatch.setattr(playback, "_revalidate_catalog", held_catalog)
    monkeypatch.setattr(WorkSet, "cancel_and_wait", cancel_session_work)
    monkeypatch.setattr(encoding.PlaybackEncoding, "aclose", closing)
    monkeypatch.setattr(encoding, "stop_child", reaping)
    engine = playback_env.factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", intercept, retval=True)
    async with playback_env.factory() as blocker, playback_env.factory() as observer:
        tasks = []
        try:
            await blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
            initial = asyncio.create_task(playback_env.client.get(f"/api/playback/{token}/segments/0.ts"))
            tasks.append(initial)
            assert (await asyncio.wait_for(initial, 5)).status_code == 200
            await asyncio.wait_for(ready.wait(), 5)
            begin.set()
            await asyncio.wait_for(entered.wait(), 5)
            assert len(captured) == 1
            driver, read_task = captured[0]
            async with asyncio.timeout(5):
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT FROM pg_locks "
                        "WHERE pid = :pid AND locktype = 'advisory' AND NOT granted)"
                    ),
                    {"pid": driver.info.backend_pid},
                ):
                    assert read_task is not None and not read_task.done()
            run = session.encoding
            worker = run.task
            if action == "close":
                deleting = await playback_env.client.delete(f"/api/playback/{token}/")
                assert deleting.status_code == 204

                async def finish_close():
                    await asyncio.gather(*service._draining)

                finishing = asyncio.create_task(finish_close())
            else:

                async def switch():
                    return await service.open(
                        snapshot, library, principal, mode="hls", source=session.grant.path, audio_stream_index=2
                    )

                finishing = asyncio.create_task(switch())
            tasks.append(finishing)
            await asyncio.wait_for(transitioned.wait(), 5)
            assert worker.cancelling() > 0
            assert not scope_finished.is_set() and not finishing.done()
            assert read_task is not None and read_task is not worker and read_task.cancelling() == 0
            worker.cancel()
            worker.cancel()
            await asyncio.wait_for(blocker.rollback(), 5)
            result = await asyncio.wait_for(finishing, 5)
            if action == "switch":
                assert result is session and session.audio_stream_index == 2
            assert scope_finished.is_set() and read_task.done() and not read_task.cancelled()
            assert worker.done() and run.task is None
            async with asyncio.timeout(5):
                cursor = await driver.execute("SELECT count(*) FROM media_files")
                try:
                    assert await cursor.fetchone() == (1,)
                finally:
                    await cursor.close()
                    await driver.rollback()
                async with playback_env.factory() as db_session:
                    assert await db_session.scalar(select(MediaFile.id)) == file_id
                reopened = await open_playback(playback_env, file_id, mode="hls")
                assert reopened.status_code == 200
                assert (await playback_env.client.get(reopened.json()["urls"]["media"])).status_code == 200
        finally:
            begin.set()
            event.remove(engine.sync_engine, "before_cursor_execute", intercept)
            try:
                await asyncio.wait_for(blocker.rollback(), 5)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
                finally:
                    await asyncio.wait_for(service.aclose(), 5)


@pytest.mark.asyncio
async def test_encoding_queue(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import encoding

    monkeypatch.setenv("FAKE_FFMPEG_OUTPUT_BYTES", "100000")
    service = playback_env.app.state.playback
    service._semaphore = asyncio.Semaphore(1)
    entered = asyncio.Event()
    children = []
    spawn, run = encoding.spawn_child, encoding.PlaybackEncoding.run

    async def record(*argv, **kwargs):
        child = await spawn(*argv, **kwargs)
        children.append(child)
        return child

    async def waiting(self, *args, **kwargs):
        if children:
            entered.set()
        return await run(self, *args, **kwargs)

    monkeypatch.setattr(encoding, "spawn_child", record)
    monkeypatch.setattr(encoding.PlaybackEncoding, "run", waiting)
    _, first_id = await seed_media(playback_env)
    _, second_id = await seed_media(playback_env, "other.mkv")
    first = (await open_playback(playback_env, first_id, mode="hls")).json()["playback_id"]
    second = (await open_playback(playback_env, second_id, mode="hls")).json()["playback_id"]
    assert (await playback_env.client.get(f"/api/playback/{first}/segments/0.ts")).status_code == 200
    requesting = asyncio.create_task(playback_env.client.get(f"/api/playback/{second}/segments/0.ts"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(children) == 1
        service.close(first)
        response = await asyncio.wait_for(requesting, 2)
        assert response.status_code == 200
        assert children[0].returncode is not None
        assert len(children) == 2
    finally:
        if not requesting.done():
            requesting.cancel()
        await asyncio.wait_for(asyncio.gather(requesting, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_encoding_disconnect(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.api import playback
    from server.playback import encoding

    entered, disconnected, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def spawn(*_argv, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            completed.set()

    monkeypatch.setattr(encoding, "spawn_child", spawn)
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "app": playback_env.app, "method": "GET", "headers": []}, receive)
    requesting = asyncio.create_task(playback.get_segment(token, 0, request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        disconnected.set()
        with pytest.raises(playback.HTTPException) as caught:
            await asyncio.wait_for(requesting, 2)
        assert caught.value.status_code == 409
        assert completed.is_set()
        session = playback_env.app.state.playback.session(token)
        assert session.files._files == {}
        assert session.encoding.task is None
    finally:
        disconnected.set()
        if not requesting.done():
            requesting.cancel()
        await asyncio.wait_for(asyncio.gather(requesting, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_conversion_output_limit(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.playback.segments.SEGMENT_MAX_BYTES", 64)
    _library_id, file_id = await seed_media(playback_env)
    payload = (await open_playback(playback_env, file_id, mode="hls")).json()
    response = await playback_env.client.get(f"/api/playback/{payload['playback_id']}/segments/0.ts")
    assert response.status_code == 502
    session = playback_env.app.state.playback.session(payload["playback_id"])
    assert session is not None
    assert session.files._files == {}


@pytest.mark.asyncio
async def test_close_preparing_segment(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, completed = asyncio.Event(), asyncio.Event()

    async def encode(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            completed.set()

    monkeypatch.setattr("server.playback.encoding.spawn_child", encode)
    _library_id, file_id = await seed_media(playback_env)
    payload = (await open_playback(playback_env, file_id, mode="hls")).json()
    service = playback_env.app.state.playback
    session = service.session(payload["playback_id"])
    assert session is not None
    requesting = asyncio.create_task(playback_env.client.get(f"/api/playback/{payload['playback_id']}/segments/0.ts"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        service.close(payload["playback_id"])
        response = await asyncio.wait_for(requesting, 2)
        assert response.status_code == 409
        assert completed.is_set()
        await asyncio.wait_for(service.aclose(), 2)
        assert not (playback_env.files_dir / session.files.name).exists()
        assert (playback_env.lib_dir / "movie.mkv").read_bytes() == b"x" * 2048
    finally:
        if not requesting.done():
            requesting.cancel()
        await asyncio.wait_for(asyncio.gather(requesting, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_media_ranges(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    base = f"/api/playback/{playback_id}/media"

    head = await playback_env.client.head(base)
    assert head.status_code == 200
    assert head.headers["content-length"] == "2048"
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert head.content == b""

    full = await playback_env.client.get(base, headers={"Accept-Encoding": "gzip"})
    assert full.status_code == 200 and full.content == b"x" * 2048
    assert "content-encoding" not in full.headers

    partial = await playback_env.client.get(base, headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == "bytes 0-99/2048"
    assert len(partial.content) == 100
    assert "content-encoding" not in partial.headers

    suffix = await playback_env.client.get(base, headers={"Range": "bytes=-48"})
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == "bytes 2000-2047/2048"

    for malformed in ("bytes=0-1,4-5", "bytes=5000-6000", "bytes=-", "bytes=--5", "bytes=abc-9"):
        response = await playback_env.client.get(base, headers={"Range": malformed})
        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */2048"


@pytest.mark.asyncio
async def test_media_acquisition_cancellation(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    _, file_id = await seed_media(playback_env)
    token = (await open_playback(playback_env, file_id)).json()["playback_id"]
    path = playback_env.lib_dir / "movie.mkv"
    url = f"/api/playback/{token}/media"
    entered, checkpoint = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    open_file = Path.open
    opened = []

    def blocked_open(current, *args, **kwargs):
        if current == path:
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("Playback source acquisition was not released.")
        handle = open_file(current, *args, **kwargs)
        if current == path:
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", blocked_open)
    task = asyncio.create_task(playback_env.client.get(url))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await asyncio.wait_for(playback_env.client.head(url), 2)).status_code == 200
        for _ in range(2):
            task.cancel()
            checkpoint.clear()
            loop.call_soon(checkpoint.set)
            await asyncio.wait_for(checkpoint.wait(), 2)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(opened) == 1 and opened[0].closed
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_playback_reads_current(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    path = playback_env.lib_dir / "movie.mkv"
    direct_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    assert (await playback_env.client.get(f"/api/playback/{direct_id}/media")).status_code == 200
    playback_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]

    path.write_bytes(b"y" * 4096)
    media = await playback_env.client.get(f"/api/playback/{direct_id}/media")
    assert media.status_code == 200 and media.content == b"y" * 4096
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/segments/0.ts")).status_code == 200


@pytest.mark.asyncio
async def test_suspended_library_refused(playback_env: PlaybackEnv) -> None:
    library_id, file_id = await seed_media(playback_env)
    async with playback_env.factory() as session:
        library = await session.get(Library, library_id)
        assert library is not None
        library.suspended = True
        await session.commit()

    response = await open_playback(playback_env, file_id)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mode_switch_grant(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    now = utcnow()
    monkeypatch.setattr("server.api.playback.utcnow", lambda: now)
    monkeypatch.setattr("server.playback.service.utcnow", lambda: now)
    _library_id, file_id = await seed_media(playback_env)
    direct = await open_playback(playback_env, file_id)
    playback_id = direct.json()["playback_id"]
    service = playback_env.app.state.playback
    session = service.session(playback_id)
    assert session is not None
    grant = session.grant
    original_expiry = grant.expires_at

    now += timedelta(seconds=60)
    switched = await open_playback(playback_env, file_id, mode="hls", playback_id=playback_id)
    assert switched.status_code == 200
    assert switched.json()["playback_id"] == playback_id
    assert switched.json()["mode"] == "hls"
    assert grant.expires_at == original_expiry + timedelta(seconds=60)
    now = original_expiry + timedelta(seconds=1)
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/media")).status_code == 200
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/master.m3u8")).status_code == 200

    _other_library, other_file = await seed_media(playback_env, "other.mkv")
    clash = await open_playback(playback_env, other_file, playback_id=playback_id)
    assert clash.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["poster", "subtitles/e2.vtt", "fonts/f3"])
async def test_switch_during_extraction(
    playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    from server.api import playback

    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_name": "h264", "codec_type": "video"},
                {"index": 1, "codec_name": "aac", "codec_type": "audio"},
                {"index": 2, "codec_name": "subrip", "codec_type": "subtitle"},
                {
                    "index": 3,
                    "codec_name": "ttf",
                    "codec_type": "attachment",
                    "tags": {"filename": "Fake Font.ttf", "mimetype": "font/ttf"},
                },
            ],
            "format": {"duration": "600.0", "format_name": "matroska"},
        },
    )
    _library_id, file_id = await seed_media(playback_env)
    payload = (await open_playback(playback_env, file_id)).json()
    playback_id = payload["playback_id"]
    entered, release = asyncio.Event(), asyncio.Event()
    run_capture = playback.run_capture

    async def held_capture(*args, **kwargs):
        entered.set()
        await release.wait()
        return await run_capture(*args, **kwargs)

    monkeypatch.setattr(playback, "run_capture", held_capture)
    requesting = asyncio.create_task(playback_env.client.get(f"/api/playback/{playback_id}/{resource}"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        switched = await open_playback(playback_env, file_id, mode="hls", playback_id=playback_id)
        assert switched.status_code == 200
        assert switched.json()["playback_id"] == playback_id
        release.set()
        response = await asyncio.wait_for(requesting, 2)
        assert response.status_code == 200, response.text
        assert response.content
        assert (await playback_env.client.get(f"/api/playback/{playback_id}/media")).status_code == 200
        assert (await playback_env.client.get(f"/api/playback/{playback_id}/master.m3u8")).status_code == 200
        session = playback_env.app.state.playback.session(playback_id)
        assert session is not None
        assert session.files._files == {}
    finally:
        release.set()
        if not requesting.done():
            requesting.cancel()
        await asyncio.wait_for(asyncio.gather(requesting, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_concurrent_playback_opens(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.playback import service

    both_entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    probe = service.run_ffprobe

    async def held_probe(*args, **kwargs):
        nonlocal count
        raw = await probe(*args, **kwargs)
        count += 1
        if count == 2:
            both_entered.set()
        await release.wait()
        return raw

    monkeypatch.setattr(service, "run_ffprobe", held_probe)
    playback_env.app.state.playback.config.max_grants = 1
    _library_id, file_id = await seed_media(playback_env)
    requests = [asyncio.create_task(open_playback(playback_env, file_id)) for _ in range(2)]
    try:
        await asyncio.wait_for(both_entered.wait(), 2)
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(*requests), 2)
        assert first.status_code == second.status_code == 200
        assert first.json()["playback_id"] == second.json()["playback_id"]
        assert len(list(playback_env.files_dir.iterdir())) == 1
    finally:
        release.set()
        for request in requests:
            if not request.done():
                request.cancel()
        await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), 2)


@pytest.mark.asyncio
async def test_stable_playback_urls(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    now = utcnow()
    for module in ("server.playback.service", "server.api.playback", "server.playback.responses"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: now)
    _library_id, file_id = await seed_media(playback_env)
    first = (await open_playback(playback_env, file_id)).json()
    assert (await playback_env.client.delete(f"/api/playback/{first['playback_id']}/")).status_code == 204
    reopened = (await open_playback(playback_env, file_id)).json()
    assert reopened["playback_id"] == first["playback_id"]
    assert reopened["urls"] == first["urls"]

    now += timedelta(days=3)
    renewed = (await open_playback(playback_env, file_id)).json()
    assert renewed["urls"] == first["urls"]
    assert renewed["expires_at"] != first["expires_at"]
    previous = playback_env.app.state.playback
    await previous.aclose()
    playback_env.app.state.playback = PlaybackService(
        previous.config, ffmpeg_bin=str(FAKE_FFMPEG), ffprobe_bin=str(FAKE_FFPROBE), files_dir=playback_env.files_dir
    )
    restarted = (await open_playback(playback_env, file_id)).json()
    assert (await playback_env.client.get(first["urls"]["media"])).status_code == 404
    assert (await playback_env.client.get(restarted["urls"]["media"])).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "hls"])
@pytest.mark.parametrize("catalog", ["missing", "stale"])
async def test_playback_stale_catalog(playback_env: PlaybackEnv, mode: str, catalog: str) -> None:
    _, file_id = await seed_media(playback_env)
    path = playback_env.lib_dir / "movie.mkv"
    if catalog == "stale":
        path.write_bytes(b"current media")
    async with playback_env.factory() as session:
        file = await session.get(MediaFile, file_id)
        assert file is not None
        if catalog == "missing":
            file.source_identity = None
            await session.commit()
            await session.refresh(file)
        before = (file.source_identity, file.size, file.mtime_ns, file.probe_id, file.health_status, file.updated_at)
    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [{"index": 0, "codec_name": "h264", "codec_type": "video", "width": 1280, "height": 720}],
            "format": {"duration": "13", "format_name": "matroska"},
        },
    )
    opened = await open_playback(playback_env, file_id, mode=mode)
    assert opened.status_code == 200, opened.text
    payload = opened.json()
    assert payload["generation"] == source_identity(path.stat())
    assert payload["media"]["duration_s"] == 13
    assert (payload["media"]["width"], payload["media"]["height"]) == (1280, 720)
    media = await playback_env.client.get(payload["urls"]["media"])
    assert media.status_code == 200 and media.content == path.read_bytes()
    if mode == "hls":
        base = f"/api/playback/{payload['playback_id']}"
        assert (await playback_env.client.get(f"{base}/stream.m3u8")).status_code == 200
        assert (await playback_env.client.get(f"{base}/segments/0.ts")).status_code == 200
    async with playback_env.factory() as session:
        file = await session.get(MediaFile, file_id)
        assert file is not None
        assert (
            file.source_identity,
            file.size,
            file.mtime_ns,
            file.probe_id,
            file.health_status,
            file.updated_at,
        ) == before
        assert await session.scalar(select(func.count(Job.id))) == 0


@pytest.mark.asyncio
async def test_replaced_playback_url(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    first = (await open_playback(playback_env, file_id)).json()
    path = playback_env.lib_dir / "movie.mkv"
    path.write_bytes(b"y" * 4096)
    opened = await open_playback(playback_env, file_id)
    assert opened.status_code == 200, opened.text
    replaced = opened.json()
    assert replaced["playback_id"] != first["playback_id"]
    prior = await playback_env.client.get(first["urls"]["media"])
    assert prior.status_code == 200 and prior.content == b"y" * 4096
    response = await playback_env.client.get(replaced["urls"]["media"])
    assert response.status_code == 200
    assert response.content == b"y" * 4096


@pytest.mark.asyncio
async def test_cached_delivery_headers(playback_env: PlaybackEnv) -> None:
    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_name": "h264", "codec_type": "video"},
                {"index": 1, "codec_name": "aac", "codec_type": "audio"},
                {"index": 2, "codec_name": "subrip", "codec_type": "subtitle"},
                {
                    "index": 3,
                    "codec_name": "ttf",
                    "codec_type": "attachment",
                    "tags": {"filename": "Fake Font.ttf", "mimetype": "font/ttf"},
                },
            ],
            "format": {"duration": "600.0", "format_name": "matroska"},
        },
    )
    _library_id, file_id = await seed_media(playback_env)
    opened = await open_playback(playback_env, file_id)
    assert opened.headers["cache-control"] == "private, no-store"
    payload = opened.json()
    base = f"/api/playback/{payload['playback_id']}"
    for resource in ("media", "segments/0.ts", "subtitles/e2.vtt", "fonts/f3", "poster"):
        response = await playback_env.client.get(f"{base}/{resource}")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    for resource in ("master.m3u8", "stream.m3u8", "subtitles/e2.m3u8"):
        response = await playback_env.client.get(f"{base}/{resource}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_close_and_expiry(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.api.playback.utcnow", lambda: T0)
    monkeypatch.setattr("server.playback.service.utcnow", lambda: T0)
    _library_id, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]

    closed = await playback_env.client.delete(f"/api/playback/{playback_id}/")
    assert closed.status_code == 204
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/media")).status_code == 404

    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    monkeypatch.setattr("server.api.playback.utcnow", lambda: T0 + timedelta(seconds=7200))
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/media")).status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_playback() -> None:
    lib_dir = make_test_temp_dir("playback-auth-lib")
    files_dir = make_test_temp_dir("playback-auth-files")
    async with database_context("playback-auth") as factory:
        app = create_app(
            factory,
            config=ServerConfig(api_key="test-key"),
            ffprobe_bin=str(FAKE_FFPROBE),
            ffmpeg_bin=str(FAKE_FFMPEG),
            playback_root=files_dir,
        )
        app.state.workspace_dir = make_test_temp_dir("playback-auth-ws")
        path = lib_dir / "movie.mkv"
        path.write_bytes(b"x" * 2048)
        identity = source_identity(path.stat())
        async with factory() as session:
            library = Library(name=f"lib-{uuid.uuid4().hex}", roots=[str(lib_dir)])
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(path),
                size=identity["size"],
                mtime_ns=path.stat().st_mtime_ns,
                source_identity=identity,
                container="mkv",
                video_codec="h264",
                medium="movie",
                last_scanned_at=T0,
            )
            session.add(media)
            _, token = await create_user_session(session, "user-1", 7)
            await session.commit()
        headers = {"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"}
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                opened = await client.post(f"/api/files/{media.id}/playback", headers=headers)
                assert opened.status_code == 200
                playback_id = opened.json()["playback_id"]
                assert (await client.get(f"/api/playback/{playback_id}/media")).status_code == 200
                assert (await client.post("/api/auth/logout", headers=headers)).status_code == 204
                assert (await client.get(f"/api/playback/{playback_id}/media")).status_code in {401, 404}
                assert app.state.playback.session(playback_id) is None
        finally:
            await app.state.playback.aclose()


@pytest.mark.asyncio
async def test_logout_during_probe() -> None:
    lib_dir = make_test_temp_dir("playback-probe-lib")
    files_dir = make_test_temp_dir("playback-probe-files")
    async with database_context("playback-probe") as factory:
        app = create_app(
            factory,
            config=ServerConfig(api_key="test-key"),
            ffprobe_bin=str(FAKE_FFPROBE),
            ffmpeg_bin=str(FAKE_FFMPEG),
            playback_root=files_dir,
        )
        app.state.workspace_dir = make_test_temp_dir("playback-probe-ws")
        path = lib_dir / "movie.mkv"
        path.write_bytes(b"x" * 2048)
        identity = source_identity(path.stat())
        async with factory() as session:
            library = Library(name=f"lib-{uuid.uuid4().hex}", roots=[str(lib_dir)])
            session.add(library)
            await session.flush()
            media = MediaFile(
                library_id=library.id,
                path=str(path),
                size=identity["size"],
                mtime_ns=path.stat().st_mtime_ns,
                source_identity=identity,
                container="mkv",
                video_codec="h264",
                medium="movie",
                last_scanned_at=T0,
            )
            session.add(media)
            _, token = await create_user_session(session, "user-2", 7)
            await session.commit()
        headers = {"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"}

        from server.playback import service as service_module

        probed = asyncio.Event()
        entered = asyncio.Event()
        original_probe = service_module.run_ffprobe

        async def held_probe(*args: Any, **kwargs: Any) -> dict:
            entered.set()
            await probed.wait()
            return await original_probe(*args, **kwargs)

        monkeypatch_holder = pytest.MonkeyPatch()
        monkeypatch_holder.setattr(service_module, "run_ffprobe", held_probe)
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                opening = asyncio.create_task(client.post(f"/api/files/{media.id}/playback", headers=headers))
                await asyncio.wait_for(entered.wait(), 2)
                revoke = await client.post("/api/auth/logout", headers=headers)
                assert revoke.status_code == 204
                probed.set()
                opened = await asyncio.wait_for(opening, 2)
                assert opened.status_code == 401
                assert app.state.playback._sessions == {}
        finally:
            probed.set()
            if not opening.done():
                opening.cancel()
            await asyncio.wait_for(asyncio.gather(opening, return_exceptions=True), 2)
            monkeypatch_holder.undo()
            await app.state.playback.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "data", "mimetype"),
    [
        ("movie-poster.JPG", b"\xff\xd8\xffposter", "image/jpeg"),
        ("movie.jpeg", b"\xff\xd8\xffposter", "image/jpeg"),
        ("Poster.PNG", b"\x89PNG\r\n\x1a\nposter", "image/png"),
        ("cover.webp", b"RIFF\x04\x00\x00\x00WEBPposter", "image/webp"),
        ("folder.jpg", b"\xff\xd8\xffposter", "image/jpeg"),
    ],
)
async def test_nearby_poster(
    playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, name: str, data: bytes, mimetype: str
) -> None:
    _library_id, file_id = await seed_media(playback_env)
    (playback_env.lib_dir / name).write_bytes(data)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    payload = (await open_playback(playback_env, file_id)).json()
    url = payload["urls"]["poster"]
    assert "?v=" in url
    assert (await open_playback(playback_env, file_id)).json()["urls"]["poster"] == url
    poster = await playback_env.client.get(url)
    assert poster.status_code == 200
    assert poster.headers["content-type"] == mimetype
    assert "immutable" in poster.headers["cache-control"]
    assert poster.content == data
    unversioned = await playback_env.client.get(url.split("?")[0])
    assert unversioned.content == data
    assert unversioned.headers["cache-control"] == "private, no-store"
    assert (await playback_env.client.delete(f"/api/playback/{payload['playback_id']}/")).status_code == 204
    assert (await playback_env.client.get(url)).status_code == 404


@pytest.mark.asyncio
async def test_poster_priority(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    for name in ("movie-poster.avif", "movie.jpg", "poster.jpg", "cover.jpg", "folder.jpg"):
        (playback_env.lib_dir / name).write_bytes(name.encode())
    payload = (await open_playback(playback_env, file_id)).json()
    poster = await playback_env.client.get(payload["urls"]["poster"])
    assert poster.status_code == 200
    assert poster.headers["content-type"] == "image/avif"
    assert poster.content == b"movie-poster.avif"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["empty", "symlink", "directory", "fifo", "oversized"])
async def test_poster_fallback(playback_env: PlaybackEnv, tmp_path: Path, kind: str) -> None:
    _library_id, file_id = await seed_media(playback_env)
    path = playback_env.lib_dir / "poster.jpg"
    match kind:
        case "empty":
            path.touch()
        case "symlink":
            target = tmp_path / "outside.jpg"
            target.write_bytes(b"\xff\xd8\xffoutside")
            path.symlink_to(target)
        case "directory":
            path.mkdir()
        case "fifo":
            os.mkfifo(path)
        case "oversized":
            with path.open("wb") as image:
                image.write(b"\xff\xd8\xff")
                image.truncate(POSTER_MAX_BYTES + 1)
    payload = (await open_playback(playback_env, file_id)).json()
    poster = await playback_env.client.get(payload["urls"]["poster"])
    assert poster.status_code == 200
    assert poster.headers["content-type"].startswith("image/jpeg")
    assert poster.content == b"\0" * 100
    assert "immutable" in poster.headers["cache-control"]


@pytest.mark.asyncio
async def test_poster_replacement(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    path = playback_env.lib_dir / "poster.jpg"
    path.write_bytes(b"\xff\xd8\xfforiginal")
    payload = (await open_playback(playback_env, file_id)).json()
    path.unlink()
    path.write_bytes(b"\xff\xd8\xffreplacement")
    current = await playback_env.client.get(payload["urls"]["poster"])
    assert current.status_code == 200
    assert current.content == b"\xff\xd8\xffreplacement"
    assert (await playback_env.client.delete(f"/api/playback/{payload['playback_id']}/")).status_code == 204
    reopened = (await open_playback(playback_env, file_id)).json()
    assert reopened["playback_id"] == payload["playback_id"]
    assert reopened["urls"]["poster"] != payload["urls"]["poster"]
    assert (await playback_env.client.get(reopened["urls"]["poster"])).content == b"\xff\xd8\xffreplacement"
    assert (await playback_env.client.get(payload["urls"]["poster"])).status_code == 410


@pytest.mark.asyncio
async def test_poster_failure_clean(
    playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _library_id, file_id = await seed_media(playback_env)
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")
    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    poster = await playback_env.client.get(f"/api/playback/{playback_id}/poster")
    assert poster.status_code == 422
    assert "fake ffmpeg failure" in poster.json()["detail"]
    record = next(record for record in caplog.records if record.name == "server.api.playback")
    assert getattr(record, "error") in poster.json()["detail"]
    service: PlaybackService = playback_env.app.state.playback
    session = service.session(playback_id)
    assert session is not None and not session.files._files


@pytest.mark.asyncio
async def test_preparation_catalog_cleanup(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from server.api import playback

    _, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    revalidate = playback._revalidate_catalog
    calls = 0

    async def removed(request, grant):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(410, "Library removed during conversion.")
        return await revalidate(request, grant)

    monkeypatch.setattr(playback, "_revalidate_catalog", removed)
    response = await playback_env.client.get(f"/api/playback/{playback_id}/poster")
    assert response.status_code == 410
    assert response.json()["detail"] == "Library removed during conversion."
    session = playback_env.app.state.playback.session(playback_id)
    assert session is not None and not session.files._files


@pytest.mark.asyncio
async def test_audio_only_media(playback_env: PlaybackEnv) -> None:
    write_probe(
        playback_env,
        "audio.flac",
        {
            "streams": [{"index": 0, "codec_name": "flac", "codec_type": "audio", "tags": {}}],
            "format": {"duration": "120.0", "format_name": "flac"},
        },
    )
    path = playback_env.lib_dir / "audio.flac"
    path.write_bytes(b"x" * 512)
    identity = source_identity(path.stat())
    async with playback_env.factory() as session:
        library = Library(name=f"lib-{uuid.uuid4().hex}", roots=[str(playback_env.lib_dir)])
        session.add(library)
        await session.flush()
        media = MediaFile(
            library_id=library.id,
            path=str(path),
            size=identity["size"],
            mtime_ns=path.stat().st_mtime_ns,
            source_identity=identity,
            container="flac",
            medium="music",
            last_scanned_at=T0,
        )
        session.add(media)
        await session.commit()
        file_id = media.id

    payload = (await open_playback(playback_env, file_id)).json()
    assert payload["media"]["has_video"] is False
    assert payload["media"]["has_audio"] is True
    assert payload["media"]["mimetype"] == "audio/flac"

    hls_id = (await open_playback(playback_env, file_id, mode="hls")).json()["playback_id"]
    stream = await playback_env.client.get(f"/api/playback/{hls_id}/stream.m3u8")
    assert stream.text.count("segments/") == 20
    segment = await playback_env.client.get(f"/api/playback/{hls_id}/segments/0.ts")
    assert segment.status_code == 200
    assert (await playback_env.client.get(f"/api/playback/{hls_id}/poster")).status_code == 404
    (playback_env.lib_dir / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\ncover")
    assert (await playback_env.client.delete(f"/api/playback/{hls_id}/")).status_code == 204
    reopened = (await open_playback(playback_env, file_id)).json()
    cover = await playback_env.client.get(reopened["urls"]["poster"])
    assert cover.status_code == 200
    assert cover.content == b"\x89PNG\r\n\x1a\ncover"
    assert cover.headers["content-type"] == "image/png"


@pytest.mark.asyncio
async def test_playlist_details() -> None:
    playlist = media_playlist(614.5, 6.0)
    lines = playlist.splitlines()
    assert "segments/0.ts" in lines
    assert lines[-3] == "#EXTINF:2.500000,"
    assert lines[-2] == "segments/102.ts"
    assert "#EXT-X-TARGETDURATION:6" in lines

    from server.playback.segments import subtitle_playlist

    cues = subtitle_playlist(0.5, "x0.vtt")
    assert "#EXT-X-TARGETDURATION:1" in cues


@pytest.mark.asyncio
async def test_external_subtitles(playback_env: PlaybackEnv) -> None:
    (playback_env.lib_dir / "movie.eng.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n2\n00:00:03,000 --> 00:00:04,500\nbye\n",
        encoding="utf-8",
    )
    (playback_env.lib_dir / "movie.de.ass").write_text("[Script Info]\nTitle: de\n", encoding="utf-8")
    _library_id, file_id = await seed_media(playback_env)
    payload = (await open_playback(playback_env, file_id)).json()
    tracks = payload["subtitles"]
    assert [track["id"] for track in tracks] == ["x0", "x1"]
    assert tracks[0]["url"].startswith(f"/api/playback/{payload['playback_id']}/subtitles/x0.vtt?v=")
    assert tracks[1]["url"].startswith(f"/api/playback/{payload['playback_id']}/subtitles/x1.ass?v=")
    playback_id = payload["playback_id"]

    vtt = await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/x0.vtt")
    assert vtt.status_code == 200
    assert "00:00:01.000 --> 00:00:02.000" in vtt.text
    ass = await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/x1.ass")
    assert ass.status_code == 200
    assert ass.text == "[Script Info]\nTitle: de\n"

    (playback_env.lib_dir / "movie.eng.srt").write_text("1\n00:00:09,000 --> 00:00:10,000\nno\n")
    replaced = await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/x0.vtt")
    assert replaced.status_code == 200
    assert "00:00:09.000 --> 00:00:10.000" in replaced.text

    assert (await playback_env.client.delete(f"/api/playback/{playback_id}/")).status_code == 204
    reopened = (await open_playback(playback_env, file_id)).json()
    assert reopened["playback_id"] == playback_id
    assert reopened["subtitles"][0]["url"] != tracks[0]["url"]
    assert (await playback_env.client.get(reopened["subtitles"][0]["url"])).status_code == 200

    assert (await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/x9.vtt")).status_code == 404


@pytest.mark.asyncio
async def test_embedded_subtitles_fonts(playback_env: PlaybackEnv) -> None:
    write_probe(
        playback_env,
        "movie.mkv",
        {
            "streams": [
                {"index": 0, "codec_name": "h264", "codec_type": "video", "tags": {}},
                {"index": 1, "codec_name": "ac3", "codec_type": "audio", "tags": {}},
                {
                    "index": 2,
                    "codec_name": "ass",
                    "codec_type": "subtitle",
                    "tags": {"language": "ger", "title": 'Evil "Title"\r\nInjected'},
                },
                {
                    "index": 3,
                    "codec_name": "hdmv_pgs_subtitle",
                    "codec_type": "subtitle",
                    "tags": {"language": "eng"},
                },
                {
                    "index": 4,
                    "codec_name": "ttf",
                    "codec_type": "attachment",
                    "tags": {"filename": "DejaVu Sans.ttf", "mimetype": "font/ttf"},
                },
            ],
            "format": {"duration": "600.0", "format_name": "matroska", "bit_rate": "5000000"},
        },
    )
    _library_id, file_id = await seed_media(playback_env)
    payload = (await open_playback(playback_env, file_id, mode="hls")).json()
    tracks = {track["id"]: track for track in payload["subtitles"]}
    assert tracks["e2"]["renderer"] == "assjs" and tracks["e2"]["delivery_format"] == "ass"
    assert tracks["e2"]["name"] == 'Evil "Title"Injected'
    assert tracks["e3"]["renderer"] == "bitmap" and "url" not in tracks["e3"]
    fonts = payload["fonts"]
    assert fonts[0]["url"] == f"/api/playback/{payload['playback_id']}/fonts/f4"
    playback_id = payload["playback_id"]

    master = await playback_env.client.get(f"/api/playback/{playback_id}/master.m3u8")
    assert "BANDWIDTH=" in master.text
    assert "\\r" not in master.text and "\\n" not in master.text

    ass = await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/e2.ass")
    assert ass.status_code == 200
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/subtitles/e2.vtt")).status_code == 404

    font = await playback_env.client.get(f"/api/playback/{playback_id}/fonts/f4")
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/ttf"
    assert font_names(font.content) == ["Fake Font"]
    assert json.loads(font.headers["X-Millwright-Font"])["families"] == ["Fake Font"]
    assert (await playback_env.client.get(f"/api/playback/{playback_id}/fonts/f2")).status_code == 404


def test_font_name_parsing() -> None:
    family = "DejaVu Sans".encode("utf-16-be")
    full = "DejaVu Sans Oblique".encode("utf-16-be")
    name_records = [
        (3, 1, 0x409, 1, len(family), 0),
        (3, 1, 0x409, 4, len(full), len(family)),
    ]
    strings = family + full
    table = (
        struct.pack(">HHH", 0, len(name_records), 6 + len(name_records) * 12)
        + b"".join(struct.pack(">HHHHHH", *record) for record in name_records)
        + strings
    )
    header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0) + struct.pack(">4sIII", b"name", 0, 28, len(table))
    assert font_names(header + table) == ["DejaVu Sans", "DejaVu Sans Oblique"]
    assert font_names(b"not a font") == []


@pytest.mark.asyncio
async def test_playback_capacity(playback_env: PlaybackEnv) -> None:
    playback_env.app.state.playback.config.max_grants = 1
    _library_id, file_id = await seed_media(playback_env)
    first = await open_playback(playback_env, file_id)
    assert first.status_code == 200
    repeated = await open_playback(playback_env, file_id)
    assert repeated.status_code == 200
    assert repeated.json()["playback_id"] == first.json()["playback_id"]
    _other_library, other_file = await seed_media(playback_env, "other.mkv")
    assert (await open_playback(playback_env, other_file)).status_code == 429


@pytest.mark.asyncio
async def test_grant_session_expiry(playback_env: PlaybackEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    now = utcnow()
    expires = now + timedelta(seconds=30)
    async with playback_env.factory() as db:
        row, token = await create_user_session(db, "operator", 1)
        row.expires_at = expires
        await db.commit()
    playback_env.client.headers.pop("Authorization")
    playback_env.client.headers.update({"Cookie": f"mw_session={token}", "X-Millwright-Request": "1"})
    for module in ("server.playback.service", "server.api.playback", "server.playback.responses"):
        monkeypatch.setattr(f"{module}.utcnow", lambda: now)
    try:
        _library_id, file_id = await seed_media(playback_env)
        payload = (await open_playback(playback_env, file_id)).json()
        session = playback_env.app.state.playback.session(payload["playback_id"])
        assert session is not None
        assert session.grant.expires_at == expires
        now += timedelta(seconds=29)
        assert (await playback_env.client.get(payload["urls"]["media"])).status_code == 200
        now += timedelta(seconds=2)
        assert (await playback_env.client.get(payload["urls"]["media"])).status_code == 401
    finally:
        playback_env.client.headers.pop("Cookie")
        playback_env.client.headers.pop("X-Millwright-Request")
        playback_env.client.headers["Authorization"] = "Bearer test-key"


def test_session_files_isolated() -> None:
    root = make_test_temp_dir("playback-own")
    service = PlaybackService(
        PlaybackConfig(),
        ffmpeg_bin=str(FAKE_FFMPEG),
        ffprobe_bin=str(FAKE_FFPROBE),
        files_dir=root / "playback",
    )
    session = service.files.create_session()
    artifact = session.new_artifact("seg")
    os.pwrite(artifact.fd, b"data", 0)
    # An unknown file is never touched by session cleanup.
    (root / "playback" / session.name / "foreign.bin").write_bytes(b"keep")
    session.close()
    assert (root / "playback" / session.name / "foreign.bin").read_bytes() == b"keep"
    assert not (root / "playback" / session.name / artifact.name).exists()

    session2 = service.files.create_session()
    original = root / "playback" / session2.name
    original.rename(root / "playback" / "moved")
    original.mkdir()
    (original / "keep").write_bytes(b"keep")
    try:
        artifact2 = session2.new_artifact("seg")
        os.pwrite(artifact2.fd, b"owned", 0)
        assert (root / "playback" / "moved" / artifact2.name).read_bytes() == b"owned"
    finally:
        session2.close()
    assert (original / "keep").read_bytes() == b"keep"
    assert not (root / "playback" / "moved" / artifact2.name).exists()

    real = root / "real"
    real.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        PlaybackFiles(link / "playback").create_session()
    assert not (real / "playback").exists()


def test_playback_directory_validation() -> None:
    root = make_test_temp_dir("playback-roots")
    with pytest.raises(PlaybackFilesError):
        validate_playback_dir(root / "playback", [root])
    with pytest.raises(PlaybackFilesError):
        validate_playback_dir(root, [root / "playback" / "inner"])
    validate_playback_dir(root / "playback", [root / "elsewhere"])


@pytest.mark.asyncio
async def test_unknown_files_retained(playback_env: PlaybackEnv) -> None:
    _library_id, file_id = await seed_media(playback_env)
    playback_id = (await open_playback(playback_env, file_id)).json()["playback_id"]
    service: PlaybackService = playback_env.app.state.playback
    session_obj = service.session(playback_id)
    assert session_obj is not None
    unknown = session_obj.files.root / session_obj.files.name / "unknown.bin"
    unknown.write_bytes(b"keep")
    await service.aclose()
    assert unknown.exists()


@pytest.mark.asyncio
async def test_encoder_chain_cache() -> None:
    files_dir = make_test_temp_dir("playback-catalog")
    service = PlaybackService(
        PlaybackConfig(),
        ffmpeg_bin=str(FAKE_FFMPEG),
        ffprobe_bin=str(FAKE_FFPROBE),
        files_dir=files_dir,
    )
    # QSV ranks higher but is absent from the fake's encoder list.
    assert await service.catalog.chain() == ["h264_nvenc", "libx264"]
    service.catalog.record_failure("h264_nvenc")
    assert await service.catalog.chain() == ["libx264"]
    service.catalog.record_success("h264_nvenc")
    assert await service.catalog.chain() == ["h264_nvenc", "libx264"]
    service.catalog.record_failure("libx264")
    assert await service.catalog.chain() == ["h264_nvenc", "libx264"]
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize(
    "trigger", ["logout", "close", "expired", "cancel", "repeat_cancel", "send_error", "cleanup_error"]
)
async def test_blocked_send_revocation(trigger: str, spec_version: str, monkeypatch: pytest.MonkeyPatch) -> None:
    files_dir = make_test_temp_dir("playback-guard")
    service = PlaybackService(
        PlaybackConfig(),
        ffmpeg_bin=str(FAKE_FFMPEG),
        ffprobe_bin=str(FAKE_FFPROBE),
        files_dir=files_dir,
    )
    grant = PlaybackGrant(
        playback_id="pl_test",
        media_file_id=1,
        library_id=1,
        path=files_dir / "movie.mkv",
        generation={},
        user_id=1,
        session_hash="hash-1",
        session_expires_at=None,
        expires_at=utcnow() + timedelta(seconds=3600),
        files_dir=files_dir,
    )
    session = PlaybackSession(
        grant=grant,
        config=service.config,
        segment_semaphore=service.segment_semaphore,
        delivery_semaphore=service.delivery_semaphore,
        mode="direct",
        summary=MediaSummary(container="mkv", duration_s=10.0, has_video=True, has_audio=True, mimetype="video/mp4"),
        files=service.files.create_session(),
    )
    service._sessions[grant.playback_id] = session
    tickets = WsTickets()

    closed, sending = asyncio.Event(), asyncio.Event()
    cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
    released = []
    send_error = RuntimeError("send failed")
    cleanup_error = RuntimeError("cleanup failed")

    async def content() -> AsyncGenerator[bytes]:
        try:
            yield b"first\n"
            yield b"second\n"
        finally:
            if trigger == "repeat_cancel":
                cleaning.set()
                await finish_cleanup.wait()
            closed.set()
            if trigger == "cleanup_error":
                raise cleanup_error

    app = Starlette()
    app.state.auth = SimpleNamespace(ws_tickets=tickets)
    app.state.playback = service

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/media",
            "headers": [],
            "app": app,
            "asgi": {"spec_version": spec_version},
        }
    )
    response = GuardedPlaybackResponse(
        request, grant, session, content(), media_type="video/mp4", headers={}, cleanup=lambda: released.append(True)
    )
    failing = asyncio.Event()
    clock_advanced = asyncio.Event()
    if trigger == "expired":
        wait = asyncio.wait

        async def wait_deadline(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            if timeout is not None:
                await clock_advanced.wait()
                return set(), set(tasks)
            return await wait(tasks, return_when=return_when)

        monkeypatch.setattr(asyncio, "wait", wait_deadline)

    async def send(message):
        if message["type"] == "http.response.body":
            sending.set()
            await failing.wait()
            raise send_error

    async def receive():
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(response(request.scope, receive, send))
    try:
        await asyncio.wait_for(sending.wait(), 2)
        if trigger == "logout":
            tickets.revoke_session("hash-1")
        elif trigger in {"close", "cleanup_error"}:
            service.close(grant.playback_id)
        elif trigger == "expired":
            monkeypatch.setattr("server.playback.responses.utcnow", lambda: grant.expires_at)
            clock_advanced.set()
        elif trigger in {"cancel", "repeat_cancel"}:
            task.cancel()
            if trigger == "repeat_cancel":
                await asyncio.wait_for(cleaning.wait(), 2)
                assert task in session.work.tasks
                task.cancel()
                finish_cleanup.set()
        else:
            failing.set()
        if trigger in {"cancel", "repeat_cancel"}:
            with pytest.raises(asyncio.CancelledError) as failure:
                await asyncio.wait_for(task, 2)
            assert not isinstance(failure.value, PlaybackStreamClosed)
        elif trigger == "send_error":
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value is send_error
        elif trigger == "cleanup_error":
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value is cleanup_error
        else:
            with pytest.raises(PlaybackStreamClosed):
                await asyncio.wait_for(task, 2)
        assert closed.is_set()
        assert released == [True]
        assert not tickets._sessions or all(not watchers for watchers in tickets._sessions.values())
    finally:
        finish_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        await service.aclose()
