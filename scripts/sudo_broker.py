#!/usr/bin/python3
"""sudo-broker — a root daemon that runs commands only after a human approves.

Started by a human in a terminal they keep open:

    sudo /usr/local/sbin/sudo-broker

Agents ask through it instead of holding standing sudo rights:

    sudo-ask systemctl restart caddy

The daemon prints the exact command with the requesting uid/pid/cwd and waits
for `y`. Each request rings the terminal bell (and rings again every 30 s while
it is unanswered), so the window you started the broker in asks for focus.
Approvals are single-use, the executed command gets no access to the
terminal (stdin is /dev/null), and the whole capability disappears when the
daemon exits (Ctrl-C, terminal closed, or idle timeout). There is deliberately
no way to pre-authorise a command.

Protocol: newline-delimited JSON over an AF_UNIX stream socket; see sudo_ask.py
for the client half. Every request is logged to stderr (your terminal) — pipe
stderr to a file if you want an audit trail across sessions.
"""

from __future__ import annotations

import json
import os
import pwd
import select
import socket
import struct
import subprocess
import sys
import threading
import time

SOCK_PATH = os.environ.get("SUDO_BROKER_SOCK", "/run/sudo-broker/agent.sock")
SOCK_DIR = os.path.dirname(SOCK_PATH)

MAX_REQUEST_BYTES = 65536
MAX_CMD_CHARS = 4096
PROMPT_TIMEOUT = 120.0  # seconds to answer before the request is denied
BELL_INTERVAL = 30.0  # re-ring the terminal bell while a prompt is unanswered
EXEC_TIMEOUT = 600.0  # seconds before an approved command is killed
IDLE_EXIT = 1800.0  # exit when nothing has connected for this long

CHILD_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "TERM": "dumb",
}

_send_lock = threading.Lock()
_prompt_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[sudo-broker] {msg}", file=sys.stderr, flush=True)


def bell() -> None:
    """Ring the operator's terminal bell so the window asks for attention.

    Terminals and multiplexers turn BEL into an urgency hint, a taskbar flash
    or a marked tab — that is how the human notices a request that arrived
    while another window had focus. The bell goes to the first of stdout and
    stderr that is a terminal, because an audit setup may redirect stderr to a
    log; when neither is a tty nothing is written, so log files stay free of
    stray control bytes.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # fd closed at startup — nothing to ring
            continue
        try:
            if not stream.isatty():
                continue
            stream.write("\a")
            stream.flush()
            return
        except (AttributeError, OSError, ValueError):
            continue


def sanitize(text: str, limit: int = MAX_CMD_CHARS) -> str:
    """Make untrusted text safe to print on the operator's terminal."""
    if len(text) > limit:
        text = text[:limit] + f"… [truncated, {len(text)} chars total]"
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 32 or o == 127:
            out.append(f"\\x{o:02x}")
        else:
            out.append(ch)
    return "".join(out)


def peer_info(conn: socket.socket) -> tuple[int, int, int, str]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = f"uid{uid}"
    return pid, uid, gid, name


def send(conn: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode() + b"\n"
    with _send_lock:
        conn.sendall(data)


def read_request(conn: socket.socket) -> dict | None:
    conn.settimeout(30.0)
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > MAX_REQUEST_BYTES:
            return None
    line = buf.split(b"\n", 1)[0]
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
        return None
    return req


def approve(info: tuple[int, int, int, str], cmd: str, cwd: str) -> tuple[bool, str]:
    """Show the request on the terminal and wait for an explicit y."""
    pid, uid, _gid, name = info
    if not _prompt_lock.acquire(timeout=PROMPT_TIMEOUT):
        return False, "another request is already pending"
    try:
        print("", flush=True)
        print("┌─ sudo-broker " + "─" * 58, flush=True)
        print(f"│ from : {name} (uid {uid}, pid {pid})", flush=True)
        print(f"│ cwd  : {sanitize(cwd, 512)}", flush=True)
        print(f"│ run  : {sanitize(cmd)}", flush=True)
        print(f"│ answer within {int(PROMPT_TIMEOUT)}s", flush=True)
        print("└─ approve? [y/N] ", end="", flush=True)
        bell()
        answer = None
        deadline = time.monotonic() + PROMPT_TIMEOUT
        while answer is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sys.stdin], [], [], min(BELL_INTERVAL, remaining))
            if ready:
                answer = sys.stdin.readline().strip().lower()
            else:
                bell()  # still unanswered — nudge the terminal again
        if answer is None:
            print("\n(sudo-broker: no answer -> denied)", flush=True)
            log(f"DENIED (timeout): {cmd!r} from uid {uid}")
            return False, "timed out waiting for approval"
        if answer not in ("y", "yes"):
            print("(sudo-broker: denied)", flush=True)
            log(f"DENIED (operator said no): {cmd!r} from uid {uid}")
            return False, "denied by operator"
        print("(sudo-broker: approved — running)", flush=True)
        log(f"APPROVED: {cmd!r} from uid {uid} (cwd {cwd})")
        return True, ""
    finally:
        _prompt_lock.release()


