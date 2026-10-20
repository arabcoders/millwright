from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from common.media.tools import ProbeError, run_ffprobe


@pytest.mark.asyncio
async def test_probe_sidecar(tmp_path: Path, mock_ffprobe: AsyncMock) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"source")
    raw = {"streams": [], "format": {"duration": "123"}}
    Path(f"{path}.probe.json").write_text(json.dumps(raw))
    assert await run_ffprobe("ffprobe", path) == raw


@pytest.mark.asyncio
async def test_probe_failure(tmp_path: Path, mock_ffprobe: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "movie.mkv"
    monkeypatch.setenv("FAKE_FFPROBE_FAIL", path.name)
    with pytest.raises(ProbeError) as failed:
        await run_ffprobe("ffprobe", path)
    assert failed.value.tool == "ffprobe"
    assert failed.value.stdout == "this is not json {{{"
    assert str(path) in str(failed.value)
