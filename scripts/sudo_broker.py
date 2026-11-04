#!/usr/bin/python3
"""sudo-broker — a root daemon that normally runs commands after human approval.

Started by a human in a terminal they keep open:

    sudo /usr/local/sbin/sudo-broker

Agents ask through it instead of holding standing sudo rights:

    sudo-ask systemctl restart caddy

The daemon prints the exact command with the requesting uid/pid/cwd and waits
for `y`. A request also raises a desktop notification (OSC 99 — kitty and foot
turn it into a native one, a click focuses the window) and rings the terminal
bell, which then repeats every 30 s while the prompt is unanswered. There is
deliberately **no answer timeout**: a request waits as long as the human needs,
because being hurried is how a wrong command gets approved. The explicit
`--auto-accept-all` startup mode skips confirmation for ordinary commands and
logs each as AUTO-APPROVED instead. The fixed break-glass operation always
requires confirmation and calls only the lock controller.
Each request names the identity it needs (`as`, default `root`): the daemon
drops to that uid/gid for the command, with its own HOME and user-session
environment, and the prompt shows it. Only the identities in ALLOWED_USERS are
accepted — normally root and kek — so anything that only writes kek's files or
reaches kek's user session never has to run as root, and root stays visible as
the exception it is.
Approvals are single-use, the executed command gets no access to the
terminal (stdin is /dev/null), and the whole capability disappears when the
daemon exits (Ctrl-C, terminal closed, or idle timeout). There is no way to
pre-authorise an individual command.

Requests that arrive while a prompt is on screen now wait in arrival order
instead of being refused: the operator sees one prompt at a time (with the
number still waiting), each request is still approved individually, and the
queue lives only in memory — a queued command can never outlive the daemon.
A client that disconnects while queued is skipped when its turn comes.

Protocol: newline-delimited JSON over an AF_UNIX stream socket; see sudo_ask.py
for the client half. Every request is logged to stderr (your terminal) — pipe
stderr to a file if you want an audit trail across sessions.
"""

from __future__ import annotations

import argparse
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
from collections.abc import Callable

if __package__:
    from scripts import broker_audit
else:
    import broker_audit  # ty: ignore[unresolved-import]

SOCK_PATH = os.environ.get("SUDO_BROKER_SOCK", "/run/sudo-broker/agent.sock")
SOCK_DIR = os.path.dirname(SOCK_PATH)

MAX_REQUEST_BYTES = 65536
MAX_CMD_CHARS = 4096
BELL_INTERVAL = 30.0  # re-ring the terminal bell while a prompt is unanswered
EXEC_TIMEOUT = 600.0  # seconds before an approved command is killed
IDLE_EXIT = 1800.0  # exit when nothing has connected for this long
AUTO_ACCEPT_ALL = False

# Identities a request may ask for. Root stays in because `sudo-ask` may be the
# only way to install a package or touch /etc; kek is here so a command that
# only needs to write kek's files or reach kek's user session never has to run
# as root. Widen this list deliberately, never from an environment variable.
ALLOWED_USERS = ("root", "kek")

CHILD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_send_lock = threading.Lock()
_prompt_lock = threading.Lock()
#: Everything written to the operator's terminal goes through this lock, so a
#: log line can never land in the middle of the prompt.
_term_lock = threading.RLock()
#: The last prompt line currently on screen, or None between prompts.
_prompt_text: str | None = None


def _set_prompt(text: str) -> None:
    global _prompt_text
    with _term_lock:
        _prompt_text = text


def _clear_prompt() -> None:
    global _prompt_text
    with _term_lock:
        _prompt_text = None

#: Requests allowed to be pending at once (the head plus those waiting).
#: The queue is in-memory on purpose: a request must never outlive the daemon.
MAX_QUEUED = 16


class _TurnQueue:
    """FIFO of requests waiting for the operator's attention.

    A request that arrives while a prompt is on screen waits its turn instead
    of being refused. Only the head is printed and answered; the rest wait
    without a timeout, exactly like a single request does.
    """

    def __init__(self, limit: int = MAX_QUEUED) -> None:
        self._limit = limit
        self._cond = threading.Condition()
        self._order: list[object] = []

    def enter(self) -> tuple[object | None, int]:
        """Take a place in line; returns (token, requests ahead).

        ``None`` means the queue is full — the caller is refused rather than
        letting one local user accumulate unbounded waiting threads.
        """
        with self._cond:
            if len(self._order) >= self._limit:
                return None, len(self._order)
            token = object()
            self._order.append(token)
            return token, len(self._order) - 1

    def wait(self, token: object) -> None:
        with self._cond:
            while self._order and self._order[0] is not token:
                self._cond.wait()

    def leave(self, token: object) -> None:
        with self._cond:
            try:
                self._order.remove(token)
            except ValueError:
                pass
            self._cond.notify_all()

    def __len__(self) -> int:
        with self._cond:
            return len(self._order)


