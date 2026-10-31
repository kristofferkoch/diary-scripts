#!/usr/bin/python3
"""sudo-ask — ask the sudo-broker to run a command, and wait for the answer.

Used by an agent that has no standing sudo rights:

    sudo-ask systemctl restart caddy
    sudo-ask --as kek 'systemctl --user restart mail-sync'
    sudo-ask 'docker compose -f /path/to/diary-approved/immich/compose.yaml up -d'

A human with `sudo /usr/local/sbin/sudo-broker` running in a terminal sees the
command, its cwd and the requesting uid, and types y or n. Output is streamed
back; the exit status is the command's own, or:

    2  no broker running / bad socket
    1  denied or malformed request (a request nobody answers just waits)
    124 the broker killed an approved command that ran too long

Nothing here can escalate on its own: the socket only exists while a root
daemon is listening, and that daemon only runs what a human just approved.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys

SOCK_PATH = os.environ.get("SUDO_BROKER_SOCK", "/run/sudo-broker/agent.sock")


def die(msg: str) -> None:
    print(f"sudo-ask: {msg}", file=sys.stderr)
    sys.exit(2)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: sudo-ask [--as USER] <command ...>\n"
            "       sudo-ask [--as USER] 'shell command with | pipes or && logic'\n\n"
            "Sends the command to the sudo-broker that a human has running in a\n"
            "terminal. It runs only if they approve it there. --as picks the\n"
            "identity it runs as (default root; the broker only allows the\n"
            "users it was built with, normally root and kek).",
            file=sys.stderr,
        )
        return 2

    as_user = "root"
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

    cmd = rest[0] if len(rest) == 1 else " ".join(rest)

    try:
        st = os.stat(SOCK_PATH)
    except OSError:
        die(
            "no broker running.\n"
            "  Start one in your own terminal and leave it open:\n"
            "      sudo sudo-broker"
        )
    if not stat.S_ISSOCK(st.st_mode):
        die(f"{SOCK_PATH} exists but is not a socket — refusing to connect")
    if st.st_uid != 0:
        # Inside a user namespace (dsh sandboxes the shell in one that maps
        # only the agent's uid/gid) root is unmapped and reads back as 65534,
        # so the ownership check is meaningless there. Fall back to the
        # property that matters: only root can create an entry in a directory
        # that is not group- or world-writable.
        parent = os.path.dirname(SOCK_PATH) or "."
        try:
            pst = os.stat(parent)
        except OSError as exc:
            die(f"cannot stat {parent} ({exc})")
        if pst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            die(f"{parent} is writable by group/other — refusing to trust {SOCK_PATH}")

    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(SOCK_PATH)
    except OSError as exc:
        die(f"cannot reach the broker ({exc}). Is the terminal still open?")

    conn.sendall(
        json.dumps(
            {"cmd": cmd, "as": as_user, "cwd": os.getcwd(), "client_pid": os.getpid()}
        ).encode()
        + b"\n"
    )

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
                    if state == "approved":
                        ran_as = msg.get("as", "root")
                        suffix = "" if ran_as == "root" else f" as {ran_as}"
                        print(f"(sudo-broker: approved{suffix})", file=sys.stderr, flush=True)
                    elif state in ("denied", "error"):
                        reason = msg.get("msg", "")
                        print(f"(sudo-broker: {state}{': ' + reason if reason else ''})",
                              file=sys.stderr, flush=True)
                elif kind == "exit":
                    code = int(msg.get("code", 1))
                    if msg.get("msg"):
                        print(f"(sudo-broker: {msg['msg']})", file=sys.stderr, flush=True)
    except OSError as exc:
        print(f"sudo-ask: connection lost ({exc})", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
