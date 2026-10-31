"""Dirfd-safe preparation for the diary-sync broker's fixed mount sources."""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import stat
from pathlib import Path

from scripts.safe_io import DIR_FLAGS, open_dir

TRACKS = ("spond", "vigilo", "skolemelding")


def require_unix_socket(path: Path) -> None:
    mode = os.lstat(path).st_mode
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"not a Unix socket: {path}")


def _secure_dir(parent_fd: int, name: str, uid: int, gid: int, mode: int) -> int:
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _secure_state(parent_fd: int, name: str, uid: int, gid: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, 0o664, dir_fd=parent_fd)
    except FileExistsError:
        fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"{}\n")
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o664)
    finally:
        os.close(fd)


def prepare_memory(memory: Path, uid: int, gid: int) -> None:
    """Create and pin the broker's mount sources without dereferencing links.

    The sticky, setgid memory directory lets the agent create its ordinary state
    files but prevents it from renaming or unlinking the owner-held bind sources.
    """
    memory_fd = open_dir(memory, create=True)
    try:
        os.fchown(memory_fd, uid, gid)
        os.fchmod(memory_fd, 0o3775)
        for track in TRACKS:
            output_fd = _secure_dir(memory_fd, track, uid, gid, 0o2775)
            os.close(output_fd)
            _secure_state(memory_fd, f"{track}-state.json", uid, gid)
    finally:
        os.close(memory_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--bus", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--group", required=True)
    args = parser.parse_args(argv)
    require_unix_socket(args.bus)
    prepare_memory(
        args.memory,
        pwd.getpwnam(args.owner).pw_uid,
        grp.getgrnam(args.group).gr_gid,
    )


if __name__ == "__main__":
    main()
