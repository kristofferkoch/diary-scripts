"""Prepare and migrate legacy collector state into its fixed runtime root."""
from __future__ import annotations

import json
import os
import pwd
import grp
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from scripts.diary_sync_install import _secure_dir, _secure_state
from scripts.safe_io import DIR_FLAGS, open_dir

TRACKS = ("spond", "vigilo", "skolemelding")
SOURCE_ROOT = Path("/home/kek/diary/memory")
DESTINATION_ROOT = Path("/var/lib/diary/legacy-state")
RUNTIME_PREFIX = "/var/lib/diary/legacy-state"
LEGACY_PREFIXES = (
    "/home/kek/diary/memory",
    "/etc/diary/workspace/memory",
)


class MigrationConflict(RuntimeError):
    """Source and destination contain different data and neither is newer."""


def _regular(parent_fd: int, name: str) -> os.stat_result:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"legacy state entry is not a regular file: {name}")
    return info


def _read_regular(parent_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _regular(parent_fd, name)
        chunks = []
        while True:
            chunk = os.read(fd, 128 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short write during legacy migration")
        offset += written


def _map_legacy_path(value: str, track: str) -> str:
    for prefix in LEGACY_PREFIXES:
        marker = f"{prefix}/{track}/"
        if value.startswith(marker):
            relative = value[len(marker):]
            parts = PurePosixPath(relative).parts
            if not relative or any(part in ("", ".", "..") for part in parts):
                raise OSError("unsafe legacy attachment path")
            return f"{RUNTIME_PREFIX}/{track}/{relative}"
    return value


def _rewrite_paths(value: Any, track: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _map_legacy_path(item, track) if key == "path" and isinstance(item, str)
            else _rewrite_paths(item, track)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_paths(item, track) for item in value]
    return value


def _normalise_content(data: bytes, name: str, track: str) -> bytes:
    if not name.endswith(".jsonl"):
        return data
    records = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise OSError("legacy JSONL record is not an object")
        records.append(_rewrite_paths(record, track))
    return "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")


def _publish_replace(parent_fd: int, name: str, data: bytes, mode: int, uid: int, gid: int) -> None:
    temporary = f".{name}.{os.getpid()}.rewrite.tmp"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode, dir_fd=parent_fd,
    )
    try:
        _write_all(fd, data)
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _copy_missing(
    source_parent_fd: int,
    destination_parent_fd: int,
    name: str,
    track: str,
    uid: int,
    gid: int,
) -> str:
    source_info = _regular(source_parent_fd, name)
    source_data = _normalise_content(_read_regular(source_parent_fd, name), name, track)
    try:
        destination_info = _regular(destination_parent_fd, name)
    except FileNotFoundError:
        destination_info = None
    if destination_info is not None:
        raw_destination = _read_regular(destination_parent_fd, name)
        destination_data = _normalise_content(raw_destination, name, track)
        if raw_destination != destination_data:
            _publish_replace(
                destination_parent_fd, name, destination_data,
                stat.S_IMODE(destination_info.st_mode), uid, gid,
            )
        if source_data == destination_data:
            return "identical"
        if destination_info.st_mtime_ns >= source_info.st_mtime_ns:
            return "preserved-newer"
        raise MigrationConflict(f"conflicting legacy state: {name}")

    source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_parent_fd)
    temporary = f".{name}.{os.getpid()}.migrate.tmp"
    try:
        destination_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(source_info.st_mode),
            dir_fd=destination_parent_fd,
        )
        try:
            _write_all(destination_fd, source_data)
            os.fchown(destination_fd, uid, gid)
            os.fchmod(destination_fd, stat.S_IMODE(source_info.st_mode))
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        current = os.fstat(source_fd)
        if (current.st_size, current.st_mtime_ns) != (
            source_info.st_size, source_info.st_mtime_ns
        ):
            raise RuntimeError(f"legacy source changed during migration: {name}")
        try:
            os.link(
                temporary, name, src_dir_fd=destination_parent_fd,
                dst_dir_fd=destination_parent_fd, follow_symlinks=False,
            )
        except FileExistsError:
            return _copy_missing(source_parent_fd, destination_parent_fd, name, track, uid, gid)
        os.unlink(temporary, dir_fd=destination_parent_fd)
        os.fsync(destination_parent_fd)
    finally:
        os.close(source_fd)
        try:
            os.unlink(temporary, dir_fd=destination_parent_fd)
        except FileNotFoundError:
            pass
    return "copied"


