from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from common.fs.identity import source_identity
from common.media.tools import ProbeError, run_ffprobe
from common.process import ChildNotReapedError

from server.models import utcnow
from server.playback.artifacts import PlaybackFiles, PlaybackFilesError, SessionFiles, validate_playback_dir
from server.playback.catalog import EncoderCatalog
from server.playback.grants import PlaybackGrant, playback_token
from server.playback.posters import Poster, discover_poster
from server.playback.segments import MediaSummary
from server.playback.subtitles import (
    FontAttachment,
    SubtitleTrack,
    discover_embedded,
    discover_external,
    discover_fonts,
)
from server.serialize import iso_utc
from server.workspace import wait_workspace_io

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from server.core.auth import Principal
    from server.fs.source import MediaSnapshot
    from server.models import Library, MediaFile
    from server.playback.encoding import PlaybackEncoding
    from server.settings import PlaybackConfig

LOG = logging.getLogger("server.playback")

_PROBE_TIMEOUT_S = 20.0
WORK_CLOSED = "Playback work closed."


class PlaybackError(Exception):
    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.status = status


class WorkSet:
    """Active encoding and preparation tasks for one playback session."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    def register_current(self) -> None:
        """Closing the session cancels this request's work."""
        task = asyncio.current_task()
        if task is None or self.closed.is_set():
            raise PlaybackError("playback session is closing", status=409)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def cancel_and_wait(self) -> None:
        """Wait for every task's cancellation cleanup before removing it."""
        self.closed.set()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel(WORK_CLOSED)
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result
        self.tasks.clear()


@dataclass(kw_only=True)
class PlaybackSession:
    grant: PlaybackGrant
    config: PlaybackConfig
    segment_semaphore: asyncio.Semaphore
    delivery_semaphore: asyncio.Semaphore
    mode: str
    summary: MediaSummary
    files: SessionFiles
    tracks: list[SubtitleTrack] = field(default_factory=list)
    fonts: list[FontAttachment] = field(default_factory=list)
    audio_stream_index: int | None = None
    subtitle_track_id: str | None = None
    poster: Poster | None = None
    work: WorkSet = field(default_factory=WorkSet)
    encoding: PlaybackEncoding | None = None

    def payload(self) -> dict:
        grant = self.grant
        base = f"/api/playback/{grant.playback_id}"
        selection = {"audio_stream_index": self.audio_stream_index, "subtitle_track_id": self.subtitle_track_id}
        query = urlencode({key: value for key, value in selection.items() if value is not None})
        return {
            "playback_id": grant.playback_id,
            "expires_at": iso_utc(grant.expires_at),
            "mode": self.mode,
            "media": {
                "container": self.summary.container,
                "mimetype": self.summary.mimetype,
                "duration_s": self.summary.duration_s,
                "has_video": self.summary.has_video,
                "has_audio": self.summary.has_audio,
                "video_codec": self.summary.video_codec,
                "video_profile": self.summary.video_profile,
                "video_pix_fmt": self.summary.video_pix_fmt,
                "width": self.summary.width,
                "height": self.summary.height,
            },
            # Resume follows the path-and-size generation.
            "generation": grant.generation,
            "subtitles": [track.manifest(base) for track in self.tracks],
            "fonts": [font.manifest(base) for font in self.fonts],
            "audio_tracks": [asdict(track) for track in self.summary.audio_tracks],
            "audio_stream_index": self.audio_stream_index,
            "subtitle_track_id": self.subtitle_track_id,
            "urls": {
                "media": f"{base}/media",
                "master": f"{base}/master.m3u8" + (f"?{query}" if query else ""),
                "poster": f"{base}/poster" + (f"?v={self.poster.version}" if self.poster is not None else ""),
            },
        }


