#!/usr/bin/python3
"""Root-owned diary lock controller and local owner break-glass client.

The daemon derives caller identity from SO_PEERCRED, persists LOCKING, LOCKED,
and ATTENDED states, verifies conflicting systemd mode units, and fails locked
when state, audit, or supervision is unavailable. Mode units are markers only;
network, DNS, broker, and turn enforcement belong to separate changes.
"""
from __future__ import annotations

import argparse
import grp
import json
import math
import os
import pwd
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, cast

SCHEMA_VERSION = 1
DEFAULT_SOCKET = "/run/diary-lock/socket"
DEFAULT_STATE = "/var/lib/diary-lock/state.json"
DEFAULT_AUDIT_SOCKET = "/run/diary-audit/socket"
DEFAULT_GROUP = "diary-lock-control"
DEFAULT_OWNER = "kek"
DEFAULT_ATTENDED_SECONDS = 15 * 60
AUDIT_HEALTH_INTERVAL = 30.0
MAX_REQUEST_BYTES = 4096
MAX_STATE_BYTES = 4096
STATES = frozenset({"LOCKING", "LOCKED", "ATTENDED"})


class LockError(ValueError):
    """The controller cannot prove a safe state or request."""


class RequestRejected(LockError):
    """A caller or request is unauthorized without implying controller failure."""


def peer_credentials(conn: socket.socket) -> dict[str, int]:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return {"pid": pid, "uid": uid, "gid": gid}


def sd_notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = chr(0) + address[1:]
    elif not address.startswith("/"):
        raise LockError("NOTIFY_SOCKET must be an absolute path or abstract socket")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(2.0)
        sock.sendto(message.encode("ascii"), address)
    except OSError as exc:
        raise LockError("could not notify systemd") from exc
    finally:
        sock.close()
    return True


def request(socket_path: str, value: dict, timeout: float = 10.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                raise LockError("controller response is too large")
        reply = json.loads(data)
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot use controller socket: {exc}") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error", "controller request failed") if isinstance(reply, dict) else "controller request failed"
        raise LockError(error)
    return reply


class AuditClient:
    def __init__(self, socket_path: str = DEFAULT_AUDIT_SOCKET) -> None:
        self.socket_path = socket_path

    def ping(self) -> None:
        reply = request(self.socket_path, {"cmd": "ping"}, timeout=2.0)
        if reply.get("pong") is not True:
            raise LockError("audit daemon returned an invalid health response")

    def emit(
        self,
        event: str,
        operation: str,
        decision: str,
        state: str,
        reason: str,
        caller: dict[str, int] | None = None,
    ) -> None:
        value: dict[str, Any] = {
            "event": event,
            "component": "diary-lockd",
            "operation": operation,
            "decision": decision,
            "lock_state": state.lower(),
            "reason": reason,
        }
        if caller is not None:
            value["caller"] = caller
        request(self.socket_path, {"cmd": "write", "event": value}, timeout=5.0)


