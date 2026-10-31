#!/usr/bin/python3
"""diary-sync — an allow-listed socket broker for the sjekk's privileged steps.

The sjekk needs four things the agent must *not* be able to do itself:

* ``spond-sync`` — unlocks the Spond password through ``pass`` (GPG),
* ``vigilo-sync`` / ``skolemelding-sync`` — read and refresh the BankID tokens
  stored under ``~/.config/diary``,
* ``systemctl --user start mail-sync.service`` — kek's own user manager,
* ``systemctl --user show/ is-active`` — the pipeline units' state.

Handing the agent the pass store or the token directory would give it far more
than the sjekk needs. Instead the account owner runs this broker in a small
systemd unit and lets the ``diary`` group ask for exactly four verbs over a
Unix socket::

    {"cmd": "sync",   "target": "spond" | "vigilo" | "skolemelding" | "all"}
    {"cmd": "start",  "unit": "mail-sync.service"}
    {"cmd": "status"}
    {"cmd": "ping"}

The broker (this file, ``main``) runs as the account owner and is the only
process that touches the secrets. The client (``client_main``, entry point
``diary-sync``) runs as the agent and never sees them.

Security boundary
-----------------
* Only the verbs and fields above exist; extra argv, environment, working
  directory, and filesystem fields are rejected before any command runs.
  Targets and units are looked up in fixed tables, so a caller cannot run
  arbitrary code.
* ``sync`` is rate-limited (6/hour, burst 6) and serialised: a second
  request waits up to 240 s for the slot, then gets ``busy``.
* Requests are capped at 64 KiB, replies at 4000 chars of output tail.
* Rejections are logged to the journal with the caller's uid.
* The socket is ``0770`` in a ``0750`` runtime dir owned by ``kek:diary`` — the
  group membership is the capability, exactly like ``signal-notify``.

``--fallback`` on the client does the work locally when the socket is missing,
so a plain kek shell keeps working if the broker is down.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time

DEFAULT_SOCKET = "/run/diary-sync/socket"
VERSION = "1.0"

# --- allow-lists (the whole security boundary) -------------------------------

SYNC_TARGETS: dict[str, tuple[str, ...]] = {
    "spond": ("spond-sync", "--once"),
    "vigilo": ("vigilo-sync", "--once"),
    "skolemelding": ("skolemelding-sync", "--once"),
}
ALLOWED_START_UNITS = ("mail-sync.service",)
STATUS_UNITS = (
    "mail-sync.timer",
    "mail-sync.service",
    "signal-capture.service",
    "embed-messages.timer",
    "embed-messages.service",
    "embed-images.timer",
    "embed-images.service",
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 4000
SYNC_TIMEOUT = 300.0
STATUS_TIMEOUT = 60.0
SYNC_BURST, SYNC_PERIOD = 6, 3600.0
STATUS_BURST, STATUS_PERIOD = 120, 60.0


class BrokerUnavailable(RuntimeError):
    """No broker socket (not installed, not started, or wrong path)."""


class Limiter:
    """Token bucket: burst of `burst`, refilling over `period` seconds."""

    def __init__(self, burst: int, period: float) -> None:
        self.burst = burst
        self.period = period
        self.tokens = float(burst)
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst,
                              self.tokens + (now - self.stamp) * self.burst / self.period)
            self.stamp = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


def tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Last `limit` characters, with a marker when something was dropped."""
    text = text or ""
    if len(text) <= limit:
        return text
    return "…(truncated)…\n" + text[-limit:]


def run_command(argv: list[str] | tuple[str, ...],
                timeout: float = SYNC_TIMEOUT) -> dict:
    """Run an allow-listed command; never raises. Returns a JSON-able result."""
    started = time.monotonic()
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": tail((proc.stdout or "") + (proc.stderr or "")),
            "duration_s": round(time.monotonic() - started, 1),
            "argv": " ".join(argv),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        for name, value in (("stdout", out), ("stderr", err)):
            if isinstance(value, bytes):
                out = out.decode("utf-8", "replace") if name == "stdout" else out
                err = err.decode("utf-8", "replace") if name == "stderr" else err
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "output": tail(str(out) + str(err)),
            "duration_s": round(time.monotonic() - started, 1),
            "argv": " ".join(argv),
        }
    except OSError as exc:
        return {
            "ok": False,
            "exit_code": None,
            "error": f"could not run {argv[0]!r}: {exc}",
            "duration_s": round(time.monotonic() - started, 1),
            "argv": " ".join(argv),
        }


def run_sync(target: str, *, runner=run_command,
             timeout: float = SYNC_TIMEOUT) -> dict:
    """Run one allow-listed sync target (never an arbitrary argv)."""
    argv = SYNC_TARGETS.get(target)
    if argv is None:
        return {"ok": False, "error": f"unknown sync target {target!r}"}
    result = runner(argv, timeout)
    result["cmd"] = "sync"
    result["target"] = target
    return result


