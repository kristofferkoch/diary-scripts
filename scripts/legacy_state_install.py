"""Prepare and migrate legacy collector state into its fixed runtime root."""
from __future__ import annotations

import os
import pwd
import grp
import stat
from pathlib import Path

from scripts.diary_sync_install import _secure_dir, _secure_state
from scripts.safe_io import open_dir

TRACKS = ("spond", "vigilo", "skolemelding")
SOURCE_ROOT = Path("/home/kek/diary/memory")
DESTINATION_ROOT = Path("/var/lib/diary/legacy-state")


class MigrationConflict(RuntimeError):
    """Source and destination contain different data and neither is newer."""


def _regular(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"legacy state entry is not a regular file: {path}")
    return info


def _copy_missing(source: Path, destination: Path, uid: int, gid: int) -> str:
    source_info = _regular(source)
    try:
        destination_info = _regular(destination)
    except FileNotFoundError:
        destination_info = None
    if destination_info is not None:
        if source.read_bytes() == destination.read_bytes():
            return "identical"
        if destination_info.st_mtime_ns >= source_info.st_mtime_ns:
            return "preserved-newer"
        raise MigrationConflict(f"conflicting legacy state: {destination}")

    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.migrate.tmp")
    try:
        destination_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(source_info.st_mode),
        )
        try:
            while True:
                chunk = os.read(source_fd, 128 * 1024)
                if not chunk:
                    break
                os.write(destination_fd, chunk)
            os.fchown(destination_fd, uid, gid)
            os.fchmod(destination_fd, stat.S_IMODE(source_info.st_mode))
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        current = os.fstat(source_fd)
        if (current.st_size, current.st_mtime_ns) != (
            source_info.st_size, source_info.st_mtime_ns
        ):
            raise RuntimeError(f"legacy source changed during migration: {source}")
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            return _copy_missing(source, destination, uid, gid)
        parent_fd = open_dir(destination.parent)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(source_fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return "copied"


def migrate_legacy_state(
    source_root: Path = SOURCE_ROOT,
    destination_root: Path = DESTINATION_ROOT,
    *,
    uid: int,
    gid: int,
) -> dict[str, int]:
    """Copy missing legacy state without replacing or hiding destination data."""
    destination_fd = open_dir(destination_root, create=True)
    counts = {"copied": 0, "identical": 0, "preserved-newer": 0}
    try:
        os.fchown(destination_fd, uid, gid)
        os.fchmod(destination_fd, 0o2775)
        for track in TRACKS:
            os.close(_secure_dir(destination_fd, track, uid, gid, 0o2775))
    finally:
        os.close(destination_fd)

    for track in TRACKS:
        candidates = [(source_root / f"{track}-state.json", destination_root / f"{track}-state.json")]
        source_dir = source_root / track
        try:
            source_info = os.lstat(source_dir)
        except FileNotFoundError:
            source_info = None
        if source_info is not None:
            if not stat.S_ISDIR(source_info.st_mode):
                raise OSError(f"legacy source directory is not a directory: {source_dir}")
            candidates.extend(
                (path, destination_root / track / path.name)
                for path in sorted(source_dir.glob("*.jsonl"))
            )
        for source, destination in candidates:
            try:
                result = _copy_missing(source, destination, uid, gid)
            except FileNotFoundError:
                continue
            counts[result] += 1

    destination_fd = open_dir(destination_root)
    try:
        for track in TRACKS:
            _secure_state(destination_fd, f"{track}-state.json", uid, gid)
    finally:
        os.close(destination_fd)
    return counts


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("legacy state migration must run as root")
    counts = migrate_legacy_state(
        uid=pwd.getpwnam("kek").pw_uid,
        gid=grp.getgrnam("diary").gr_gid,
    )
    print("legacy state migration: " + " ".join(f"{key}={value}" for key, value in counts.items()))


if __name__ == "__main__":
    main()
