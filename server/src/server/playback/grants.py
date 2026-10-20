from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

PLAYBACK_PREFIX = "pl_"
PLAYBACK_CONTRACT = "continuous-hls-v1"


@dataclass(kw_only=True, slots=True)
class PlaybackGrant:
    playback_id: str
    media_file_id: int
    library_id: int
    path: Path
    generation: dict[str, int]
    user_id: int | None
    session_hash: str | None
    session_expires_at: datetime | None
    expires_at: datetime
    files_dir: Path


def playback_token(
    *,
    key: bytes,
    media_file_id: int,
    library_id: int,
    path: Path,
    generation: dict[str, int],
    user_id: int | None,
    session_hash: str | None,
    segment_duration_s: float,
) -> str:
    identity = json.dumps(
        [
            PLAYBACK_CONTRACT,
            media_file_id,
            library_id,
            str(path),
            generation,
            user_id,
            session_hash,
            segment_duration_s,
        ],
        sort_keys=True,
    ).encode()
    return PLAYBACK_PREFIX + hmac.digest(key, identity, "sha256").hex()[:32]
