from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from common.diagnostics import error_message
from common.process import ChildNotReapedError, drain, run_capture
from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.api.auth import PrincipalDep, current_principal
from server.fs.source import MediaSnapshot, authorized_library
from server.models import MediaFile, utcnow
from server.playback.artifacts import Artifact
from server.playback.encoding import PlaybackEncoding
from server.playback.media import CACHE_HEADERS, MediaRangeError, head_headers, parse_range, stream_range
from server.playback.posters import POSTER_MAX_BYTES, poster_argv, read_poster
from server.playback.responses import GuardedPlaybackResponse
from server.playback.segments import (
    bandwidth_bps,
    master_playlist,
    media_playlist,
    segment_count,
    subtitle_playlist,
)
from server.playback.service import PlaybackError, PlaybackService, PlaybackSession
from server.playback.subtitles import (
    EMBEDDED_MAX_BYTES,
    FONT_MAX_BYTES,
    SubtitleError,
    decode_text,
    extraction_argv,
    font_argv,
    font_by_id,
    font_metadata,
    read_external,
    srt_to_vtt,
    track_by_id,
)
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from server.models import Library
    from server.playback.grants import PlaybackGrant

router = APIRouter(prefix="/api", tags=["playback"])
LOG = logging.getLogger("server.api.playback")

_SLOT_WAIT_S = 120.0
_EXTRACT_TIMEOUT_S = 30.0
_POSTER_TIMEOUT_S = 60.0
_M3U8_TYPE = "application/x-mpegURL"


class OpenPlaybackBody(BaseModel):
    mode: Literal["direct", "hls"] = "direct"
    playback_id: str | None = None
    audio_stream_index: int | None = Field(default=None, ge=0, strict=True)
    subtitle_track_id: str | None = None


def _service(request: Request) -> PlaybackService:
    service: PlaybackService | None = getattr(request.app.state, "playback", None)
    if service is None:
        raise HTTPException(503, "playback unavailable")
    return service


def _authorize(request: Request, playback_id: str):
    service = _service(request)
    session = service.session(playback_id)
    if session is None:
        raise HTTPException(404, "playback session not found")
    grant = session.grant
    if session.work.closed.is_set() or utcnow() >= grant.expires_at:
        raise HTTPException(401, "invalid or expired playback grant")
    return service, session, grant


