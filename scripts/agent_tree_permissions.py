#!/usr/bin/env python3
"""Repair a quiesced tree using no-follow dirfds and unprivileged fd mutations."""
from __future__ import annotations

import argparse
import array
import grp
import os
import pwd
import re
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
END = 0xFFFFFFFF
OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


class TreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int


@dataclass(frozen=True)
class Entry:
    components: tuple[str, ...]
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int
    nlink: int

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.mode)


def _display(root: Path, components: tuple[str, ...]) -> str:
    return str(root.joinpath(*components))


def _parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        raise TreeError(f"tree root must be absolute: {path}")
    parts = path.parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise TreeError(f"tree root contains an unsafe component: {path}")
    return parts


def _open_directory(path: Path) -> int:
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in _parts(path):
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mount_path(value: str) -> str:
    return OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _reject_nested_mounts(root: Path, mountinfo: str | None = None) -> None:
    if mountinfo is None:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    root_text = os.path.abspath(root)
    prefix = root_text.rstrip(os.sep) + os.sep
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise TreeError("cannot parse /proc/self/mountinfo")
        mountpoint = _mount_path(fields[4])
        if mountpoint.startswith(prefix):
            raise TreeError(f"nested mount is not allowed beneath {root}: {mountpoint}")


def _entry(components: tuple[str, ...], value: os.stat_result) -> Entry:
    return Entry(
        components=components,
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        gid=value.st_gid,
        nlink=value.st_nlink,
    )


def _matches(entry: Entry, value: os.stat_result) -> bool:
    return entry == _entry(entry.components, value)


def _validate(entry: Entry, root: Path, root_dev: int, allowed_uids: set[int]) -> None:
    path = _display(root, entry.components)
    if entry.dev != root_dev:
        raise TreeError(f"filesystem mount crossed at {path}")
    if entry.uid not in allowed_uids:
        raise TreeError(f"unexpected owner uid {entry.uid} at {path}")
    if entry.is_dir:
        return
    if stat.S_ISREG(entry.mode):
        if entry.nlink != 1:
            raise TreeError(f"multiply linked regular file is not allowed: {path}")
        return
    if stat.S_ISLNK(entry.mode):
        raise TreeError(f"symlink is not allowed: {path}")
    raise TreeError(f"special file is not allowed: {path}")


def _scan_directory(
    fd: int,
    root: Path,
    components: tuple[str, ...],
    root_dev: int,
    allowed_uids: set[int],
    entries: list[Entry],
) -> None:
    for name in os.listdir(fd):
        child_components = (*components, name)
        value = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(value.st_mode):
            continue
        entry = _entry(child_components, value)
        _validate(entry, root, root_dev, allowed_uids)
        flags = DIR_FLAGS if entry.is_dir else FILE_FLAGS
        child_fd = os.open(name, flags, dir_fd=fd)
        try:
            opened = os.fstat(child_fd)
            _validate(_entry(child_components, opened), root, root_dev, allowed_uids)
            if not _matches(entry, opened):
                raise TreeError(f"entry changed during inspection: {_display(root, child_components)}")
            entries.append(entry)
            if entry.is_dir:
                _scan_directory(child_fd, root, child_components, root_dev, allowed_uids, entries)
        finally:
            os.close(child_fd)


def _inspect(root: Path, allowed_uids: set[int]) -> tuple[int, list[Entry]]:
    _reject_nested_mounts(root)
    fd = _open_directory(root)
    try:
        root_stat = os.fstat(fd)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise TreeError(f"tree root is not a directory: {root}")
        root_entry = _entry((), root_stat)
        _validate(root_entry, root, root_stat.st_dev, allowed_uids)
        entries = [root_entry]
        _scan_directory(fd, root, (), root_stat.st_dev, allowed_uids, entries)
        return fd, entries
    except BaseException:
        os.close(fd)
        raise


def inspect_tree(root: Path, allowed_uids: set[int]) -> list[Entry]:
    fd, entries = _inspect(root, allowed_uids)
    os.close(fd)
    return entries


