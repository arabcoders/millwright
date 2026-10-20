from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

import pytest

from common.fs import effects
from common.fs.effects import copy_file, move_file
from tests.helpers import fd_path, make_test_temp_dir


def test_copies_long_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    destination = tmp_path / ("m" * (os.pathconf(tmp_path, "PC_NAME_MAX") - 4) + ".mkv")
    source.write_bytes(b"payload")
    monkeypatch.setattr(
        "common.fs.effects.fcntl.ioctl", lambda *_: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported"))
    )
    copy_file(source, destination)
    assert destination.read_bytes() == b"payload" and source.read_bytes() == b"payload"
    assert not list(tmp_path.glob(".*.part"))


def test_preserves_copy_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    source, destination, partial = tmp_path / "source", tmp_path / "destination", tmp_path / ".123.partial"
    source.write_bytes(b"payload")
    partial.mkdir()
    (partial / "file").write_bytes(b"old-bytes")
    monkeypatch.setattr("common.fs.acquire.uuid.uuid4", lambda: SimpleNamespace(hex="123"))
    with pytest.raises(FileExistsError):
        copy_file(source, destination)
    assert source.read_bytes() == b"payload" and (partial / "file").read_bytes() == b"old-bytes"
    assert not destination.exists()


def test_copy_no_clobber() -> None:
    root = make_test_temp_dir("fileops-copy-existing")
    source, destination = root / "source", root / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        copy_file(source, destination)
    assert destination.read_bytes() == b"old"


def test_restore_no_clobber() -> None:
    root = make_test_temp_dir("fileops-no-clobber")
    claim, destination = root / "claim", root / "destination"
    claim.write_bytes(b"new")
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        move_file(claim, destination, overwrite=False)
    assert claim.read_bytes() == b"new" and destination.read_bytes() == b"old"


def test_moves_with_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_test_temp_dir("fileops-move")
    destination_parent = root / "destination-parent"
    destination_parent.mkdir()
    source, destination = root / "source", destination_parent / "destination"
    source.write_bytes(b"payload")
    real_link = os.link

    def link(src: str | Path, dst: str | Path, **kwargs) -> None:
        if (
            fd_path(src, kwargs.get("src_dir_fd")) == source
            and fd_path(dst, kwargs.get("dst_dir_fd")).parent == destination.parent
        ):
            raise OSError(errno.EXDEV, "xdev")
        real_link(src, dst, **kwargs)

    monkeypatch.setattr("common.fs.effects.os.link", link)
    move_file(source, destination)
    assert destination.read_bytes() == b"payload" and not source.exists()


def test_move_parent_confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, detached, outside = tmp_path / "parent", tmp_path / "detached", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    source, destination = tmp_path / "source", parent / "destination"
    source.write_bytes(b"payload")
    destination.write_bytes(b"old-bytes")
    redirected = outside / destination.name
    redirected.write_bytes(b"outside")
    real_link = os.link

    def swap_parent(src: str | Path, dst: str | Path, **kwargs) -> None:
        assert fd_path(src, kwargs.get("src_dir_fd")) == source
        assert fd_path(dst, kwargs.get("dst_dir_fd")).parent == parent
        real_link(src, dst, **kwargs)
        parent.rename(detached)
        parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr("common.fs.effects.os.link", swap_parent)
    move_file(source, destination)
    assert not source.exists()
    assert redirected.read_bytes() == b"outside"
    assert (detached / destination.name).read_bytes() == b"payload"
    assert not list(detached.glob(".*.part"))


@pytest.mark.parametrize("error", [errno.EINVAL, errno.ENOTSUP])
@pytest.mark.parametrize("restore", [False, True])
def test_unsupported_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int, restore: bool) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"original")
    monkeypatch.setattr(
        "common.fs.effects.os.link", lambda *_, **__: (_ for _ in ()).throw(OSError(error, "unsupported"))
    )
    operation = move_file if restore else copy_file
    with pytest.raises(OSError):
        operation(source, destination, overwrite=False)
    assert source.read_bytes() == b"original" and not destination.exists()
    assert not list(tmp_path.glob(".*.part"))


@pytest.mark.parametrize("restore", [False, True])
def test_interrupted_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"original")

    def interrupt(_source: object, target: BinaryIO) -> None:
        target.write(b"incomplete")
        assert not destination.exists()
        raise OSError("interrupted")

    monkeypatch.setattr(
        "common.fs.effects.fcntl.ioctl", lambda *_: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported"))
    )
    monkeypatch.setattr(
        "common.fs.effects.os.link", lambda *_, **__: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross device"))
    )
    monkeypatch.setattr("common.fs.effects.shutil.copyfileobj", interrupt)
    operation = move_file if restore else copy_file
    with pytest.raises(OSError):
        operation(source, destination, overwrite=False)
    assert source.read_bytes() == b"original" and not destination.exists()
    assert not list(tmp_path.glob(".*.part"))


def test_moves_across_disks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"original")
    original_link = os.link

    def link(src: Path, dst: Path, **kwargs) -> None:
        src_path = fd_path(src, kwargs.get("src_dir_fd"))
        if src_path == source:
            raise OSError(errno.EXDEV, "cross device")
        assert src_path.parent.parent == destination.parent
        assert src_path.name == "file" and not destination.exists()
        original_link(src, dst, **kwargs)

    monkeypatch.setattr("common.fs.effects.os.link", link)
    move_file(source, destination, overwrite=False)
    assert destination.read_bytes() == b"original" and not source.exists()


@pytest.mark.parametrize("overwrite", [False, True])
def test_move_sync_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")

    def fail_sync(path: Path) -> None:
        assert path == destination
        assert source.read_bytes() == destination.read_bytes() == b"payload"
        # A sync error must not be interpreted as an unsupported link or rename.
        raise OSError(errno.EXDEV, "sync failed")

    monkeypatch.setattr(effects, "copy_file", lambda *_args, **_kwargs: pytest.fail("unexpected copy"))
    with pytest.raises(OSError):
        move_file(source, destination, overwrite=overwrite, sync_destination=fail_sync)
    assert source.read_bytes() == destination.read_bytes() == b"payload"
    assert not list(tmp_path.glob(".*.part"))


@pytest.mark.parametrize("sync", [False, True])
def test_move_late_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync: bool) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    original_link = os.link

    def racing_link(src: Path, dst: Path, **kwargs) -> None:
        destination.write_bytes(b"old-bytes")
        original_link(src, dst, **kwargs)

    def sync_destination(_path: Path) -> None:
        pytest.fail("syncing an existing file")

    monkeypatch.setattr("common.fs.effects.os.link", racing_link)
    with pytest.raises(FileExistsError):
        move_file(source, destination, overwrite=False, sync_destination=sync_destination if sync else None)
    assert source.read_bytes() == b"payload" and destination.read_bytes() == b"old-bytes"


def test_replacement_failure_preserves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    destination.write_bytes(b"old-bytes")
    monkeypatch.setattr(
        "common.fs.effects.os.replace", lambda *_, **__: (_ for _ in ()).throw(OSError("publish failed"))
    )
    with pytest.raises(OSError):
        move_file(source, destination, sync_destination=lambda _: pytest.fail("unpublished sync"))
    assert source.read_bytes() == b"payload" and destination.read_bytes() == b"old-bytes"
    assert not list(tmp_path.glob(".*.part"))
