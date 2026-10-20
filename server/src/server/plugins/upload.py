from __future__ import annotations

import ctypes
import io
import os
import stat
import tomllib
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from common.fs.acquire import DIRECTORY_FLAGS, _check_entry, _check_parent, directory_fd, regular_fd
from pydantic import ValidationError

from server.plugins.manifest import PluginManifest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 100 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
PluginSnapshot = dict[str, tuple[int, ...]]


class UploadError(ValueError):
    """The message is shown to the uploader."""


class UploadSizeError(UploadError):
    """Decompressed content exceeds the size limit. Returned as HTTP 413."""


def archive_manifest(archive: zipfile.ZipFile) -> tuple[str, PluginManifest]:
    _check_entry_names(archive.namelist())
    if sum(entry.file_size for entry in archive.infolist()) > MAX_DECOMPRESSED_BYTES:
        msg = f"plugin archive decompresses beyond {MAX_DECOMPRESSED_BYTES} bytes"
        raise UploadSizeError(msg)
    prefix = _root_prefix(archive)
    try:
        with archive.open(f"{prefix}plugin.toml") as source:
            raw = source.read(MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise UploadSizeError("plugin manifest is too large")
        manifest = PluginManifest.model_validate(tomllib.loads(raw.decode("utf-8")))
    except (KeyError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        msg = f"invalid plugin manifest: {exc}"
        raise UploadError(msg) from exc
    for entry in archive.infolist():
        path = PurePosixPath(entry.filename)
        if prefix and path.parts[0] != prefix.rstrip("/"):
            raise UploadError("archive must contain a single plugin root")
        mode = entry.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise UploadError("plugin archive contains a non-regular entry")
    return prefix, manifest


def _check_entry_names(names: Sequence[str]) -> None:
    seen: set[str] = set()
    files: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        normalized = path.as_posix()
        if not name or path.is_absolute() or ".." in path.parts or "\\" in name or normalized == ".":
            msg = f"archive entry escapes the plugin root: {name}"
            raise UploadError(msg)
        if normalized in seen or name.rstrip("/") != normalized:
            msg = f"archive entry has a duplicate or non-normalized path: {name}"
            raise UploadError(msg)
        seen.add(normalized)
        if not name.endswith("/"):
            files.add(normalized)
    for name in seen:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise UploadError("archive file shadows a directory")


def _root_prefix(archive: zipfile.ZipFile) -> str:
    names = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
    if "plugin.toml" in names:
        return ""
    tops = sorted({PurePosixPath(name).parts[0] for name in names if name})
    if len(tops) == 1 and f"{tops[0]}/plugin.toml" in names:
        return f"{tops[0]}/"
    if any(name.endswith("/plugin.toml") for name in names):
        raise UploadError("archive must contain a single plugin root")
    raise UploadError("archive contains no plugin.toml")


def _stat_fields(metadata: os.stat_result) -> tuple[int, ...]:
    return tuple(
        getattr(metadata, key)
        for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
    )


def plugin_snapshot(fd: int) -> PluginSnapshot:
    snapshot: PluginSnapshot = {}

    def visit(parent: int, prefix: str) -> None:
        for name in sorted(os.listdir(parent)):
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            relative = prefix + name
            snapshot[relative] = _stat_fields(metadata)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    _check_entry(Path(name), parent, child)
                    visit(child, relative + "/")
                    _check_entry(Path(name), parent, child)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise UploadError("uploaded plugin contains a symlink, hardlink, or non-regular entry")

    visit(fd, "")
    return snapshot


def directory_manifest(fd: int) -> PluginManifest:
    with regular_fd("plugin.toml", fd) as manifest_fd:
        if os.fstat(manifest_fd).st_nlink != 1:
            raise UploadError("plugin manifest contains a hardlink")
        with os.fdopen(os.dup(manifest_fd), "rb") as source:
            raw = source.read(MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise UploadSizeError("plugin manifest is too large")
        manifest = PluginManifest.model_validate(tomllib.loads(raw.decode("utf-8")))
        _check_entry(Path("plugin.toml"), fd, manifest_fd)
        return manifest


def move_plugin_directory(root: Path, root_fd: int, source: str, destination: str, directory_fd: int) -> None:
    _check_parent(root / source, root_fd)
    _check_entry(Path(source), root_fd, directory_fd)
    # POSIX rename can replace an unrelated empty directory. Require Linux's
    # no-replace operation. Hardlink publication cannot move directory trees.
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(root_fd, os.fsencode(source), root_fd, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)
    _check_entry(Path(destination), root_fd, directory_fd)
    _check_parent(root / destination, root_fd)


def remove_plugin_directory(root: Path, root_fd: int, name: str, directory_fd: int, snapshot: PluginSnapshot) -> None:
    _check_parent(root / name, root_fd)
    _check_entry(Path(name), root_fd, directory_fd)
    if plugin_snapshot(directory_fd) != snapshot:
        raise UploadError("plugin directory changed before cleanup")

    def remove(parent_fd: int, prefix: str) -> None:
        for leaf in sorted(os.listdir(parent_fd)):
            relative = prefix + leaf
            metadata = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if relative not in snapshot or _stat_fields(metadata) != snapshot[relative]:
                raise UploadError("unexpected entry appeared during plugin cleanup")
            _check_parent(root / name, root_fd)
            _check_entry(Path(name), root_fd, directory_fd)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(leaf, DIRECTORY_FLAGS, dir_fd=parent_fd)
                try:
                    _check_entry(Path(leaf), parent_fd, child)
                    if _stat_fields(os.fstat(child)) != snapshot[relative]:
                        raise UploadError("plugin cleanup directory changed")
                    remove(child, relative + "/")
                    _check_entry(Path(leaf), parent_fd, child)
                    _check_parent(root / name, root_fd)
                    _check_entry(Path(name), root_fd, directory_fd)
                    os.rmdir(leaf, dir_fd=parent_fd)
                finally:
                    os.close(child)
            else:
                os.unlink(leaf, dir_fd=parent_fd)

    remove(directory_fd, "")
    _check_parent(root / name, root_fd)
    _check_entry(Path(name), root_fd, directory_fd)
    os.rmdir(name, dir_fd=root_fd)
    _check_parent(root / name, root_fd)


@dataclass(kw_only=True)
class PluginInstallation:
    manifest: PluginManifest
    path: Path
    root_fd: int
    installed_fd: int
    installed_snapshot: PluginSnapshot
    previous_fd: int | None
    previous_snapshot: PluginSnapshot | None
    handles: ExitStack

    def commit(self) -> None:
        _check_parent(self.path, self.root_fd)
        _check_entry(self.path, self.root_fd, self.installed_fd)
        if plugin_snapshot(self.installed_fd) != self.installed_snapshot:
            raise UploadError("published plugin changed before commit")
        if self.previous_fd is not None and self.previous_snapshot is not None:
            remove_plugin_directory(
                self.path.parent, self.root_fd, f".{self.path.name}.replaced", self.previous_fd, self.previous_snapshot
            )

    def rollback(self) -> None:
        root, name = self.path.parent, self.path.name
        partial = f".{name}.partial"
        _check_parent(self.path, self.root_fd)
        _check_entry(self.path, self.root_fd, self.installed_fd)
        if plugin_snapshot(self.installed_fd) != self.installed_snapshot:
            raise UploadError("published plugin changed before rollback")
        if self.previous_fd is not None and self.previous_snapshot is not None:
            _check_entry(Path(f".{name}.replaced"), self.root_fd, self.previous_fd)
            if plugin_snapshot(self.previous_fd) != self.previous_snapshot:
                raise UploadError("previous plugin changed before rollback")
        move_plugin_directory(root, self.root_fd, name, partial, self.installed_fd)
        if self.previous_fd is not None and self.previous_snapshot is not None:
            if plugin_snapshot(self.previous_fd) != self.previous_snapshot:
                raise UploadError("previous plugin changed before rollback")
            move_plugin_directory(root, self.root_fd, f".{name}.replaced", name, self.previous_fd)
        remove_plugin_directory(root, self.root_fd, partial, self.installed_fd, self.installed_snapshot)

    def close(self) -> None:
        self.handles.close()


def install_plugin(
    data: bytes, uploaded_dir: Path, *, bundled_taken: Callable[[str], bool], replace: bool = True
) -> tuple[PluginManifest, Path]:
    installation = install_plugin_pending(data, uploaded_dir, bundled_taken=bundled_taken, replace=replace)
    try:
        installation.commit()
        return installation.manifest, installation.path
    finally:
        installation.close()


def install_plugin_pending(
    data: bytes, uploaded_dir: Path, *, bundled_taken: Callable[[str], bool], replace: bool = True
) -> PluginInstallation:
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadSizeError("plugin archive is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadError("upload is not a zip archive") from exc
    with archive:
        prefix, manifest = archive_manifest(archive)
        if bundled_taken(manifest.id):
            msg = f"plugin id {manifest.id!r} is already used by a bundled plugin"
            raise UploadError(msg)
        return _extract(archive, prefix, uploaded_dir / manifest.id, manifest, replace=replace)


def _copy_entry(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, written: int
) -> tuple[int, tuple[int, ...]]:
    # The parent path points to an open directory descriptor, not the configured upload root.
    entry_bytes = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as dst, archive.open(entry) as src:
        while chunk := src.read(_COPY_CHUNK):
            entry_bytes += len(chunk)
            if written + entry_bytes > MAX_DECOMPRESSED_BYTES:
                msg = f"plugin archive decompresses beyond {MAX_DECOMPRESSED_BYTES} bytes"
                raise UploadSizeError(msg)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
        snapshot = _stat_fields(os.fstat(dst.fileno()))
        if _stat_fields(os.stat(target, follow_symlinks=False)) != snapshot:
            raise UploadError("plugin extraction file changed during creation")
    return entry_bytes, snapshot


def _extract(
    archive: zipfile.ZipFile, prefix: str, plugin_dir: Path, manifest: PluginManifest, *, replace: bool
) -> PluginInstallation:
    root, name = plugin_dir.parent, plugin_dir.name
    partial, replaced = f".{name}.partial", f".{name}.replaced"
    with ExitStack() as handles:
        root_fd = handles.enter_context(directory_fd(root, create=True))
        _check_parent(plugin_dir, root_fd)
        for directory in (partial, replaced):
            try:
                os.stat(directory, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise UploadError("Partial plugin upload or recovery copy needs inspection before another upload.")
        previous_fd: int | None = None
        previous_snapshot: PluginSnapshot | None = None
        try:
            previous_fd = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            pass
        else:
            handles.callback(os.close, previous_fd)
            if not replace:
                msg = f"plugin already exists: {name}"
                raise UploadError(msg)
            previous_snapshot = plugin_snapshot(previous_fd)
            if directory_manifest(previous_fd).id != name or "plugin.py" not in previous_snapshot:
                raise UploadError("existing directory does not contain a valid plugin installation")
            _check_entry(plugin_dir, root_fd, previous_fd)
        os.mkdir(partial, mode=0o700, dir_fd=root_fd)
        partial_stat = os.stat(partial, dir_fd=root_fd, follow_symlinks=False)
        partial_fd = os.open(partial, DIRECTORY_FLAGS, dir_fd=root_fd)
        handles.callback(os.close, partial_fd)
        current = os.fstat(partial_fd)
        if (current.st_dev, current.st_ino) != (partial_stat.st_dev, partial_stat.st_ino):
            raise UploadError("Partial plugin upload directory changed while opening it.")
        _check_parent(plugin_dir, root_fd)
        decompressed = 0
        files: PluginSnapshot = {}
        directories: dict[str, tuple[int, int]] = {}
        for entry in archive.infolist():
            relative = PurePosixPath(entry.filename).relative_to(PurePosixPath(prefix))
            if not relative.parts:
                continue
            parent_fd = os.dup(partial_fd)
            try:
                parent_parts: list[str] = []
                for part in relative.parts if entry.is_dir() else relative.parts[:-1]:
                    parent_parts.append(part)
                    key = "/".join(parent_parts)
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        if key not in directories:
                            raise UploadError("unexpected directory occupies plugin extraction path") from None
                    else:
                        created = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                        directories[key] = (created.st_dev, created.st_ino)
                    child = os.open(part, DIRECTORY_FLAGS, dir_fd=parent_fd)
                    metadata = os.fstat(child)
                    identity = (metadata.st_dev, metadata.st_ino)
                    if key in directories and directories[key] != identity:
                        os.close(child)
                        raise UploadError("plugin extraction directory changed")
                    directories[key] = identity
                    os.close(parent_fd)
                    parent_fd = child
                if not entry.is_dir():
                    target = Path(f"/proc/self/fd/{parent_fd}") / relative.name
                    written, file_snapshot = _copy_entry(archive, entry, target, decompressed)
                    decompressed += written
                    files[relative.as_posix()] = file_snapshot
                _check_entry(Path(partial), root_fd, partial_fd)
                _check_parent(plugin_dir, root_fd)
            finally:
                os.close(parent_fd)
        installed_snapshot = plugin_snapshot(partial_fd)
        if (
            set(installed_snapshot) != files.keys() | directories.keys()
            or any(installed_snapshot[key] != snapshot for key, snapshot in files.items())
            or any(installed_snapshot[key][:2] != identity for key, identity in directories.items())
        ):
            raise UploadError("plugin extraction contains an unexpected or changed entry")
        if directory_manifest(partial_fd) != manifest:
            raise UploadError("extracted plugin manifest changed")
        if previous_fd is not None and plugin_snapshot(previous_fd) != previous_snapshot:
            raise UploadError("existing plugin generation changed")
        try:
            if previous_fd is not None:
                move_plugin_directory(root, root_fd, name, replaced, previous_fd)
            move_plugin_directory(root, root_fd, partial, name, partial_fd)
        except BaseException:
            # Restore only the original directory opened above. Leave both copies
            # for inspection if the destination exists or the parent has changed.
            if previous_fd is not None:
                try:
                    _check_parent(plugin_dir, root_fd)
                    _check_entry(Path(replaced), root_fd, previous_fd)
                    if plugin_snapshot(previous_fd) == previous_snapshot:
                        try:
                            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            move_plugin_directory(root, root_fd, replaced, name, previous_fd)
                except Exception:
                    pass
            raise
        return PluginInstallation(
            manifest=manifest,
            path=plugin_dir,
            root_fd=root_fd,
            installed_fd=partial_fd,
            installed_snapshot=installed_snapshot,
            previous_fd=previous_fd,
            previous_snapshot=previous_snapshot,
            handles=handles.pop_all(),
        )