class PlaybackService:
    def __init__(
        self,
        config: PlaybackConfig,
        *,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        files_dir: Path,
        forbidden_roots: Sequence[Path] = (),
    ) -> None:
        self.config = config
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.files_dir = files_dir
        validate_playback_dir(files_dir, list(forbidden_roots))
        self.catalog = EncoderCatalog(ffmpeg_bin)
        self.files = PlaybackFiles(files_dir)
        # API-key playback has no session hash, so its bearer IDs need a separate secret.
        self._token_key = secrets.token_bytes(32)
        self._sessions: dict[str, PlaybackSession] = {}
        self._draining: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(config.max_concurrent_segments)
        self.delivery_semaphore = asyncio.Semaphore(config.max_concurrent_segments)
        self._closed = False

    @property
    def segment_semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    def configure(self, config: PlaybackConfig) -> None:
        if config.max_concurrent_segments != self.config.max_concurrent_segments:
            # Existing sessions keep the same conversion limit.
            self._semaphore = asyncio.Semaphore(config.max_concurrent_segments)
            self.delivery_semaphore = asyncio.Semaphore(config.max_concurrent_segments)
        self.config = config

    def session(self, playback_id: str) -> PlaybackSession | None:
        return self._sessions.get(playback_id)

    async def open(
        self,
        file: MediaFile | MediaSnapshot,
        library: Library,
        principal: Principal,
        *,
        mode: str,
        source: Path,
        audio_stream_index: int | None = None,
        subtitle_track_id: str | None = None,
        playback_id: str | None = None,
        revalidate: Callable[[], Awaitable[None]] | None = None,
    ) -> PlaybackSession:
        # Check capacity before probing media or creating temporary files.
        if self._closed:
            raise PlaybackError("Playback is shutting down.", status=503)
        await self.purge()
        previous = self._sessions.get(playback_id) if playback_id is not None else None
        config = previous.config if previous is not None else self.config
        segment_semaphore = previous.segment_semaphore if previous is not None else self.segment_semaphore
        delivery_semaphore = previous.delivery_semaphore if previous is not None else self.delivery_semaphore
        validate_playback_dir(self.files_dir, [Path(root) for root in library.roots])

        generation = source_identity(await wait_workspace_io(source.stat))

        token = playback_token(
            key=self._token_key,
            media_file_id=file.id,
            library_id=library.id,
            path=source,
            generation=generation,
            user_id=principal.user_id,
            session_hash=principal.session_hash,
            segment_duration_s=config.segment_duration_s,
        )
        if playback_id is not None and playback_id != token:
            raise PlaybackError("playback session does not match this file", status=409)
        playback_id = token
        existing = self._sessions.get(playback_id)
        summary = None
        if existing is None:
            if len(self._sessions) >= self.config.max_grants:
                raise PlaybackError("Too many active playback sessions. Try again later.", status=429)
            try:
                raw = await run_ffprobe(self.ffprobe_bin, source, timeout_s=_PROBE_TIMEOUT_S)
            except ProbeError as exc:
                raise PlaybackError("could not probe media", status=422) from exc
            summary = MediaSummary.from_probe(raw)
            if summary.duration_s <= 0:
                raise PlaybackError("media has no playable duration", status=422)

        if revalidate is not None:
            await revalidate()
        if self._closed or (existing is not None and existing.work.closed.is_set()):
            raise PlaybackError("Playback session closed.", status=409)
        # A concurrent open may have completed while this request probed.
        existing = self._sessions.get(playback_id)
        media = existing.summary if existing is not None else summary
        if media is None:
            raise PlaybackError("Playback session closed.", status=409)
        selected_audio = media.audio_stream_index if audio_stream_index is None else audio_stream_index
        if selected_audio is not None and not any(track.stream_index == selected_audio for track in media.audio_tracks):
            raise PlaybackError("Audio track not found.", status=422)
        if selected_audio != media.audio_stream_index:
            mode = "hls"
        tracks = existing.tracks if existing is not None else [*discover_external(source), *discover_embedded(raw)]
        if subtitle_track_id is not None:
            if not media.has_video or not any(
                track.track_id == subtitle_track_id and track.renderer == "bitmap" for track in tracks
            ):
                raise PlaybackError("Bitmap subtitle track not found.", status=422)
            mode = "hls"
        expires = utcnow() + timedelta(seconds=config.grant_ttl_s)
        if principal.session_expires_at is not None:
            expires = min(expires, principal.session_expires_at)
        if existing is not None:
            grant = existing.grant
            if (
                grant.media_file_id != file.id
                or grant.user_id != principal.user_id
                or grant.session_hash != principal.session_hash
                or grant.generation != generation
            ):
                raise PlaybackError("playback session does not match this file", status=409)
            while existing.encoding is not None and (
                existing.audio_stream_index != selected_audio
                or existing.subtitle_track_id != subtitle_track_id
                or mode != "hls"
            ):
                encoding = existing.encoding
                await encoding.aclose()
                if self._closed or existing.work.closed.is_set() or self._sessions.get(playback_id) is not existing:
                    raise PlaybackError("Playback session closed.", status=409)
                if existing.encoding is encoding:
                    existing.encoding = None
            grant.expires_at = expires
            grant.session_expires_at = principal.session_expires_at
            existing.mode = mode
            existing.audio_stream_index = selected_audio
            existing.subtitle_track_id = subtitle_track_id
            return existing

        if summary is None:
            raise PlaybackError("Playback session closed.", status=409)
        if len(self._sessions) >= self.config.max_grants:
            raise PlaybackError("Too many active playback sessions. Try again later.", status=429)

        try:
            files = self.files.create_session()
        except PlaybackFilesError as exc:
            raise PlaybackError(str(exc), status=503) from exc
        grant = PlaybackGrant(
            playback_id=playback_id,
            media_file_id=file.id,
            library_id=library.id,
            path=source,
            generation=generation,
            user_id=principal.user_id,
            session_hash=principal.session_hash,
            session_expires_at=principal.session_expires_at,
            expires_at=expires,
            files_dir=self.files_dir / files.name,
        )
        session = PlaybackSession(
            grant=grant,
            config=config,
            segment_semaphore=segment_semaphore,
            delivery_semaphore=delivery_semaphore,
            mode=mode,
            summary=summary,
            files=files,
            tracks=tracks,
            fonts=discover_fonts(raw),
            audio_stream_index=selected_audio,
            subtitle_track_id=subtitle_track_id,
            poster=discover_poster(source),
        )
        self._sessions[grant.playback_id] = session
        return session

    def close(self, playback_id: str) -> PlaybackGrant | None:
        session = self._sessions.get(playback_id)
        if session is not None:
            self._close_session(session)
            return session.grant
        return None

    def revoke_session(self, session_hash: str) -> list[PlaybackGrant]:
        revoked = []
        for session in list(self._sessions.values()):
            if session.grant.session_hash == session_hash:
                self._close_session(session)
                revoked.append(session.grant)
        return revoked

    async def purge(self) -> None:
        now = utcnow()
        for session in list(self._sessions.values()):
            if session.grant.expires_at <= now:
                self._close_session(session)

    def _close_session(self, session: PlaybackSession) -> None:
        session.work.closed.set()
        self._sessions.pop(session.grant.playback_id, None)
        task = asyncio.create_task(self._drain_session(session))
        self._draining.add(task)
        task.add_done_callback(self._drained)

    def _drained(self, task: asyncio.Task) -> None:
        self._draining.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOG.error("Playback cleanup failed. Temporary files were retained.", exc_info=task.exception())

    async def _drain_session(self, session: PlaybackSession) -> None:
        # Active encodes keep their child processes and files until they finish.
        await session.work.cancel_and_wait()
        if session.encoding is not None:
            await session.encoding.aclose()
        with SessionFilesCloser(session.files):
            pass

    async def aclose(self) -> None:
        self._closed = True
        sessions, self._sessions = self._sessions, {}
        results = await asyncio.gather(
            *(self._drain_session(session) for session in sessions.values()),
            *self._draining,
            return_exceptions=True,
        )
        for index, result in enumerate(results):
            if isinstance(result, ChildNotReapedError) or (index < len(sessions) and isinstance(result, Exception)):
                raise result


class SessionFilesCloser:
    """Close a session's files, reporting rather than raising."""

    def __init__(self, files: SessionFiles) -> None:
        self._files = files

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        try:
            self._files.close()
        except OSError:
            LOG.warning("Playback file cleanup failed.", exc_info=True)
        return False
