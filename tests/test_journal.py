from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.journal import append_message, read_messages
from common.protocol.messages import ActionReport, Finished, Progress


def test_appends_messages(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    attempt.mkdir(parents=True)
    messages = [
        Progress(job_id=1, attempt_id=1, percent=25).model_dump(mode="json"),
        Finished(job_id=1, attempt_id=1, result="failed", log_path="jobs/1/attempts/1/ffmpeg.log").model_dump(
            mode="json"
        ),
        ActionReport(job_id=1, attempt_id=1).model_dump(mode="json"),
    ]

    for message in messages:
        append_message(root, "jobs/1/attempts/1/ffmpeg.log", message)

    assert read_messages(root, "jobs/1/attempts/1/ffmpeg.log") == messages
    assert (attempt / "job.ndjson").read_text() == "".join(
        json.dumps(message, separators=(",", ":")) + "\n" for message in messages
    )


def test_ignores_trailing_line(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    journal = root / "jobs/1/attempts/1/job.ndjson"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"type":"progress","percent":10}\n{"type":"finished"')

    assert read_messages(root, "jobs/1/attempts/1/ffmpeg.log") == [{"type": "progress", "percent": 10}]


def test_skips_malformed_line(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    journal = root / "jobs/1/attempts/1/job.ndjson"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"type":"progress"}\nnot-json\n{"type":"action_report","ok":true}\n')

    assert read_messages(root, "jobs/1/attempts/1/ffmpeg.log") == [
        {"type": "progress"},
        {"type": "action_report", "ok": True},
    ]


def test_missing_journal(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_messages(tmp_path / "workspace", "jobs/1/attempts/1/ffmpeg.log")


def test_rejects_redirect(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    outside = tmp_path / "outside.ndjson"
    attempt.mkdir(parents=True)
    outside.write_text("outside\n")
    (attempt / "job.ndjson").symlink_to(outside)

    with pytest.raises(OSError):
        append_message(root, "jobs/1/attempts/1/ffmpeg.log", {"type": "progress"})
    assert outside.read_text() == "outside\n"


def test_accepts_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    attempt = root / "jobs/1/attempts/1"
    journal = attempt / "job.ndjson"
    linked = root / "linked.ndjson"
    attempt.mkdir(parents=True)
    linked.write_text('{"type":"progress"}\n')
    journal.hardlink_to(linked)

    message = {"type": "progress", "percent": 10}
    append_message(root, "jobs/1/attempts/1/ffmpeg.log", message)
    assert read_messages(root, "jobs/1/attempts/1/ffmpeg.log") == [{"type": "progress"}, message]
    assert linked.read_bytes() == journal.read_bytes()
