#!/usr/bin/python3
"""sudo-ask — ask the sudo-broker to run a command, and wait for the answer.

Used by an agent that has no standing sudo rights:

    sudo-ask systemctl restart caddy
    sudo-ask --as kek 'systemctl --user restart mail-sync'
    sudo-ask 'docker compose -f /path/to/diary-approved/immich/compose.yaml up -d'
    sudo-ask --break-glass

A human with `sudo /usr/local/sbin/sudo-broker` running in a terminal sees the
command, its cwd and the requesting uid, and types y or n. Output is streamed
back; the exit status is the command's own, or:

    2  bad socket / cannot watch for the broker
    1  denied or malformed request (a request nobody answers just waits)
    124 the broker killed an approved command that ran too long

Nothing here can escalate on its own: the socket only exists while a root
daemon is listening, and that daemon only runs what a human just approved.
If the broker is not running, the client waits for its socket to appear.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import shlex
import socket
import stat
import sys

SOCK_PATH = os.environ.get("SUDO_BROKER_SOCK", "/run/sudo-broker/agent.sock")


def die(msg: str) -> None:
    print(f"sudo-ask: {msg}", file=sys.stderr)
    sys.exit(2)


def status(msg: str) -> None:
    if sys.stdout.isatty():
        print(f"(sudo-broker: {msg})", file=sys.stderr, flush=True)


class SocketWatcher:
    def __init__(self, path: str) -> None:
        self.path = path
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.wd: int | None = None
        self.target: str | None = None

    def arm(self) -> None:
        target = os.path.dirname(self.path) or "."
        while not os.path.isdir(target):
            parent = os.path.dirname(target)
            if parent == target:
                break
            target = parent
        if target == self.target:
            return
        if self.wd is not None:
            self.libc.inotify_rm_watch(self.fd, self.wd)
        wd = self.libc.inotify_add_watch(
            self.fd, os.fsencode(target), 0x00000100 | 0x00000080,
        )
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"cannot watch {target}")
        self.wd = wd
        self.target = target

    def wait(self) -> None:
        select.select([self.fd], [], [])
        os.read(self.fd, 4096)

    def close(self) -> None:
        os.close(self.fd)


def connect_to_broker() -> socket.socket:
    watcher = SocketWatcher(SOCK_PATH)
    announced = False
    try:
        while True:
            watcher.arm()
            try:
                st = os.stat(SOCK_PATH)
            except (FileNotFoundError, NotADirectoryError):
                st = None
            if st is not None:
                if not stat.S_ISSOCK(st.st_mode):
                    die(f"{SOCK_PATH} exists but is not a socket — refusing to connect")
                if st.st_uid != 0:
                    # A user namespace may leave root unmapped. In that case the
                    # non-writable parent directory supplies the ownership check.
                    parent = os.path.dirname(SOCK_PATH) or "."
                    try:
                        pst = os.stat(parent)
                    except OSError as exc:
                        die(f"cannot stat {parent} ({exc})")
                    if pst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                        die(f"{parent} is writable by group/other — refusing to trust {SOCK_PATH}")
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    conn.connect(SOCK_PATH)
                except (FileNotFoundError, ConnectionRefusedError):
                    conn.close()
                except OSError as exc:
                    conn.close()
                    die(f"cannot reach the broker ({exc})")
                else:
                    return conn
            if not announced:
                status("waiting for broker to start")
                announced = True
            watcher.wait()
    finally:
        watcher.close()


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: sudo-ask [--as USER] <command ...>\n"
            "       sudo-ask [--as USER] 'shell command with | pipes or && logic'\n"
            "       sudo-ask --break-glass\n\n"
            "Sends the command to the sudo-broker that a human has running in a\n"
            "terminal. It runs only if they approve it there. --as picks the\n"
            "identity it runs as (default root; the broker only allows the\n"
            "users it was built with, normally root and kek). Break-glass asks\n"
            "the controller for attended mode and always requires confirmation.",
            file=sys.stderr,
        )
        return 2

    break_glass = argv == ["--break-glass"]
    as_user = "root"
    if break_glass:
        cmd = ""
    else:
        rest = list(argv)
        while rest:
            if rest[0] == "--as" and len(rest) > 1:
                as_user = rest[1]
                del rest[:2]
                continue
            if rest[0].startswith("--as="):
                as_user = rest[0].split("=", 1)[1]
                del rest[0]
                continue
            break
        if not rest:
            die("--as needs a command to run")
        if not as_user:
            die("--as needs a user name")

        # The broker runs the request as a shell command, so several argv
        # words must keep their quoting. Joining them with a bare space turned
        # `sudo-ask bash -lc 'set -o pipefail; ...'` into `bash -lc set -o
        # pipefail; ...` — which printed the whole login environment, secrets
        # included, instead of running the script.
        cmd = rest[0] if len(rest) == 1 else shlex.join(rest)

    try:
        conn = connect_to_broker()
    except OSError as exc:
        die(f"cannot watch for the broker ({exc})")

    payload = {"cmd": cmd, "cwd": os.getcwd(), "client_pid": os.getpid()}
    if break_glass:
        payload["break_glass"] = True
    else:
        payload["as"] = as_user
    conn.sendall(json.dumps(payload).encode() + b"\n")

    code = 1
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("t")
                if kind == "out":
                    stream = sys.stdout if msg.get("fd") == "out" else sys.stderr
                    stream.write(msg.get("d", ""))
                    stream.flush()
                elif kind == "state":
                    state = msg.get("s")
                    if state == "queued":
                        ahead = int(msg.get("ahead", 0))
                        status(f"queued — {ahead} ahead")
                    elif state == "approved":
                        ran_as = msg.get("as", "root")
                        suffix = "" if ran_as == "root" else f" as {ran_as}"
                        status(f"approved{suffix}")
                    elif state in ("denied", "error"):
                        reason = msg.get("msg", "")
                        status(f"{state}{': ' + reason if reason else ''}")
                elif kind == "exit":
                    code = int(msg.get("code", 1))
                    if msg.get("msg"):
                        status(msg["msg"])
    except OSError as exc:
        print(f"sudo-ask: connection lost ({exc})", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
