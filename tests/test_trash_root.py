from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.helpers import create_test_app as create_app
from server.core.config import ServerConfig
from server.nodehub.hub import NodeHub
from tests.helpers import make_test_temp_dir


def test_hub_rejects_symlink() -> None:
    workspace = make_test_temp_dir("trash-workspace")
    external = make_test_temp_dir("trash-external")
    marker = external / "marker"
    marker.write_text("keep", encoding="utf-8")
    root = workspace.parent / "hub-trash-root"
    root.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError):
        NodeHub(None, workspace_dir=workspace, trash_dir=root)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert root.is_symlink()


def test_app_rejects_symlink() -> None:
    workspace = make_test_temp_dir("app-trash-workspace")
    external = make_test_temp_dir("app-trash-external")
    marker = external / "marker"
    marker.write_text("keep", encoding="utf-8")
    root = workspace.parent / "app-trash-root"
    root.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError):
        create_app(async_sessionmaker(), config=ServerConfig(workspace_dir=str(workspace), trash_dir=str(root)))

    assert marker.read_text(encoding="utf-8") == "keep"
    assert root.is_symlink()