async def _revalidate_catalog(request: Request, grant: PlaybackGrant) -> Library:
    """Every delivery rechecks catalog authorization and the playback session."""
    db: async_sessionmaker = request.app.state.db

    async def read_catalog() -> Library:
        async with db() as db_session:
            row = (
                await db_session.execute(select(MediaFile).where(MediaFile.id == grant.media_file_id))
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(410, "file no longer exists")
            if row.library_id != grant.library_id or row.path != str(grant.path):
                raise HTTPException(410, "File moved to another library or path. Reopen playback.")
            library = await authorized_library(db_session, row.library_id, row)
            if library is None:
                raise HTTPException(410, "library is unavailable")
            return library

    # Keep the read and its session close together until both finish before
    # propagating cancellation.
    return await drain(asyncio.create_task(read_catalog()))


def _guarded_bytes(
    request: Request,
    session: PlaybackSession,
    grant: PlaybackGrant,
    data: bytes,
    media_type: str,
    headers: dict[str, str] | None = None,
    *,
    cache: bool = False,
) -> GuardedPlaybackResponse:
    async def content() -> AsyncGenerator[bytes]:
        yield data

    return GuardedPlaybackResponse(
        request,
        grant,
        session,
        content(),
        media_type=media_type,
        headers={**(CACHE_HEADERS if cache else {"Cache-Control": "private, no-store"}), **(headers or {})},
    )


@router.post("/files/{file_id}/playback", summary="Open or refresh a playback session")
async def open_playback(
    file_id: int,
    request: Request,
    principal: PrincipalDep,
    body: Annotated[OpenPlaybackBody | None, Body()] = None,
) -> JSONResponse:
    db: async_sessionmaker = request.app.state.db
    service = _service(request)
    payload = body or OpenPlaybackBody()

    async def revalidate() -> None:
        # A logout committed while the probe ran must not receive a grant.
        await current_principal(request)

    async with db() as db_session:
        file = (await db_session.execute(select(MediaFile).where(MediaFile.id == file_id))).scalar_one_or_none()
        if file is None:
            raise HTTPException(404, "file not found")
        library = await authorized_library(db_session, file.library_id, file)
        if library is None:
            raise HTTPException(404, "file not found")
        snapshot = MediaSnapshot.from_media(file)
    try:
        session = await service.open(
            snapshot,
            library,
            principal,
            mode=payload.mode,
            audio_stream_index=payload.audio_stream_index,
            subtitle_track_id=payload.subtitle_track_id,
            playback_id=payload.playback_id,
            source=Path(snapshot.path),
            revalidate=revalidate,
        )
    except OSError as exc:
        LOG.warning("Playback source %s could not be opened", snapshot.path, extra={"error": str(exc)})
        raise HTTPException(410, f"Source could not be opened: {exc}") from exc
    except PlaybackError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return JSONResponse(session.payload(), headers={"Cache-Control": "private, no-store"})


@router.get("/playback/{playback_id}/media", summary="Stream the source file")
@router.head("/playback/{playback_id}/media", include_in_schema=False)
async def get_media(playback_id: str, request: Request) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    await _revalidate_catalog(request, grant)
    mimetype = session.summary.mimetype

    try:
        size = (await wait_workspace_io(grant.path.stat)).st_size
    except OSError as exc:
        LOG.warning("Playback source %s could not be opened", grant.path, extra={"error": str(exc)})
        raise HTTPException(410, f"Source could not be opened: {exc}") from exc
    if request.method == "HEAD":
        return Response(headers=head_headers(mimetype, size), media_type=mimetype)
    try:
        start, end, status = parse_range(request.headers.get("range"), size)
    except MediaRangeError as exc:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{exc.size}"}) from exc

    length = end + 1 - start
    headers = {
        "Content-Type": mimetype,
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        **CACHE_HEADERS,
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    try:
        opening = asyncio.create_task(asyncio.to_thread(grant.path.open, "rb"))
        try:
            source = await asyncio.shield(opening)
        except asyncio.CancelledError:
            acquired = await drain(opening, propagate_cancel=False)
            await wait_workspace_io(acquired.close)
            raise
    except OSError as exc:
        raise HTTPException(410, f"Source could not be opened: {exc}") from exc

    async def close_source() -> None:
        await wait_workspace_io(source.close)

    async def content() -> AsyncGenerator[bytes]:
        chunks = stream_range(source.fileno(), start, end)

        def read_next() -> bytes | None:
            return next(chunks, None)

        try:
            while (chunk := await drain(asyncio.create_task(asyncio.to_thread(read_next)))) is not None:
                yield chunk
        finally:
            await close_source()

    return GuardedPlaybackResponse(
        request,
        grant,
        session,
        content(),
        status_code=status,
        media_type=mimetype,
        headers=headers,
        cleanup=close_source,
    )


@router.get("/playback/{playback_id}/master.m3u8", summary="HLS master playlist")
async def get_master(
    playback_id: str,
    request: Request,
    audio_stream_index: Annotated[int | None, Query(ge=0)] = None,
    subtitle_track_id: str | None = None,
) -> Response:
    _service, session, _grant = _authorize(request, playback_id)
    await _revalidate_catalog(request, _grant)
    audio_stream_index = _audio_stream(session, audio_stream_index)
    _subtitle_stream(session, subtitle_track_id)
    native = [track for track in session.tracks if track.renderer == "native" and track.delivery_format == "vtt"]
    body = master_playlist(
        [track.manifest("") for track in native],
        bandwidth_bps(session.config.segment_duration_s),
        audio_stream_index=audio_stream_index,
        subtitle_track_id=subtitle_track_id,
    )
    return _guarded_bytes(request, session, _grant, body.encode(), _M3U8_TYPE)


@router.get("/playback/{playback_id}/stream.m3u8", summary="HLS media playlist")
async def get_stream_playlist(
    playback_id: str,
    request: Request,
    audio_stream_index: Annotated[int | None, Query(ge=0)] = None,
    subtitle_track_id: str | None = None,
) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    await _revalidate_catalog(request, grant)
    audio_stream_index = _audio_stream(session, audio_stream_index)
    _subtitle_stream(session, subtitle_track_id)
    body = media_playlist(
        session.summary.duration_s,
        session.config.segment_duration_s,
        audio_stream_index=audio_stream_index,
        subtitle_track_id=subtitle_track_id,
    )
    return _guarded_bytes(request, session, grant, body.encode(), _M3U8_TYPE)


def _audio_stream(session: PlaybackSession, index: int | None) -> int | None:
    selected = session.summary.audio_stream_index if index is None else index
    if selected is not None and not any(track.stream_index == selected for track in session.summary.audio_tracks):
        raise HTTPException(422, "Audio track not found.")
    return selected


def _subtitle_stream(session: PlaybackSession, track_id: str | None) -> int | None:
    if track_id is None:
        return None
    track = track_by_id(session.tracks, track_id)
    if track is None or track.renderer != "bitmap" or not session.summary.has_video:
        raise HTTPException(422, "Bitmap subtitle track not found.")
    return int(track.source)


async def _prepare(request: Request, session: PlaybackSession, grant: PlaybackGrant, build) -> Artifact:
    """Run a bounded preparation task registered with the playback session."""
    service: PlaybackService = request.app.state.playback
    await _revalidate_catalog(request, grant)
    try:
        await wait_workspace_io(grant.path.stat)
    except OSError as exc:
        LOG.warning("Playback source %s could not be opened", grant.path, extra={"error": str(exc)})
        raise HTTPException(410, f"Source could not be opened: {exc}") from exc

    async def guarded() -> Artifact:
        try:
            async with asyncio.timeout(_SLOT_WAIT_S):
                await session.segment_semaphore.acquire()
        except TimeoutError:
            raise HTTPException(
                503, f"Playback conversion waited {_SLOT_WAIT_S:g} seconds for an encoder slot. Retry shortly."
            ) from None
        try:
            artifact = await build(service, session, grant.path)
            try:
                await _revalidate_catalog(request, grant)
            except BaseException:
                artifact.discard()
                raise
            return artifact
        finally:
            session.segment_semaphore.release()

    return await _race_closed(request, session, grant, guarded())


async def _race_closed(request: Request, session: PlaybackSession, grant: PlaybackGrant, coro) -> Artifact:
    tickets = request.app.state.auth.ws_tickets
    revoked = tickets.watch_session(grant.session_hash) if grant.session_hash is not None else None

    async def work() -> Artifact:
        started = False
        try:
            session.work.register_current()
            if (
                session.work.closed.is_set()
                or (revoked is not None and revoked.is_set())
                or utcnow() >= grant.expires_at
            ):
                raise HTTPException(401, "Playback grant expired or revoked.")
            started = True
            return await coro
        finally:
            if not started:
                coro.close()
            session.work.tasks.discard(asyncio.current_task())

    async def disconnected() -> None:
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    task = asyncio.create_task(work())
    waiters = [asyncio.create_task(session.work.closed.wait()), asyncio.create_task(disconnected())]
    if revoked is not None:
        waiters.append(asyncio.create_task(revoked.wait()))
    delivered = False
    try:
        done, _pending = await asyncio.wait(
            [task, *waiters],
            timeout=max(0, (grant.expires_at - utcnow()).total_seconds()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            if task.cancelled() and session.work.closed.is_set():
                raise HTTPException(409, "Playback session closed.")
            result = task.result()
            delivered = True
            return result
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise HTTPException(409, "playback session closed")
    finally:
        for waiter in waiters:
            waiter.cancel()
        if not task.done():
            task.cancel()

        async def cleanup() -> None:
            results = await asyncio.gather(task, *waiters, return_exceptions=True)
            try:
                for result in results:
                    if isinstance(result, ChildNotReapedError):
                        raise result
                if not delivered and not task.cancelled() and task.exception() is None:
                    task.result().discard()
            finally:
                if grant.session_hash is not None and revoked is not None:
                    tickets.unwatch_session(grant.session_hash, revoked)

        await drain(asyncio.create_task(cleanup()))


@router.get("/playback/{playback_id}/segments/{index}.ts", summary="Stream a prepared HLS segment")
async def get_segment(
    playback_id: str,
    index: int,
    request: Request,
    audio_stream_index: Annotated[int | None, Query(ge=0)] = None,
    subtitle_track_id: str | None = None,
) -> Response:
    service, session, grant = _authorize(request, playback_id)
    summary = session.summary
    audio_stream_index = _audio_stream(session, audio_stream_index)
    subtitle_stream_index = _subtitle_stream(session, subtitle_track_id)
    segment_s = session.config.segment_duration_s
    if index < 0 or index >= segment_count(summary.duration_s, segment_s):
        raise HTTPException(404, "segment out of range")
    encoding: PlaybackEncoding | None = None

    async def validate() -> None:
        try:
            await _revalidate_catalog(request, grant)
            await wait_workspace_io(grant.path.stat)
        except OSError as exc:
            LOG.warning("Playback source %s could not be opened", grant.path, extra={"error": str(exc)})
            raise HTTPException(410, f"Source could not be opened: {exc}") from exc

    async def prepare() -> Artifact:
        nonlocal encoding
        try:
            async with asyncio.timeout(_SLOT_WAIT_S):
                await session.delivery_semaphore.acquire()
        except TimeoutError:
            # A request waiting for a slot must not stop the current encode.
            raise HTTPException(
                503, f"Playback delivery waited {_SLOT_WAIT_S:g} seconds for a slot. Retry shortly."
            ) from None
        release_delivery_slot = True
        try:
            encoding = session.encoding
            try:
                await validate()
            except HTTPException as exc:
                if exc.status_code == 410 and encoding is not None:
                    await encoding.stop()
                raise
            if session.encoding is None:
                session.encoding = PlaybackEncoding(service, session)
            encoding = session.encoding
            artifact = await encoding.get(index, audio_stream_index, subtitle_stream_index, validate)
            artifact.on_close = session.delivery_semaphore.release
            release_delivery_slot = False
            try:
                await validate()
            except BaseException:
                artifact.discard()
                await encoding.stop()
                raise
            return artifact
        finally:
            if release_delivery_slot:
                session.delivery_semaphore.release()

    artifact = await _race_closed(request, session, grant, prepare())

    async def content() -> AsyncGenerator[bytes]:
        completed = False
        try:
            for chunk in artifact.read_chunks():
                yield chunk
            completed = True
        finally:
            artifact.discard()
            if not completed and encoding is not None:
                await encoding.stop()

    return GuardedPlaybackResponse(
        request,
        grant,
        session,
        content(),
        media_type="video/mpegts",
        headers={**CACHE_HEADERS, "X-Accel-Buffering": "no"},
        cleanup=artifact.discard,
    )


@router.get("/playback/{playback_id}/subtitles/{track_id}.m3u8", summary="HLS subtitle playlist")
async def get_subtitle_playlist(playback_id: str, track_id: str, request: Request) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    await _revalidate_catalog(request, grant)
    track = track_by_id(session.tracks, track_id)
    if track is None or track.renderer != "native" or track.delivery_format != "vtt":
        raise HTTPException(404, "subtitle track not found")
    body = subtitle_playlist(session.summary.duration_s, track.manifest("")["url"].rsplit("/", 1)[-1])
    return _guarded_bytes(request, session, grant, body.encode(), _M3U8_TYPE)


async def _convert(
    service: PlaybackService,
    session: PlaybackSession,
    argv: list[str],
    *,
    operation: str,
    suffix: str,
    timeout_s: float,
    max_bytes: int,
) -> Artifact:
    artifact = session.files.new_artifact(suffix)
    status = 422
    try:
        returncode, stderr = await run_capture(
            [service.ffmpeg_bin, *argv],
            timeout_s=timeout_s,
            output_fd=artifact.fd,
            max_bytes=max_bytes,
        )
        size = artifact.stat().st_size
        if returncode == 0 and 0 < size <= max_bytes:
            return artifact
        if returncode != 0:
            reason = (
                f"FFmpeg exited with code {returncode}. {stderr.decode('utf-8', errors='replace').strip()}".rstrip()
            )
        else:
            reason = f"FFmpeg produced {size} bytes. Expected 1 to {max_bytes} bytes."
    except TimeoutError:
        status = 504
        reason = f"FFmpeg exceeded the {timeout_s:g}-second limit."
    except (OSError, ValueError) as exc:
        reason = error_message(exc)
    except ChildNotReapedError:
        artifact.close()
        LOG.exception("%s for %s could not stop FFmpeg. Temporary files retained.", operation, session.grant.path)
        raise
    except BaseException:
        artifact.discard()
        raise
    artifact.discard()
    detail = f"{operation} failed: {reason}"
    LOG.warning("%s failed. Source: %s", operation, session.grant.path, extra={"error": reason})
    raise HTTPException(status, detail)


@router.get("/playback/{playback_id}/subtitles/{track_id}.{fmt}", summary="Fetch a subtitle track")
async def get_subtitle(playback_id: str, track_id: str, fmt: str, request: Request) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    await _revalidate_catalog(request, grant)
    track = track_by_id(session.tracks, track_id)
    if track is None or track.renderer not in {"native", "assjs"} or track.delivery_format != fmt:
        raise HTTPException(404, "subtitle track not found")

    if track.kind == "external":
        try:
            await wait_workspace_io(grant.path.stat)
        except OSError as exc:
            LOG.warning("Playback source %s could not be opened", grant.path, extra={"error": str(exc)})
            raise HTTPException(410, f"Source could not be opened: {exc}") from exc
        try:
            text = await drain(asyncio.create_task(asyncio.to_thread(read_external, grant.path.parent, track.source)))
            if track.source_format == "srt":
                text = srt_to_vtt(text)
        except (SubtitleError, OSError) as exc:
            LOG.warning("Subtitle %s for %s could not be read", track.source, grant.path, extra={"error": str(exc)})
            raise HTTPException(422, f"Subtitle could not be read: {exc}") from exc
        media_type = "text/vtt; charset=UTF-8" if fmt == "vtt" else "text/x-ssa; charset=UTF-8"
        return _guarded_bytes(request, session, grant, text.encode("utf-8"), media_type, cache=True)

    async def build(prepare_service: PlaybackService, prepare_session: PlaybackSession, source):
        return await _convert(
            prepare_service,
            prepare_session,
            extraction_argv(track, str(source), "pipe:1"),
            operation="Subtitle extraction",
            suffix="sub",
            timeout_s=_EXTRACT_TIMEOUT_S,
            max_bytes=EMBEDDED_MAX_BYTES,
        )

    artifact = await _prepare(request, session, grant, build)
    try:
        data = decode_text(artifact.read_bytes()).encode("utf-8")
    finally:
        artifact.discard()
    media_type = "text/vtt; charset=UTF-8" if fmt == "vtt" else "text/x-ssa; charset=UTF-8"
    return _guarded_bytes(request, session, grant, data, media_type, cache=True)


@router.get("/playback/{playback_id}/fonts/{font_id}", summary="Fetch an embedded subtitle font")
async def get_font(font_id: str, playback_id: str, request: Request) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    font = font_by_id(session.fonts, font_id)
    if font is None:
        raise HTTPException(404, "font not found")

    async def build(prepare_service: PlaybackService, prepare_session: PlaybackSession, source):
        return await _convert(
            prepare_service,
            prepare_session,
            font_argv(font.stream_index, str(source), "pipe:1"),
            operation="Font extraction",
            suffix="font",
            timeout_s=_EXTRACT_TIMEOUT_S,
            max_bytes=FONT_MAX_BYTES,
        )

    artifact = await _prepare(request, session, grant, build)
    try:
        data = artifact.read_bytes()
    finally:
        artifact.discard()
    try:
        metadata = font_metadata(data)
    except SubtitleError as exc:
        LOG.warning("Embedded font %s for %s could not be read", font_id, grant.path, extra={"error": str(exc)})
        raise HTTPException(422, f"Embedded font could not be read: {exc}") from exc
    return _guarded_bytes(
        request, session, grant, data, font.mimetype, {"X-Millwright-Font": json.dumps(metadata)}, cache=True
    )


@router.get("/playback/{playback_id}/poster", summary="Nearby poster or generated frame")
async def get_poster(playback_id: str, request: Request, v: str | None = None) -> Response:
    _service, session, grant = _authorize(request, playback_id)
    summary = session.summary
    poster = session.poster
    if v is not None and (poster is None or v != poster.version):
        raise HTTPException(410, "Poster changed. Reopen the player.")
    if poster is not None:
        await _revalidate_catalog(request, grant)
        try:
            await wait_workspace_io(grant.path.stat)
        except OSError as exc:
            LOG.warning("Playback source %s could not be opened", grant.path, extra={"error": str(exc)})
            raise HTTPException(410, f"Source could not be opened: {exc}") from exc
        try:
            data = await drain(asyncio.create_task(asyncio.to_thread(read_poster, grant.path.parent, poster)))
        except (OSError, ValueError) as exc:
            LOG.warning(
                "Poster %s for %s could not be read. Generating a frame.",
                poster.name,
                grant.path,
                extra={"error": str(exc)},
            )
            data = None
        if data is not None:
            return _guarded_bytes(request, session, grant, data, poster.mimetype, cache=v is not None)
    if not summary.has_video:
        raise HTTPException(404, "no poster for audio-only media")
    timestamp = max(0.0, min(summary.duration_s * 0.1, summary.duration_s - 1))

    async def build(prepare_service: PlaybackService, prepare_session: PlaybackSession, source):
        return await _convert(
            prepare_service,
            prepare_session,
            poster_argv(str(source), "pipe:1", timestamp),
            operation="Poster generation",
            suffix="poster",
            timeout_s=_POSTER_TIMEOUT_S,
            max_bytes=POSTER_MAX_BYTES,
        )

    artifact = await _prepare(request, session, grant, build)
    try:
        data = artifact.read_bytes()
    finally:
        artifact.discard()
    return _guarded_bytes(request, session, grant, data, "image/jpeg", cache=poster is None)


@router.delete("/playback/{playback_id}/", status_code=204, summary="Close a playback session")
async def close_playback(playback_id: str, request: Request) -> Response:
    service, _session, _grant = _authorize(request, playback_id)
    service.close(playback_id)
    return Response(status_code=204)