def _open_target(root_fd: int, root: Path, entry: Entry) -> int:
    fd = os.dup(root_fd)
    try:
        for part in entry.components[:-1]:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if entry.components:
            next_fd = os.open(
                entry.components[-1], DIR_FLAGS if entry.is_dir else FILE_FLAGS, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        value = os.fstat(fd)
        _validate(_entry(entry.components, value), root, entry.dev, {entry.uid})
        if not _matches(entry, value):
            raise TreeError(f"entry changed before repair: {_display(root, entry.components)}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _desired_mode(entry: Entry) -> int:
    mode = stat.S_IMODE(entry.mode)
    result = (mode & 0o777) | stat.S_IRGRP | stat.S_IWGRP
    if entry.is_dir or mode & 0o111:
        result |= stat.S_IXGRP
    result |= mode & stat.S_ISVTX
    if entry.is_dir:
        result |= stat.S_ISGID
    return result


def _apply_entry(fd: int, entry: Entry, root: Path, group_gid: int) -> None:
    value = os.fstat(fd)
    if not _matches(entry, value):
        raise TreeError(f"entry changed during repair: {_display(root, entry.components)}")
    if value.st_uid != os.getuid():
        raise TreeError(f"privilege drop does not match entry owner: {_display(root, entry.components)}")
    os.fchown(fd, -1, group_gid)
    os.fchmod(fd, _desired_mode(entry))


def _drop_privileges(account: Account) -> None:
    os.initgroups(account.name, account.gid)
    os.setgid(account.gid)
    os.setuid(account.uid)
    if os.getresuid() != (account.uid, account.uid, account.uid):
        raise TreeError(f"could not irreversibly drop privileges to {account.name}")


def _receive(sock: socket.socket) -> tuple[int, list[int]]:
    message, ancillary, flags, _ = sock.recvmsg(4, socket.CMSG_SPACE(array.array("i").itemsize))
    if len(message) != 4:
        raise TreeError("permission repair coordinator closed the connection")
    if flags & socket.MSG_CTRUNC:
        raise TreeError("file descriptor message was truncated")
    fds = array.array("i")
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data)
    return struct.unpack("!I", message)[0], list(fds)


def _serve(sock: socket.socket, entries: list[Entry], root: Path, group_gid: int) -> None:
    while True:
        index, fds = _receive(sock)
        if index == END:
            if fds:
                raise TreeError("end message unexpectedly carried a descriptor")
            sock.sendall(b".")
            return
        if index >= len(entries) or len(fds) != 1:
            raise TreeError("invalid permission repair request")
        try:
            _apply_entry(fds[0], entries[index], root, group_gid)
        finally:
            for fd in fds:
                os.close(fd)
        sock.sendall(b".")


def _read_ack(sock: socket.socket) -> None:
    response = sock.recv(1)
    if response == b".":
        return
    if response == b"E":
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        detail = b"".join(chunks).decode("utf-8", errors="replace")
        raise TreeError(f"unprivileged permission repair failed: {detail}")
    raise TreeError("unprivileged permission repair ended unexpectedly")


def _run_owner(root_fd: int, root: Path, entries: list[Entry], account: Account, group_gid: int) -> None:
    if not entries:
        return
    parent, child = socket.socketpair()
    try:
        pid = os.fork()
    except BaseException:
        parent.close()
        child.close()
        raise
    if pid == 0:
        parent.close()
        try:
            _drop_privileges(account)
            _serve(child, entries, root, group_gid)
        except (OSError, TreeError) as error:
            try:
                child.sendall(b"E" + str(error).encode("utf-8", errors="replace")[:4000])
            except OSError:
                pass
            os._exit(1)
        os._exit(0)
    child.close()
    status = 0
    try:
        for index, entry in enumerate(entries):
            fd = _open_target(root_fd, root, entry)
            try:
                parent.sendmsg(
                    [struct.pack("!I", index)],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
                )
            finally:
                os.close(fd)
            _read_ack(parent)
        parent.sendall(struct.pack("!I", END))
        _read_ack(parent)
    finally:
        parent.close()
        _, status = os.waitpid(pid, 0)
    if status != 0:
        raise TreeError(f"unprivileged permission repair child failed for {account.name}")


def repair_tree(root: Path, accounts: list[Account], group_gid: int) -> dict[str, int]:
    root_fd, entries = _inspect(root, {account.uid for account in accounts})
    counts: dict[str, int] = {}
    try:
        for account in accounts:
            owned = [entry for entry in entries if entry.uid == account.uid]
            _run_owner(root_fd, root, owned, account, group_gid)
            counts[account.name] = len(owned)
    finally:
        os.close(root_fd)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--user", action="append", required=True, dest="users")
    parser.add_argument("--group", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("permission repair must start as root")
    try:
        accounts = []
        for name in args.users:
            value = pwd.getpwnam(name)
            accounts.append(Account(name, value.pw_uid, value.pw_gid))
        group_gid = grp.getgrnam(args.group).gr_gid
        counts = repair_tree(args.root, accounts, group_gid)
    except (KeyError, OSError, TreeError) as error:
        raise SystemExit(f"FAIL: {error}") from error
    for name, count in counts.items():
        print(f"{name}: repaired {count} entries")


if __name__ == "__main__":
    main()
