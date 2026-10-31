"""Contained, symlink-safe access to local attachment stores."""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ATTACHMENT_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def validate_attachment_id(value: object) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("invalid attachment id")
    if any(char not in ATTACHMENT_ID_CHARS for char in value):
        raise ValueError("invalid attachment id")
    return value


def _component(value: object) -> str:
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ValueError("invalid attachment path component")
    if "/" in value or "\x00" in value:
        raise ValueError("invalid attachment path component")
    return value


def _root_path(root: str | os.PathLike[str]) -> Path:
    return Path(root).absolute()


def _relative_path(root: Path, path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if ".." in candidate.parts:
        raise ValueError("attachment path escapes its store")
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("attachment path escapes its store") from exc
    if not candidate.parts:
        raise ValueError("invalid attachment path")
    for part in candidate.parts:
        _component(part)
    return candidate


@contextmanager
def _directory(
    root: str | os.PathLike[str], parts: tuple[str, ...] = (), *, create: bool = False
) -> Iterator[int]:
    root_path = _root_path(root)
    if create:
        root_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            _component(part)
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def attachment_path(
    root: str | os.PathLike[str], container_id: str | None, filename: str
) -> Path:
    parts = () if container_id is None else (validate_attachment_id(container_id),)
    _component(filename)
    return _root_path(root).joinpath(*parts, filename)


def find_attachment(
    root: str | os.PathLike[str], container_id: str | None, filename: str
) -> Path | None:
    path = attachment_path(root, container_id, filename)
    parts = () if container_id is None else (container_id,)
    try:
        with _directory(root, parts) as directory_fd:
            info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("attachment is not a regular file")
    return path if info.st_size > 0 else None


def write_attachment(
    root: str | os.PathLike[str],
    container_id: str | None,
    filename: str,
    data: bytes,
    *,
    overwrite: bool = True,
) -> Path:
    path = attachment_path(root, container_id, filename)
    parts = () if container_id is None else (container_id,)
    with _directory(root, parts, create=True) as directory_fd:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as output:
            if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                raise ValueError("attachment is not a regular file")
            output.write(data)
    return path


def _read_at(directory_fd: int, filename: str, max_bytes: int | None) -> bytes:
    fd = os.open(
        filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
    )
    with os.fdopen(fd, "rb") as blob:
        if not stat.S_ISREG(os.fstat(blob.fileno()).st_mode):
            raise ValueError("attachment is not a regular file")
        return blob.read(None if max_bytes is None else max_bytes + 1)


def read_attachment(
    root: str | os.PathLike[str], value: object, max_bytes: int | None = None
) -> bytes:
    attachment_id = validate_attachment_id(value)
    with _directory(root) as directory_fd:
        data = _read_at(directory_fd, attachment_id, max_bytes)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"attachment larger than {max_bytes} bytes")
    return data


def existing_attachment_path(
    root: str | os.PathLike[str], path: str | os.PathLike[str]
) -> Path | None:
    root_path = _root_path(root)
    try:
        relative = _relative_path(root_path, path)
        with _directory(root_path, relative.parts[:-1]) as directory_fd:
            info = os.stat(
                relative.parts[-1], dir_fd=directory_fd, follow_symlinks=False
            )
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return root_path / relative


def read_attachment_path(
    root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    max_bytes: int | None = None,
) -> bytes:
    root_path = _root_path(root)
    relative = _relative_path(root_path, path)
    with _directory(root_path, relative.parts[:-1]) as directory_fd:
        data = _read_at(directory_fd, relative.parts[-1], max_bytes)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"attachment larger than {max_bytes} bytes")
    return data
