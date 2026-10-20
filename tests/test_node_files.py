from __future__ import annotations

from pathlib import Path

import pytest

import node.files as node_files
from common.fs.effects import copy_file
from node.files import partial_output, publish_output


@pytest.mark.parametrize("copy", [False, True])
def test_subtitle_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, copy: bool) -> None:
    destination = tmp_path / "output.srt"
    with partial_output(destination) as output:
        output.path.write_bytes(b"subtitle bytes")
        source = output.path
        if copy:
            monkeypatch.setattr(node_files, "publish", copy_file)
        publish_output(output, destination)
        assert not source.exists()
        assert destination.read_bytes() == b"subtitle bytes"
    assert destination.read_bytes() == b"subtitle bytes"