def execute(conn: socket.socket, cmd: str, cwd: str) -> None:
    workdir = cwd if cwd and os.path.isdir(cwd) else "/"
    try:
        proc = subprocess.Popen(  # noqa: S603 - the human approved this exact string
            ["/bin/bash", "-c", cmd],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=CHILD_ENV,
            start_new_session=True,
        )
    except OSError as exc:
        send(conn, {"t": "exit", "code": 127, "msg": f"exec failed: {exc}"})
        return

    def pump(pipe, tag: str) -> None:
        try:
            while True:
                chunk = pipe.read1(4096)
                if not chunk:
                    break
                send(conn, {"t": "out", "fd": tag, "d": chunk.decode("utf-8", "replace")})
        except OSError:
            pass
        finally:
            pipe.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.monotonic() + EXEC_TIMEOUT
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
        proc.wait()
        for t in threads:
            t.join(timeout=1.0)
        log(f"TIMEOUT after {int(EXEC_TIMEOUT)}s, killed: {cmd!r}")
        send(conn, {"t": "exit", "code": 124, "msg": f"killed after {int(EXEC_TIMEOUT)}s"})
        return

    for t in threads:
        t.join(timeout=1.0)
    log(f"EXIT {proc.returncode}: {cmd!r}")
    send(conn, {"t": "exit", "code": proc.returncode})


def handle(conn: socket.socket) -> None:
    try:
        info = peer_info(conn)
        req = read_request(conn)
        if req is None:
            send(conn, {"t": "state", "s": "error", "msg": "malformed request"})
            return
        cmd = req["cmd"]
        cwd = req.get("cwd") if isinstance(req.get("cwd"), str) else "/"
        if not cmd.strip():
            send(conn, {"t": "state", "s": "error", "msg": "empty command"})
            return
        if len(cmd) > MAX_CMD_CHARS:
            send(conn, {"t": "state", "s": "error", "msg": "command too long"})
            return
        ok, why = approve(info, cmd, cwd)
        if not ok:
            send(conn, {"t": "state", "s": "denied", "msg": why})
            return
        send(conn, {"t": "state", "s": "approved"})
        execute(conn, cmd, cwd)
    except (OSError, ValueError) as exc:
        try:
            send(conn, {"t": "state", "s": "error", "msg": str(exc)})
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    if os.geteuid() != 0:
        print("sudo-broker: must run as root (sudo /usr/local/sbin/sudo-broker)", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print(
            "sudo-broker: needs a terminal — run it in the foreground in your own shell,\n"
            "not under a service manager or with stdin redirected.",
            file=sys.stderr,
        )
        return 1

    os.makedirs(SOCK_DIR, mode=0o755, exist_ok=True)
    os.chmod(SOCK_DIR, 0o755)
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    srv.listen(16)
    srv.settimeout(30.0)

    log(f"listening on {SOCK_PATH}")
    log(f"approvals appear here; idle-exit after {int(IDLE_EXIT / 60)} min; Ctrl-C to stop")

    last_activity = time.monotonic()
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if time.monotonic() - last_activity > IDLE_EXIT:
                    log("idle timeout — exiting")
                    break
                continue
            last_activity = time.monotonic()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