def unit_state(unit: str, *, runner=run_command) -> dict:
    """`is-active` plus the last exit timestamp for one user unit."""
    active = runner(["systemctl", "--user", "is-active", unit], STATUS_TIMEOUT)
    last = runner(["systemctl", "--user", "show", unit,
                   "-p", "ExecMainExitTimestamp", "--value"], STATUS_TIMEOUT)
    state = {
        "active": (active.get("output") or "").strip().splitlines()[:1] or ["unknown"],
    }
    state["active"] = state["active"][0] or "unknown"
    stamp = (last.get("output") or "").strip()
    if stamp:
        state["last_run"] = stamp
    return state


def services_status(*, runner=run_command) -> dict:
    """State of every pipeline unit the services track asks about."""
    return {
        "ok": True,
        "cmd": "status",
        "units": {unit: unit_state(unit, runner=runner) for unit in STATUS_UNITS},
    }


def _start_unit(unit: str, *, runner=run_command) -> dict:
    if unit not in ALLOWED_START_UNITS:
        return {"ok": False, "error": f"unit {unit!r} is not startable through this broker"}
    result = runner(["systemctl", "--user", "start", unit], SYNC_TIMEOUT)
    result["cmd"] = "start"
    result["unit"] = unit
    return result


BUSY_WAIT = 240.0


def _acquire(busy: threading.Lock, timeout: float) -> bool:
    """Wait up to `timeout` for the single-sync slot.

    Failing instantly on contention is wrong for the collector: it starts the
    mail sync and the spond sync in parallel on purpose, and both go through
    this broker. Waiting turns that into a short queue; the rate limit still
    caps how much a caller can pile up. `timeout=0` restores the old
    fail-fast behaviour (tests use it).
    """
    if timeout <= 0:
        return busy.acquire(blocking=False)
    return busy.acquire(timeout=timeout)


REQUEST_FIELDS = {
    "ping": {"cmd"},
    "status": {"cmd"},
    "start": {"cmd", "unit"},
    "sync": {"cmd", "target"},
}


def handle(request, *, sync_limiter: Limiter, status_limiter: Limiter,
           busy: threading.Lock, runner=run_command,
           busy_wait: float = BUSY_WAIT) -> dict:
    """Validate one request and run it. Pure enough to unit-test: every effect
    goes through `runner`, and the limiters/lock are injected."""
    if not isinstance(request, dict):
        return {"ok": False, "error": "request must be a JSON object"}
    cmd = request.get("cmd")
    fields = REQUEST_FIELDS.get(cmd) if isinstance(cmd, str) else None
    if fields is None:
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}
    unexpected = set(request) - fields
    if unexpected:
        return {"ok": False,
                "error": f"unexpected request fields: {', '.join(sorted(unexpected))}"}

    if cmd == "ping":
        return {"ok": True, "cmd": "ping", "version": VERSION, "pid": os.getpid()}

    if cmd == "status":
        if not status_limiter.allow():
            return {"ok": False, "error": "rate limit exceeded, try again later"}
        return services_status(runner=runner)

    if cmd == "start":
        unit = request.get("unit")
        if unit not in ALLOWED_START_UNITS:
            return {"ok": False, "error": f"unit {unit!r} is not startable through this broker"}
        if not sync_limiter.allow():
            return {"ok": False, "error": "rate limit exceeded, try again later"}
        if not _acquire(busy, busy_wait):
            return {"ok": False, "error": "busy: another request is running"}
        try:
            return _start_unit(unit, runner=runner)
        finally:
            busy.release()

    target = request.get("target")
    if target == "all":
        targets = list(SYNC_TARGETS)
    elif isinstance(target, str) and target in SYNC_TARGETS:
        targets = [target]
    else:
        return {"ok": False, "error": f"unknown sync target {target!r}"}
    if not sync_limiter.allow():
        return {"ok": False, "error": "rate limit exceeded, try again later"}
    if not _acquire(busy, busy_wait):
        return {"ok": False, "error": "busy: another request is running"}
    try:
        results = [run_sync(t, runner=runner) for t in targets]
    finally:
        busy.release()
    if len(results) == 1:
        return results[0]
    return {"ok": all(r.get("ok") for r in results), "cmd": "sync",
            "target": "all", "results": results}


def serve(listen: str, *, runner=run_command) -> int:
    """Run the broker until killed. One thread per connection, max 8."""
    listen_dir = os.path.dirname(listen) or "."
    os.makedirs(listen_dir, exist_ok=True)
    try:
        os.unlink(listen)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(listen)
    os.chmod(listen, 0o660)  # UMask is belt; be explicit about the socket
    server.listen(8)
    sync_limiter = Limiter(SYNC_BURST, SYNC_PERIOD)
    status_limiter = Limiter(STATUS_BURST, STATUS_PERIOD)
    busy = threading.Lock()
    print(f"[diary-sync] listening on {listen}", file=sys.stderr, flush=True)

    def _serve_one(conn: socket.socket) -> None:
        try:
            conn.settimeout(STATUS_TIMEOUT)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    conn.sendall(b'{"ok": false, "error": "request too large"}\n')
                    return
            raw = buf.split(b"\n", 1)[0]
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                conn.sendall(b'{"ok": false, "error": "parse error"}\n')
                return
            reply = handle(request, sync_limiter=sync_limiter,
                           status_limiter=status_limiter, busy=busy, runner=runner)
            if not reply.get("ok"):
                print(f"[diary-sync] {request.get('cmd')!r} -> "
                      f"{reply.get('error', 'failed')}", file=sys.stderr, flush=True)
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    while True:
        try:
            conn, _ = server.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()
    return 0