def _source_files(track_fd: int) -> Iterator[str]:
    for entry in os.scandir(track_fd):
        if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
            yield entry.name
    try:
        attachments_fd = os.open("attachments", DIR_FLAGS, dir_fd=track_fd)
    except FileNotFoundError:
        return
    try:
        yield from _attachment_files(attachments_fd, "attachments")
    finally:
        os.close(attachments_fd)


def _attachment_files(parent_fd: int, prefix: str) -> Iterator[str]:
    for entry in os.scandir(parent_fd):
        relative = f"{prefix}/{entry.name}"
        if entry.is_dir(follow_symlinks=False):
            child_fd = os.open(entry.name, DIR_FLAGS, dir_fd=parent_fd)
            try:
                yield from _attachment_files(child_fd, relative)
            finally:
                os.close(child_fd)
        elif entry.is_file(follow_symlinks=False):
            yield relative
        else:
            raise OSError(f"unsupported attachment entry: {relative}")


def _ensure_parent(destination_fd: int, relative: str, uid: int, gid: int) -> int:
    parts = PurePosixPath(relative).parts
    parent_fd = destination_fd
    for part in parts[:-1]:
        next_fd = _secure_dir(parent_fd, part, uid, gid, 0o2775)
        if parent_fd != destination_fd:
            os.close(parent_fd)
        parent_fd = next_fd
    return parent_fd


def _source_parent(source_fd: int, track: str, relative: str) -> int:
    parent_fd = os.open(track, DIR_FLAGS, dir_fd=source_fd)
    for part in PurePosixPath(relative).parts[:-1]:
        next_fd = os.open(part, DIR_FLAGS, dir_fd=parent_fd)
        os.close(parent_fd)
        parent_fd = next_fd
    return parent_fd


def migrate_legacy_state(
    source_root: Path = SOURCE_ROOT,
    destination_root: Path = DESTINATION_ROOT,
    *,
    uid: int,
    gid: int,
) -> dict[str, int]:
    """Copy and path-convert legacy state entirely relative to retained dirfds."""
    destination_fd = open_dir(destination_root, create=True)
    counts = {"copied": 0, "identical": 0, "preserved-newer": 0}
    try:
        os.fchown(destination_fd, uid, gid)
        os.fchmod(destination_fd, 0o3775)
    finally:
        os.close(destination_fd)
    source_fd = open_dir(source_root)
    destination_fd = open_dir(destination_root)
    try:
        os.fchown(destination_fd, uid, gid)
        os.fchmod(destination_fd, 0o3775)
        for track in TRACKS:
            os.close(_secure_dir(destination_fd, track, uid, gid, 0o2775))
            try:
                source_track_fd = os.open(track, DIR_FLAGS, dir_fd=source_fd)
            except FileNotFoundError:
                candidates = [f"{track}-state.json"]
            else:
                try:
                    candidates = [f"{track}-state.json", *_source_files(source_track_fd)]
                finally:
                    os.close(source_track_fd)
            for relative in candidates:
                parent_name = PurePosixPath(relative).name
                if relative == f"{track}-state.json":
                    source_parent_fd = source_fd
                    destination_parent_fd = destination_fd
                    name = relative
                else:
                    source_parent_fd = _source_parent(source_fd, track, relative)
                    destination_parent_fd = _ensure_parent(
                        destination_fd, f"{track}/{relative}", uid, gid,
                    )
                    name = parent_name
                try:
                    result = _copy_missing(
                        source_parent_fd, destination_parent_fd, name, track, uid, gid,
                    )
                except FileNotFoundError:
                    if relative == f"{track}-state.json":
                        continue
                    raise
                finally:
                    if source_parent_fd != source_fd:
                        os.close(source_parent_fd)
                    if destination_parent_fd != destination_fd:
                        os.close(destination_parent_fd)
                counts[result] += 1
        for track in TRACKS:
            _secure_state(destination_fd, f"{track}-state.json", uid, gid)
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    return counts


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("legacy state migration must run as root")
    from scripts.legacy_state_quiesce import quiesce
    quiesce()
    counts = migrate_legacy_state(
        uid=pwd.getpwnam("kek").pw_uid,
        gid=grp.getgrnam("diary").gr_gid,
    )
    print("legacy state migration: " + " ".join(f"{key}={value}" for key, value in counts.items()))


if __name__ == "__main__":
    main()
