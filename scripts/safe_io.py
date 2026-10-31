"""Small dirfd-relative filesystem operations that never follow symlinks."""
from __future__ import annotations

import os
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _components(path: Path) -> list[str]:
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    parts = list(path.parts[1:])
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"path contains an unsafe component: {path}")
    return parts


def open_dir(path: Path, *, create: bool = False, mode: int = 0o755) -> int:
    """Open an absolute directory path component-by-component with O_NOFOLLOW."""
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in _components(Path(path)):
            if create:
                try:
                    os.mkdir(part, mode, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_dir(path: Path, *, mode: int = 0o755) -> None:
    fd = open_dir(path, create=True, mode=mode)
    os.close(fd)


def append_text(path: Path, text: str) -> None:
    """Append to one file without following its parent components or final name."""
    path = Path(path)
    if not path.name or path.name in (".", ".."):
        raise ValueError(f"not a file path: {path}")
    parent_fd = open_dir(path.parent, create=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
        fd = os.open(path.name, flags, 0o664, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(text)