_turn_queue = _TurnQueue()


def log(msg: str) -> None:
    """Write one daemon log line without destroying an open prompt.

    When a prompt is on screen the line is printed above it and the prompt is
    redrawn underneath, instead of being appended to the `[y/N]` line by
    whichever thread logged at that moment.
    """
    line = f"[sudo-broker] {msg}"
    with _term_lock:
        prompt = _prompt_text
        stream = terminal() if prompt is not None else None
        if prompt is not None and stream is not None:
            try:
                if os.environ.get("TERM") == "dumb":
                    stream.write(f"\n{line}\n{prompt}")
                else:
                    # \r\x1b[K clears the prompt line, the log takes it, and
                    # the prompt is reprinted below.
                    stream.write(f"\r\x1b[K{line}\n{prompt}")
                stream.flush()
                return
            except (OSError, ValueError):
                pass
        print(line, file=sys.stderr, flush=True)


def resolve_target(name: str) -> tuple[pwd.struct_passwd | None, str]:
    """Check a requested identity against the allowlist and the passwd database."""
    if name not in ALLOWED_USERS:
        allowed = ", ".join(ALLOWED_USERS)
        return None, f"user {name!r} may not be used (allowed: {allowed})"
    try:
        return pwd.getpwnam(name), ""
    except KeyError:
        return None, f"no such user on this host: {name!r}"


def child_env(name: str, home: str, uid: int, shell: str) -> dict[str, str]:
    """Environment for a command that runs as `name`.

    Same deliberate minimalism as before (`TERM=dumb`, fixed PATH), plus the
    per-user parts that fail silently when missing: without XDG_RUNTIME_DIR
    `systemctl --user` cannot find the user manager, and without the session
    bus address every desktop tool fails in a way that looks like a code bug.
    """
    env = {
        "PATH": CHILD_PATH,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "HOME": home or "/",
        "USER": name,
        "LOGNAME": name,
        "SHELL": shell or "/bin/sh",
    }
    runtime = f"/run/user/{uid}"
    if os.path.isdir(runtime):
        env["XDG_RUNTIME_DIR"] = runtime
        bus = os.path.join(runtime, "bus")
        if os.path.exists(bus):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def terminal():
    """The stream the operator is looking at: first of stdout, stderr that is a tty.

    None when both are redirected, which is what keeps log files clean: the
    bell and the notification are only ever written to a real terminal.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # fd closed at startup
            continue
        try:
            if stream.isatty():
                return stream
        except (AttributeError, OSError, ValueError):
            continue
    return None


def write_terminal(text: str) -> None:
    """Write an escape sequence to the operator's terminal, if there is one."""
    stream = terminal()
    if stream is None:
        return
    with _term_lock:
        try:
            stream.write(text)
            stream.flush()
        except (OSError, ValueError):
            pass


def bell() -> None:
    """Ring the operator's terminal bell so the window asks for attention.

    Terminals and multiplexers turn BEL into an urgency hint, a taskbar flash
    or a marked tab — that is how the human notices a request that arrived
    while another window had focus.
    """
    write_terminal("\a")


def notify(title: str, body: str) -> None:
    """Show a desktop notification through the terminal (kitty/foot OSC 99).

    Kitty turns this into a native notification; `a=focus` (kitty's default,
    spelled out) makes a click focus the window, and `o=unfocused` suppresses
    the popup while the terminal already has keyboard focus. A terminal that
    does not implement OSC 99 ignores the sequence. The id is per daemon, so a
    later request replaces the previous notification instead of stacking.
    """
    title = sanitize(title, 80)
    body = sanitize(body, 300)
    ident = f"sudo-broker-{os.getpid()}"
    write_terminal(
        f"\x1b]99;i={ident}:d=0:o=unfocused:a=focus;{title}\x1b\\"
        f"\x1b]99;i={ident}:p=body;{body}\x1b\\"
    )


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


def peer_alive(conn: socket.socket) -> bool:
    """False when the client hung up while its request waited in the queue.

    Readable + EOF means closed; readable with data (the client is not
    supposed to send more) still counts as alive and is left unread.
    """
    try:
        ready, _, _ = select.select([conn], [], [], 0)
        return not ready or conn.recv(1, socket.MSG_PEEK) != b""
    except OSError:
        return False


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


def consume_operator_input(last_activity: float) -> tuple[float, bool]:
    """Discard input typed between prompts and refresh the idle deadline.

    The boolean is false at end-of-input, so the main loop can stop watching a
    closed terminal instead of spinning on a stream that is always readable.
    """
    if not _prompt_lock.acquire(blocking=False):
        return last_activity, True
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return last_activity, True
        if os.read(sys.stdin.fileno(), 4096) == b"":
            return last_activity, False
        return time.monotonic(), True
    finally:
        _prompt_lock.release()


