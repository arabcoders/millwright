from __future__ import annotations

import asyncio
import logging

from common.capabilities import CapabilitiesError, binary_identity, probe_capabilities, verify_encoders
from common.diagnostics import error_message

LOG = logging.getLogger("server.playback.catalog")

# H.264 preview encoders in preference order: hardware first, CPU last.
HW_PRIORITY = ("h264_qsv", "h264_nvenc", "h264_amf", "h264_videotoolbox", "h264_vaapi")
CPU_ENCODER = "libx264"


class EncoderCatalog:
    """Server-local encoder chain, verified once per ffmpeg binary identity."""

    def __init__(self, ffmpeg_bin: str) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._lock = asyncio.Lock()
        self._identity: tuple[str, int, int, int, int] | None = None
        self._verified: frozenset[str] = frozenset()
        self._failed: set[str] = set()

    async def chain(self) -> list[str]:
        """Verified hardware encoders in preference order, then the CPU encoder."""
        identity = binary_identity(self._ffmpeg_bin)
        async with self._lock:
            if identity != self._identity:
                try:
                    caps = await probe_capabilities(self._ffmpeg_bin)
                    candidates = [name for name in HW_PRIORITY if name in caps.video_encoders]
                    self._verified = await verify_encoders(self._ffmpeg_bin, candidates)
                except (CapabilitiesError, OSError) as exc:
                    LOG.warning(
                        "Server FFmpeg capability probe for %s failed",
                        self._ffmpeg_bin,
                        extra={"error": error_message(exc)},
                    )
                    self._verified = frozenset()
                # A new binary invalidates prior runtime failures and successes.
                self._identity = identity
                self._failed.clear()
        chain = [name for name in HW_PRIORITY if name in self._verified and name not in self._failed]
        chain.append(CPU_ENCODER)
        return chain

    def record_failure(self, encoder: str) -> None:
        """Exclude the encoder until a success is recorded or the FFmpeg binary changes."""
        self._failed.add(encoder)

    def record_success(self, encoder: str) -> None:
        self._failed.discard(encoder)
