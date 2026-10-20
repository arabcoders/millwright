from __future__ import annotations

from datetime import UTC, datetime

from server.api.flows import _flow_dict
from server.api.libraries import _library_dict
from server.api.nodes import _node_dict
from server.api.notifications import _target_dict
from server.api.trash import _entry_dict
from server.models import Flow, Library, NodeRow, NotificationTarget, TrashEntry


STAMP = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
EXPECTED = "2026-01-02T03:04:05.678901Z"


def test_resource_dates() -> None:
    library = Library(id=1, name="library", roots=[])
    node = NodeRow(id=2, name="node", display_name="Node", token_hash="secret")
    flow = Flow(id=3, name="flow", version=1, definition='{"entry":"start","nodes":[],"edges":[]}')
    target = NotificationTarget(name="target", url="json://example.test")
    entry = TrashEntry(
        id="entry",
        library_id=1,
        original_path="/library/file.mkv",
        library_root="/library",
        suffix=".mkv",
        size_bytes=1,
        mtime_ns=1,
        trashed_at=STAMP,
        purge_at=STAMP,
    )
    for resource in (library, node, flow, target, entry):
        resource.created_at = STAMP
        resource.updated_at = STAMP

    assert _library_dict(library)["created_at"] == EXPECTED
    assert _node_dict(node)["updated_at"] == EXPECTED
    assert _flow_dict(flow)["created_at"] == EXPECTED
    assert _target_dict(target)["updated_at"] == EXPECTED
    serialized_entry = _entry_dict(entry, None)
    assert serialized_entry["created_at"] == EXPECTED
    assert serialized_entry["updated_at"] == EXPECTED
    assert serialized_entry["trashed_at"] == EXPECTED
