from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.capabilities import MkvTools
from common.media.probe import probe_from_raw
from common.protocol import OfferedJob
from node.agent import Agent, AgentConfig

ORIGINAL = b"original" * 128

RAW = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3", "channels": 6},
    ],
    "format": {"duration": "60"},
}


def inspection(tmp_path: Path, kind: str, tool: Path) -> tuple[Agent, OfferedJob, Path]:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    attempt.mkdir(parents=True)
    source = root / "jobs/1/source.mkv"
    source.write_bytes(ORIGINAL)
    relative = str(source.relative_to(root))
    probe = probe_from_raw(source, source.stat().st_size, RAW).model_copy(update={"file_path": relative})
    agent = Agent(
        AgentConfig(
            node_id="node-a",
            token="test",
            server_url="ws://unused",
            workspace_dir=str(root),
            ffmpeg_bin=str(tool),
            ffprobe_bin=str(Path(__file__).parent / "fakes/ffprobe.py"),
            mkv_tools=MkvTools(mkvpropedit=str(tool)),
            min_free_gb=0,
        )
    )
    offer = OfferedJob.model_validate(
        {
            "job_id": 1,
            "attempt_id": 1,
            "kind": kind,
            "source_path": relative,
            "output_path": "jobs/1/attempts/1/output.mkv",
            "log_path": "jobs/1/attempts/1/ffmpeg.log",
            "probe": probe,
            "healthcheck_hwaccel": False,
            "min_free_gb": 0,
        }
    )
    return agent, offer, source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic", "exit_code", "healthy"), [("", 0, True), ("broken audio", 0, False), ("tool failed", 1, None)]
)
async def test_node_decode(tmp_path: Path, diagnostic: str, exit_code: int, healthy: bool | None) -> None:
    argv_path = tmp_path / "argv.json"
    tool = tmp_path / "decode.py"
    tool.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
        f"Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"print({diagnostic!r}, file=sys.stderr)\nprint('progress=end')\nraise SystemExit({exit_code})\n"
    )
    tool.chmod(0o755)
    agent, offer, source = inspection(tmp_path, "healthcheck", tool)
    try:
        result = await asyncio.wait_for(agent._execute_job(offer), 10)
        assert result["kind"] == "healthcheck"
        assert result["output_path"] is None and result["output_probe"] is None
        if healthy is None:
            assert result["result"] == "failed" and result["health"] is None
        else:
            assert result["result"] == "succeeded" and result["health"]["healthy"] is healthy
        argv = json.loads(argv_path.read_text())
        assert argv[argv.index("-i") + 1] == str(source)
        assert "0:0" in argv and "0:a?" in argv
        assert source.read_bytes() == ORIGINAL
        log = source.parents[2] / offer.log_path
        assert "healthcheck: result" in log.read_text()
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "oversized"), [(0, False), (2, False), (0, True)])
async def test_node_statistics(tmp_path: Path, exit_code: int, oversized: bool) -> None:
    tool = tmp_path / "stats.py"
    tool.write_text(
        "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
        f"path=Path(sys.argv[-1])\npath.write_bytes(path.read_bytes()+{'b"x"*2048' if oversized else 'b"-stats"'})\n"
        f"print('statistics diagnostic',file=sys.stderr)\nraise SystemExit({exit_code})\n"
    )
    tool.chmod(0o755)
    agent, offer, source = inspection(tmp_path, "mkv_stats", tool)
    try:
        await asyncio.wait_for(agent._run_job(offer), 10)
        messages = []
        while not agent._outbox.empty():
            messages.append(agent._outbox.get_nowait())
        result = next(message for message in messages if message["type"] == "finished")
        assert result["kind"] == "mkv_stats"
        assert source.read_bytes() == ORIGINAL
        success = exit_code == 0 and not oversized
        assert result["result"] == ("succeeded" if success else "failed")
        if success:
            output = Path(agent._config.workspace_dir) / result["output_path"]
            assert output.read_bytes() == ORIGINAL + b"-stats"
            assert result["output_probe"]["file_path"] == offer.output_path
            assert result["raw_probe"]["streams"]
        else:
            assert result["output_path"] is None and result["output_probe"] is None
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("node_reserve", "server_reserve"), [(1, 0), (0, 1)])
async def test_statistics_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node_reserve: int, server_reserve: int
) -> None:
    marker = tmp_path / "edited"
    tool = tmp_path / "editor.py"
    tool.write_text(f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    tool.chmod(0o755)
    agent, offer, source = inspection(tmp_path, "mkv_stats", tool)
    agent._config.min_free_gb = node_reserve
    offer.min_free_gb = server_reserve
    monkeypatch.setattr("node.files.shutil.disk_usage", lambda _: SimpleNamespace(free=500_000_000))
    try:
        with pytest.raises(ValueError, match="Not enough free space"):
            await asyncio.wait_for(agent._execute_job(offer), 10)
        assert source.read_bytes() == ORIGINAL
        assert not marker.exists()
        assert not (Path(agent._config.workspace_dir) / offer.output_path).exists()
    finally:
        await agent.aclose()