# --- client ------------------------------------------------------------------


def call(payload: dict, socket_path: str = DEFAULT_SOCKET,
         timeout: float = SYNC_TIMEOUT + 30) -> dict:
    """Send one request to the broker. Raises BrokerUnavailable when it is not
    reachable (missing socket, refused connection, no reply)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise BrokerUnavailable("broker closed the connection without a reply")
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError) as exc:
        raise BrokerUnavailable(f"no broker at {socket_path}: {exc}") from exc
    except socket.timeout as exc:  # noqa: UP041 — alias of TimeoutError on 3.11+
        raise BrokerUnavailable(f"broker at {socket_path} timed out") from exc
    finally:
        sock.close()


def _print_status(status: dict) -> None:
    for unit, state in (status.get("units") or {}).items():
        active = state.get("active", "unknown")
        last = state.get("last_run")
        line = f"{unit:<26} {active:<9}"
        if last:
            line += f" last: {last}"
        print(line.rstrip())


def _fallback_status() -> dict:
    return services_status()


GLOBAL_FLAGS = ("--json", "--fallback")


def _normalise_argv(argv: list[str]) -> list[str]:
    """Accept the global flags before *or* after the subcommand.

    ``diary-sync status --json`` is what people type (and what sjekk-collect
    builds); argparse only takes them before the subcommand, so hoist them.
    """
    flags = [a for a in argv if a in GLOBAL_FLAGS]
    rest = [a for a in argv if a not in GLOBAL_FLAGS]
    return flags + rest


def client_main(argv: list[str] | None = None) -> int:
    argv = _normalise_argv(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(
        prog="diary-sync",
        description="Ask the diary-sync broker (or, with --fallback, do it here).")
    parser.add_argument("--socket", default=os.environ.get("DIARY_SYNC_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--json", action="store_true", help="machine-readable reply")
    parser.add_argument("--fallback", action="store_true",
                        help="when the broker is unreachable, do the work in this process")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="run one sync once")
    p_sync.add_argument("target", choices=[*SYNC_TARGETS, "all"])
    p_start = sub.add_parser("start", help="start an allow-listed unit")
    p_start.add_argument("unit", choices=list(ALLOWED_START_UNITS))
    sub.add_parser("status", help="unit state for the pipeline")
    sub.add_parser("ping", help="is the broker alive?")

    args = parser.parse_args(argv)
    payload: dict = {"cmd": args.cmd}
    if args.cmd == "sync":
        payload["target"] = args.target
    elif args.cmd == "start":
        payload["unit"] = args.unit

    try:
        reply = call(payload, args.socket)
    except BrokerUnavailable as exc:
        if not args.fallback:
            print(f"diary-sync: {exc} (use --fallback to run locally)", file=sys.stderr)
            return 2
        print(f"diary-sync: {exc} — running locally", file=sys.stderr)
        reply = _local(args.cmd, payload)

    if args.json:
        print(json.dumps(reply, ensure_ascii=False))
    elif reply.get("cmd") == "ping":
        print(f"ok (broker {reply.get('version')}, pid {reply.get('pid')})" if reply.get("ok")
              else f"diary-sync: {reply.get('error')}")
    elif reply.get("cmd") == "status":
        if not reply.get("ok"):
            print(f"diary-sync: {reply.get('error')}", file=sys.stderr)
            return 1
        _print_status(reply)
    else:
        out = (reply.get("output") or "").strip()
        if out:
            print(out)
        if not reply.get("ok"):
            print(f"diary-sync: {reply.get('error', 'failed')}", file=sys.stderr)
            return 1
    return 0


def _local(cmd: str, payload: dict) -> dict:
    """Same work, no socket — used by --fallback in a plain kek shell."""
    # Pass the runner explicitly: the keyword defaults are bound at import
    # time, so a test (or an embedder) that replaces module-level
    # `run_command` would otherwise be ignored.
    if cmd == "sync":
        target = payload["target"]
        if target == "all":
            results = [run_sync(t, runner=run_command) for t in SYNC_TARGETS]
            return {"ok": all(r.get("ok") for r in results), "cmd": "sync",
                    "target": "all", "results": results}
        return run_sync(target, runner=run_command)
    if cmd == "start":
        return _start_unit(payload["unit"], runner=run_command)
    if cmd == "status":
        return services_status(runner=run_command)
    return {"ok": True, "cmd": "ping", "version": VERSION, "pid": os.getpid()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--listen", default=os.environ.get("DIARY_SYNC_SOCKET", DEFAULT_SOCKET))
    args = parser.parse_args(argv)
    return serve(args.listen)


if __name__ == "__main__":
    sys.exit(main())
