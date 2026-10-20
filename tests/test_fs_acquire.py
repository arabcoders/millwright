import errno
from pathlib import Path
from stat import S_ISLNK, S_ISREG

import pytest

from common.fs.acquire import entry_stat


def test_missing_entry(tmp_path: Path) -> None:
    assert entry_stat(tmp_path / "missing") is None


def test_symlink_metadata(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"content")
    link = tmp_path / "link"
    link.symlink_to(target)

    metadata = entry_stat(link)
    assert metadata is not None and S_ISLNK(metadata.st_mode)
    followed = entry_stat(link, follow_symlinks=True)
    assert followed is not None and S_ISREG(followed.st_mode)
    assert followed.st_size == len(b"content")

    target.unlink()
    metadata = entry_stat(link)
    assert metadata is not None and S_ISLNK(metadata.st_mode)
    assert entry_stat(link, follow_symlinks=True) is None


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO, errno.ENOTDIR])
def test_lookup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int) -> None:
    def unavailable(path: Path, *, follow_symlinks: bool = True):
        raise OSError(error, "storage unavailable")

    monkeypatch.setattr(Path, "stat", unavailable)
    with pytest.raises(OSError) as failure:
        entry_stat(tmp_path / "file")
    assert failure.value.errno == error