class StateStore:
    def __init__(self, path: str, boot_id: str) -> None:
        self.path = Path(path)
        self.boot_id = boot_id

    def _check_parent(self) -> None:
        info = self.path.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise LockError("lock state directory must be private to root")

    def load(self) -> dict[str, Any]:
        self._check_parent()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise LockError("lock state must be a regular non-symlink file") from exc
        try:
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise LockError("lock state must be a regular file")
                if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    raise LockError("lock state has unsafe ownership or mode")
                raw = handle.read(MAX_STATE_BYTES + 1)
        except OSError as exc:
            raise LockError("lock state is unreadable") from exc
        if len(raw) > MAX_STATE_BYTES:
            raise LockError("lock state is oversized")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LockError("lock state is invalid") from exc
        if not isinstance(value, dict) or set(value) != {
            "version", "state", "boot_id", "expires_at", "updated_at",
        }:
            raise LockError("lock state has invalid fields")
        value = cast(dict[str, Any], value)
        if value["version"] != SCHEMA_VERSION or isinstance(value["version"], bool):
            raise LockError("unsupported lock state version")
        if not isinstance(value["state"], str) or value["state"] not in STATES:
            raise LockError("lock state is unknown")
        if not isinstance(value["boot_id"], str):
            raise LockError("lock state has an invalid boot ID")
        for field in ("expires_at", "updated_at"):
            item = value[field]
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
            ):
                raise LockError("lock state has an invalid timestamp")
        if value["updated_at"] is None:
            raise LockError("lock state has no update timestamp")
        if (value["state"] == "ATTENDED") != (value["expires_at"] is not None):
            raise LockError("lock state has inconsistent attendance expiry")
        return value

    def save(self, state: str, now: float, expires_at: float | None = None) -> dict[str, Any]:
        if state not in STATES or (state == "ATTENDED") != (expires_at is not None):
            raise LockError("invalid controller state")
        self._check_parent()
        value = {
            "version": SCHEMA_VERSION,
            "state": state,
            "boot_id": self.boot_id,
            "expires_at": expires_at,
            "updated_at": now,
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return value


class SystemdModes:
    def __init__(self, runner: Callable[..., Any] = subprocess.run) -> None:
        self.runner = runner

    def _run(self, args: list[str]) -> Any:
        try:
            return self.runner(
                ["/usr/bin/systemctl", *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LockError("systemd mode operation failed") from exc

    def _active(self, unit: str) -> bool:
        result = self._run(["is-active", "--quiet", unit])
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise LockError(f"cannot verify {unit}")

    def activate(self, mode: str) -> None:
        if mode not in {"locked", "attended"}:
            raise LockError("unsupported mode unit")
        selected = f"diary-{mode}.service"
        result = self._run(["start", selected])
        if result.returncode != 0:
            raise LockError(f"could not activate {selected}")
        if not self._active(selected):
            raise LockError(f"{selected} did not become active")
        forbidden = ["diary-attended.service" if mode == "locked" else "diary-locked.service"]
        if mode == "attended":
            forbidden.append("diary-lock-failsafe.service")
        if any(self._active(unit) for unit in forbidden):
            raise LockError("conflicting mode unit remains active")


class ModeManager(Protocol):
    def activate(self, mode: str) -> None: ...


class AuditSink(Protocol):
    def ping(self) -> None: ...

    def emit(
        self,
        event: str,
        operation: str,
        decision: str,
        state: str,
        reason: str,
        caller: dict[str, int] | None = None,
    ) -> None: ...


class Controller:
    def __init__(
        self,
        store: StateStore,
        modes: ModeManager,
        audit: AuditSink,
        attended_seconds: int = DEFAULT_ATTENDED_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 60 <= attended_seconds <= 3600:
            raise LockError("attended duration must be between 60 and 3600 seconds")
        self.store = store
        self.modes = modes
        self.audit = audit
        self.attended_seconds = attended_seconds
        self.clock = clock
        self.monotonic = monotonic
        self.state = "LOCKING"
        self.expires_at: float | None = None
        self.deadline_monotonic: float | None = None
        self.lock = threading.RLock()
        self.next_audit_check = 0.0

    def _save(self, state: str, expires_at: float | None = None) -> None:
        self.store.save(state, self.clock(), expires_at)
        self.state = state
        self.expires_at = expires_at
        if state != "ATTENDED":
            self.deadline_monotonic = None

    def status(self) -> dict[str, Any]:
        with self.lock:
            remaining = None
            expires_at = self.expires_at
            if self.deadline_monotonic is not None:
                remaining = max(0, math.ceil(self.deadline_monotonic - self.monotonic()))
                expires_at = self.clock() + remaining
            return {
                "ok": True,
                "state": self.state,
                "expires_at": expires_at,
                "remaining_seconds": remaining,
                "boot_id": self.store.boot_id,
            }

    def startup(self) -> None:
        reason = "initial_locked"
        try:
            previous = self.store.load()
        except FileNotFoundError:
            pass
        except LockError:
            reason = "unreadable_state"
        else:
            reason = "controller_restart" if previous["boot_id"] == self.store.boot_id else "boot"
        self.enter_locked(reason)

    def enter_locked(self, reason: str) -> None:
        with self.lock:
            self._save("LOCKING")
            self.modes.activate("locked")
            self._save("LOCKED")
            self.audit.emit("controller.transition", "lock", "observed", "LOCKED", reason)
            self.next_audit_check = self.monotonic() + AUDIT_HEALTH_INTERVAL

    def break_glass(self, peer: dict[str, int]) -> dict[str, Any]:
        with self.lock:
            self.audit.emit(
                "controller.break_glass", "request", "observed", self.state,
                "local_owner_override", peer,
            )
            self._save("LOCKING")
            self.modes.activate("attended")
            self.deadline_monotonic = self.monotonic() + self.attended_seconds
            self._save("ATTENDED", self.clock() + self.attended_seconds)
            self.audit.emit(
                "controller.break_glass", "attended", "allow", "ATTENDED",
                "local_owner_override", peer,
            )
            self.next_audit_check = self.monotonic() + AUDIT_HEALTH_INTERVAL
            return self.status()

    def maintenance(self) -> None:
        with self.lock:
            now = self.monotonic()
            if (
                self.state == "ATTENDED" and self.deadline_monotonic is not None
                and now >= self.deadline_monotonic
            ):
                self.enter_locked("attendance_expired")
            if now >= self.next_audit_check:
                self.audit.ping()
                self.next_audit_check = now + AUDIT_HEALTH_INTERVAL

    def failsafe(self, reason: str) -> None:
        with self.lock:
            self.state = "LOCKING"
            self.expires_at = None
            self.deadline_monotonic = None
            errors: list[Exception] = []
            try:
                self.store.save("LOCKING", self.clock())
            except (LockError, OSError) as exc:
                errors.append(exc)
            try:
                self.modes.activate("locked")
            except (LockError, OSError) as exc:
                errors.append(exc)
            else:
                try:
                    self.store.save("LOCKED", self.clock())
                except (LockError, OSError) as exc:
                    errors.append(exc)
                else:
                    self.state = "LOCKED"
            try:
                self.audit.emit("controller.failsafe", "lock", "error", self.state, reason)
            except (LockError, OSError) as exc:
                errors.append(exc)
            if errors:
                raise LockError(
                    "failsafe could not prove locked mode: " + "; ".join(str(error) for error in errors)
                ) from errors[0]

    def handle(self, value: object, peer: dict[str, int], owner_uid: int) -> dict[str, Any]:
        if peer["uid"] not in {0, owner_uid}:
            raise RequestRejected("caller is not authorized")
        if not isinstance(value, dict) or set(value) != {"cmd"}:
            raise RequestRejected("invalid controller request")
        value = cast(dict[str, Any], value)
        if value["cmd"] == "status":
            return self.status()
        if value["cmd"] == "lock":
            self.enter_locked("explicit_lock")
            return self.status()
        if value["cmd"] == "break_glass":
            return self.break_glass(peer)
        raise RequestRejected("unknown controller request")


class LockRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        conn = self.request
        server = cast(LockDaemon, self.server)
        conn.settimeout(10.0)
        peer = peer_credentials(conn)
        try:
            if not server.authorized(peer):
                raise RequestRejected("caller is not authorized")
            line = conn.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
            if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise RequestRejected("controller request is missing, oversized, or incomplete")
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestRejected("controller request is not valid JSON") from exc
            if server.read_only(peer):
                if not isinstance(value, dict) or set(value) != {"cmd"} or value.get("cmd") != "status":
                    raise RequestRejected("read-only caller may only request controller status")
                reply = server.controller.status()
            else:
                reply = server.controller.handle(value, peer, server.owner_uid)
        except RequestRejected as exc:
            reply = {"ok": False, "error": str(exc)}
        except (LockError, OSError) as exc:
            try:
                server.controller.failsafe("request_failed")
            except Exception as failsafe_error:
                server.latch_failure(failsafe_error)
                reply = {"ok": False, "error": f"{exc}; {failsafe_error}"}
            else:
                server.latch_failure(exc)
                reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
        except OSError:
            pass


class LockDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: str,
        controller: Controller,
        group: str,
        owner_uid: int,
        dashboard_user: str | None = None,
    ) -> None:
        self.path = path
        self.controller = controller
        self.group = group
        self.owner_uid = owner_uid
        self.dashboard_user = dashboard_user
        self.fatal_error: Exception | None = None
        self.fatal_lock = threading.Lock()
        parent = Path(path).parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise LockError("controller socket directory must be private to root")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if info.st_mode & 0o170000 != 0o140000 or info.st_uid != os.geteuid():
                raise LockError("refusing to replace a non-socket or foreign socket")
            os.unlink(path)
        super().__init__(path, LockRequestHandler)
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
        os.chmod(path, 0o660)

    def read_only(self, peer: dict[str, int]) -> bool:
        if self.dashboard_user is None or peer["uid"] in {0, self.owner_uid}:
            return False
        try:
            name = pwd.getpwuid(peer["uid"]).pw_name
        except KeyError:
            return False
        return name == self.dashboard_user and name not in {"agent", "diary-pipeline"}

    def authorized(self, peer: dict[str, int]) -> bool:
        if peer["uid"] in {0, self.owner_uid}:
            return True
        if not self.read_only(peer):
            return False
        try:
            group = grp.getgrnam(self.group)
            name = pwd.getpwuid(peer["uid"]).pw_name
        except KeyError:
            return False
        return peer["gid"] == group.gr_gid or name in group.gr_mem

    def latch_failure(self, error: Exception) -> None:
        with self.fatal_lock:
            if self.fatal_error is None:
                self.fatal_error = error

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def boot_id(path: str = "/proc/sys/kernel/random/boot_id") -> str:
    try:
        value = Path(path).read_text(encoding="ascii").strip()
    except OSError as exc:
        raise LockError("cannot read kernel boot ID") from exc
    if len(value) != 36 or any(character not in "0123456789abcdef-" for character in value):
        raise LockError("kernel boot ID is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show the authoritative controller state")
    sub.add_parser("lock", help="enter locked mode immediately")
    sub.add_parser("break-glass", help="enter attended mode without WebAuthn and audit the override")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "break-glass":
            owner_uid = pwd.getpwnam(DEFAULT_OWNER).pw_uid
            if os.geteuid() not in {0, owner_uid}:
                raise LockError(f"break-glass must run as {DEFAULT_OWNER}")
            if not sys.stdin.isatty():
                raise LockError("break-glass requires a local interactive terminal")
            print("WARNING: break-glass bypasses WebAuthn and creates a prominent audit event.", file=sys.stderr)
            if input("Type ATTENDED to confirm: ") != "ATTENDED":
                raise LockError("break-glass was not confirmed")
        command = args.cmd.replace("-", "_")
        reply = request(args.socket, {"cmd": command})
        print(json.dumps(reply, indent=2, sort_keys=True))
        return 0
    except (KeyError, LockError) as exc:
        print(f"diary-lock: {exc}", file=sys.stderr)
        return 2


def daemon_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the root-owned diary lock controller.")
    parser.add_argument("--listen", default=DEFAULT_SOCKET)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--audit-socket", default=DEFAULT_AUDIT_SOCKET)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--dashboard-user")
    parser.add_argument("--attended-seconds", type=int, default=DEFAULT_ATTENDED_SECONDS)
    parser.add_argument("--boot-id-file", default="/proc/sys/kernel/random/boot_id")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-lockd must run as root")
    server: LockDaemon | None = None
    controller: Controller | None = None
    try:
        controller = Controller(
            StateStore(args.state, boot_id(args.boot_id_file)),
            SystemdModes(),
            AuditClient(args.audit_socket),
            args.attended_seconds,
        )
        controller.startup()
        server = LockDaemon(
            args.listen,
            controller,
            args.group,
            pwd.getpwnam(args.owner).pw_uid,
            args.dashboard_user,
        )
        server.timeout = 1.0
        sd_notify("READY=1")
    except (KeyError, LockError, OSError) as exc:
        if controller is not None:
            try:
                controller.failsafe("startup_failed")
            except (LockError, OSError) as failsafe_error:
                print(f"diary-lockd: {failsafe_error}", file=sys.stderr)
        if server is not None:
            server.server_close()
        print(f"diary-lockd: {exc}", file=sys.stderr)
        return 1
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping and server.fatal_error is None:
            server.handle_request()
            if server.fatal_error is not None:
                break
            controller.maintenance()
            sd_notify("WATCHDOG=1")
        if server.fatal_error is not None:
            raise server.fatal_error
    except (LockError, OSError) as exc:
        if server.fatal_error is None:
            try:
                controller.failsafe("supervision_failed")
            except (LockError, OSError) as failsafe_error:
                print(f"diary-lockd: {failsafe_error}", file=sys.stderr)
        print(f"diary-lockd: {exc}", file=sys.stderr)
        return 1
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "diary-lockd":
        raise SystemExit(daemon_main())
    raise SystemExit(main())