def approve(
    info: tuple[int, int, int, str],
    cmd: str,
    cwd: str,
    as_user: str = "root",
    *,
    require_confirmation: bool = False,
    on_queue: Callable[[int], None] | None = None,
    is_alive: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Queue for the operator, then show the request and wait for `y`.

    Requests that arrive while a prompt is on screen wait in arrival order
    instead of being refused. Only the head is printed and answered — each
    request is still approved individually, nothing is batched, and there is
    still no answer timeout. A client that vanished while queued is skipped
    when its turn comes, rather than prompting for a command nobody waits for.
    """
    pid, uid, _gid, name = info
    token, ahead = _turn_queue.enter()
    if token is None:
        log(f"REFUSED (queue full, {ahead} pending): {cmd!r} from uid {uid}")
        return False, "too many requests are already pending"
    try:
        if ahead and on_queue is not None:
            on_queue(ahead)
        if ahead:
            log(f"QUEUED ({ahead} ahead): {cmd!r} from uid {uid}")
        _turn_queue.wait(token)
        if is_alive is not None and not is_alive():
            log(f"DROPPED (client gone while queued): {cmd!r} from uid {uid}")
            return False, "client disconnected while queued"
        with _prompt_lock:
            prompt_line = "└─ approve? [y/N] "
            auto = AUTO_ACCEPT_ALL and not require_confirmation
            with _term_lock:
                print(flush=True)
                print("┌─ sudo-broker " + "─" * 58, flush=True)
                print(f"│ from : {name} (uid {uid}, pid {pid})", flush=True)
                print(f"│ cwd  : {sanitize(cwd, 512)}", flush=True)
                print(f"│ as   : {as_user}", flush=True)
                print(f"│ run  : {sanitize(cmd)}", flush=True)
                waiting = len(_turn_queue) - 1
                if waiting:
                    print(f"│ queue: {waiting} more waiting", flush=True)
                if auto:
                    print("└─ AUTO-ACCEPT-ALL — running without confirmation", flush=True)
                else:
                    print("│ no timeout — answer when you have read it", flush=True)
                    print(prompt_line, end="", flush=True)
                    _set_prompt(prompt_line)
            if auto:
                log(f"AUTO-APPROVED (as {as_user}): {cmd!r} from uid {uid} (cwd {cwd})")
                return True, ""
            notify(
                f"sudo-broker: approval requested (as {as_user})",
                f"{name} (uid {uid}) wants to run: {cmd}",
            )
            bell()
            answer = None
            while answer is None:
                ready, _, _ = select.select([sys.stdin], [], [], BELL_INTERVAL)
                if ready:
                    answer = (
                        os.read(sys.stdin.fileno(), 4096)
                        .decode("utf-8", "replace")
                        .strip()
                        .lower()
                    )
                else:
                    bell()  # still unanswered — nudge the terminal again
            with _term_lock:
                _clear_prompt()
                if answer not in ("y", "yes"):
                    print("(sudo-broker: denied)", flush=True)
                else:
                    print("(sudo-broker: approved — running)", flush=True)
            if answer not in ("y", "yes"):
                log(f"DENIED (operator said no): {cmd!r} from uid {uid}")
                return False, "denied by operator"
            log(f"APPROVED (as {as_user}): {cmd!r} from uid {uid} (cwd {cwd})")
            return True, ""
    finally:
        _turn_queue.leave(token)


def execute(conn: socket.socket, cmd: str, cwd: str, target: pwd.struct_passwd) -> None:
    workdir = cwd if cwd and os.path.isdir(cwd) else "/"
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(target.pw_name, target.pw_dir, target.pw_uid, target.pw_shell),
            user=target.pw_uid,
            group=target.pw_gid,
            extra_groups=os.getgrouplist(target.pw_name, target.pw_gid),
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


def handle(
    conn: socket.socket,
    *,
    enforce_lockdown: bool = False,
    lock_socket: str = broker_audit.DEFAULT_LOCK_SOCKET,
    audit_socket: str = broker_audit.DEFAULT_SOCKET,
) -> None:
    try:
        info = peer_info(conn)
        caller = {"pid": info[0], "uid": info[1], "gid": info[2]}

        def queued(ahead: int) -> None:
            send(conn, {"t": "state", "s": "queued", "ahead": ahead})

        def still_connected() -> bool:
            return peer_alive(conn)

        def allowed(operation: str) -> bool:
            return broker_audit.authorize(
                "sudo-broker",
                operation,
                caller,
                enforce=enforce_lockdown,
                socket_path=audit_socket,
                lock_socket=lock_socket,
                owner_uid=os.geteuid(),
            )[0]

        req = read_request(conn)
        if req is None:
            allowed("unknown")
            send(conn, {"t": "state", "s": "error", "msg": "malformed request"})
            return
        break_glass = req.get("break_glass", False)
        valid_break_glass = (
            break_glass is True
            and req["cmd"] == ""
            and set(req) == {"cmd", "cwd", "client_pid", "break_glass"}
        )
        operation = "break_glass" if valid_break_glass else (
            "execute" if break_glass is False else "unknown"
        )
        if not allowed(operation):
            send(conn, {"t": "state", "s": "denied", "msg": "denied by locked-mode broker policy"})
            return
        cmd = req["cmd"]
        requested_cwd = req.get("cwd")
        cwd = requested_cwd if isinstance(requested_cwd, str) else "/"
        if break_glass is not False:
            if not valid_break_glass:
                send(conn, {"t": "state", "s": "error", "msg": "malformed break-glass request"})
                return
            ok, why = approve(
                info,
                "/usr/local/bin/diary-lock break-glass",
                cwd,
                require_confirmation=True,
                on_queue=queued,
                is_alive=still_connected,
            )
            if not ok:
                send(conn, {"t": "state", "s": "denied", "msg": why})
                return
            send(conn, {"t": "state", "s": "approved", "as": "root"})
            try:
                reply = broker_audit.controller_request(
                    lock_socket, {"cmd": "break_glass"}, timeout=10.0,
                )
                if reply.get("state") != "ATTENDED":
                    raise ValueError("controller did not confirm attended mode")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                send(conn, {"t": "exit", "code": 1, "msg": f"break-glass failed: {exc}"})
            else:
                send(conn, {"t": "exit", "code": 0})
            return
        as_user = req.get("as", "root")
        if not cmd.strip():
            send(conn, {"t": "state", "s": "error", "msg": "empty command"})
            return
        if len(cmd) > MAX_CMD_CHARS:
            send(conn, {"t": "state", "s": "error", "msg": "command too long"})
            return
        if not isinstance(as_user, str):
            send(conn, {"t": "state", "s": "error", "msg": "malformed request"})
            return
        target, problem = resolve_target(as_user)
        if target is None:
            send(conn, {"t": "state", "s": "denied", "msg": problem})
            log(f"DENIED (identity): {as_user!r} requested for {cmd!r}")
            return
        ok, why = approve(info, cmd, cwd, as_user=target.pw_name,
                          on_queue=queued, is_alive=still_connected)
        if not ok:
            send(conn, {"t": "state", "s": "denied", "msg": why})
            return
        if enforce_lockdown and not allowed("execute"):
            send(conn, {"t": "state", "s": "denied", "msg": "denied by locked-mode broker policy"})
            return
        send(conn, {"t": "state", "s": "approved", "as": target.pw_name})
        execute(conn, cmd, cwd, target)
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
    global AUTO_ACCEPT_ALL

    parser = argparse.ArgumentParser(
        description="Execute commands through operator-controlled approval."
    )
    parser.add_argument(
        "--auto-accept-all",
        action="store_true",
        help="run ordinary valid requests without operator confirmation (dangerous)",
    )
    parser.add_argument("--enforce-lockdown", action="store_true")
    parser.add_argument("--lock-socket", default=broker_audit.DEFAULT_LOCK_SOCKET)
    parser.add_argument("--audit-socket", default=broker_audit.DEFAULT_SOCKET)
    args = parser.parse_args()
    AUTO_ACCEPT_ALL = args.auto_accept_all

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
    if AUTO_ACCEPT_ALL:
        log("WARNING: AUTO-ACCEPT-ALL enabled; ordinary valid requests run without confirmation")
    else:
        log("approvals appear here")
    log(f"idle-exit after {int(IDLE_EXIT / 60)} min; Ctrl-C to stop")

    last_activity = time.monotonic()
    watch_stdin = True
    try:
        while True:
            readers = [srv, sys.stdin] if watch_stdin else [srv]
            ready, _, _ = select.select(readers, [], [], 30.0)
            if not ready:
                # A prompt waiting for the human keeps the daemon alive: the
                # idle exit must not pull the socket out from under it.
                if _prompt_lock.locked() or len(_turn_queue):
                    continue
                if time.monotonic() - last_activity > IDLE_EXIT:
                    log("idle timeout — exiting")
                    break
                continue
            if watch_stdin and sys.stdin in ready:
                last_activity, watch_stdin = consume_operator_input(last_activity)
            if srv in ready:
                conn, _ = srv.accept()
                last_activity = time.monotonic()
                threading.Thread(
                    target=handle,
                    args=(conn,),
                    kwargs={
                        "enforce_lockdown": args.enforce_lockdown,
                        "lock_socket": args.lock_socket,
                        "audit_socket": args.audit_socket,
                    },
                    daemon=True,
                ).start()
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
