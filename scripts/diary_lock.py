#!/usr/bin/python3
"""Root-owned diary lock controller and local owner break-glass client.

The daemon derives caller identity from SO_PEERCRED, persists LOCKING, LOCKED,
and ATTENDED states, verifies conflicting systemd mode units, and fails locked
when state, audit, cgroup identity, or supervision is unavailable. Mode units
own their approved enforcement hooks; optional turn enforcement uses per-turn
systemd scopes, and DNS and broker enforcement stay separate.
"""
from __future__ import annotations

import argparse
import grp
import json
import math
import os
import pwd
import re
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
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
        result = self._run(["restart", selected])
        if result.returncode != 0:
            raise LockError(f"could not activate {selected}")
        if not self._active(selected):
            raise LockError(f"{selected} did not become active")
        forbidden = ["diary-attended.service" if mode == "locked" else "diary-locked.service"]
        if mode == "attended":
            forbidden.append("diary-lock-failsafe.service")
        if any(self._active(unit) for unit in forbidden):
            raise LockError("conflicting mode unit remains active")

    def verify_network_audit(self) -> None:
        if not self._active("diary-network-auditd.service"):
            raise LockError("NFLOG audit forwarder is unavailable")


CgroupSet = tuple[str, str, str]
DEFAULT_CGROUP_SETS: tuple[CgroupSet, ...] = (("inet", "diary_agent_audit", "agent_egress"),)
AGENT_SLICE = "agent.slice"
AGENT_SERVICES = ("dsh-web.service", "kimi-web.service")
TURN_SCOPE_PREFIXES = ("dsh-turn-", "kimi-turn-", "diary-dsh-turn-", "diary-kimi-turn-")


def cgroup_set(value: str) -> CgroupSet:
    parts = tuple(value.split(":"))
    if len(parts) != 3 or any(not part for part in parts):
        raise argparse.ArgumentTypeError("cgroup set must be FAMILY:TABLE:SET")
    if any(any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in part) for part in parts):
        raise argparse.ArgumentTypeError("cgroup set contains invalid characters")
    return cast(CgroupSet, parts)


class NftCgroupIdentity:
    def __init__(
        self,
        sets: tuple[CgroupSet, ...] = DEFAULT_CGROUP_SETS,
        cgroup_root: str = "/sys/fs/cgroup",
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        if not sets:
            raise LockError("at least one cgroup identity set is required")
        self.sets = sets
        self.cgroup_root = Path(cgroup_root)
        self.runner = runner

    def _run(self, argv: list[str]) -> Any:
        try:
            return self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LockError("cgroup identity operation failed") from exc

    def _properties(self, unit: str) -> dict[str, str]:
        result = self._run([
            "/usr/bin/systemctl", "show", unit,
            "--property=ActiveState", "--property=ControlGroup",
        ])
        if result.returncode != 0:
            raise LockError(f"cannot inspect {unit}")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line:
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in {"ActiveState", "ControlGroup"} or key in values:
                raise LockError(f"systemd returned invalid properties for {unit}")
            values[key] = value
        if set(values) != {"ActiveState", "ControlGroup"}:
            raise LockError(f"systemd omitted cgroup properties for {unit}")
        return values

    def _cgroup_path(self, control_group: str) -> Path:
        if not control_group.startswith("/") or any(
            part in {"", ".", ".."} for part in control_group.split("/")[1:]
        ):
            raise LockError("systemd returned an unsafe cgroup path")
        return Path(control_group)

    def _cgroup_id(self, control_group: str) -> int:
        path = self._cgroup_path(control_group)
        try:
            info = (self.cgroup_root / path.relative_to("/")).stat()
        except OSError as exc:
            raise LockError(f"cannot stat cgroup {control_group}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise LockError(f"cgroup {control_group} is not a directory")
        return info.st_ino

    def _members(self, selector: CgroupSet) -> set[int]:
        family, table, name = selector
        result = self._run(["/usr/bin/nft", "--json", "list", "set", family, table, name])
        if result.returncode != 0:
            raise LockError(f"cannot read nftables set {table}/{name}")
        try:
            document = json.loads(result.stdout)
            records = document["nftables"]
            matches = [
                record["set"] for record in records
                if isinstance(record, dict) and "set" in record
                and record["set"].get("family") == family
                and record["set"].get("table") == table
                and record["set"].get("name") == name
            ]
            if len(matches) != 1 or not isinstance(matches[0], dict):
                raise LockError(f"nftables returned an ambiguous set {table}/{name}")
            value = matches[0]
            if value.get("type") != "cgroupv2":
                raise LockError(f"nftables set {table}/{name} is not a cgroupv2 set")
            elements = value.get("elem", [])
            if not isinstance(elements, list) or any(isinstance(item, bool) or not isinstance(item, int) for item in elements):
                raise LockError(f"nftables set {table}/{name} has invalid members")
            return set(cast(list[int], elements))
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise LockError(f"nftables returned invalid set data for {table}/{name}") from exc

    def _active_cgroup(self, unit: str) -> str:
        properties = self._properties(unit)
        if properties["ActiveState"] != "active":
            raise LockError(f"{unit} is not active")
        if not properties["ControlGroup"]:
            raise LockError(f"{unit} has no control group")
        return properties["ControlGroup"]

    def _verify_slice(self) -> str:
        control_group = self._active_cgroup(AGENT_SLICE)
        if control_group != f"/{AGENT_SLICE}":
            raise LockError(f"{AGENT_SLICE} has an unexpected control group")
        cgroup_id = self._cgroup_id(control_group)
        for selector in self.sets:
            if cgroup_id not in self._members(selector):
                family, table, name = selector
                raise LockError(f"{AGENT_SLICE} is missing from {family}:{table}:{name}")
        return control_group

    def _verify_descendant(
        self, unit: str, slice_cgroup: str, allow_inactive: bool = False,
    ) -> None:
        properties = self._properties(unit)
        if allow_inactive and properties["ActiveState"] == "inactive":
            return
        if properties["ActiveState"] != "active":
            raise LockError(f"{unit} is not active")
        control_group = properties["ControlGroup"]
        if not control_group:
            raise LockError(f"{unit} has no control group")
        self._cgroup_path(control_group)
        if not control_group.startswith(slice_cgroup + "/"):
            raise LockError(f"{unit} escaped {AGENT_SLICE}")
        self._cgroup_id(control_group)

    def _validate_scope_name(self, unit: str) -> None:
        if not (
            unit.startswith(TURN_SCOPE_PREFIXES)
            and unit.endswith(".scope")
            and all(character in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in unit)
        ):
            raise LockError("invalid agent turn scope")

    def _turn_scopes(self) -> list[str]:
        result = self._run([
            "/usr/bin/systemctl", "list-units", "--type=scope", "--state=active",
            "--no-legend", "--no-pager", "--plain",
        ])
        if result.returncode != 0:
            raise LockError("cannot list active agent turn scopes")
        scopes: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4:
                raise LockError("systemd returned malformed scope data")
            unit = fields[0]
            if unit.startswith(TURN_SCOPE_PREFIXES):
                self._validate_scope_name(unit)
                scopes.append(unit)
        return scopes

    def verify(self) -> None:
        slice_cgroup = self._verify_slice()
        for unit in AGENT_SERVICES:
            self._verify_descendant(unit, slice_cgroup)
        for unit in self._turn_scopes():
            self._verify_descendant(unit, slice_cgroup, allow_inactive=True)

    def verify_scope(self, unit: str) -> None:
        self._validate_scope_name(unit)
        slice_cgroup = self._verify_slice()
        self._verify_descendant(unit, slice_cgroup)


class SystemdUnavailable(LockError):
    """The agent user manager cannot answer; PID containment is required."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    start_time: int
    uid: int


def process_identity(pid: int, proc_root: str = "/proc") -> ProcessIdentity:
    try:
        raw = Path(proc_root, str(pid), "stat").read_text(encoding="ascii")
        fields = raw.rsplit(")", 1)[1].strip().split()
        ppid = int(fields[1])
        start_time = int(fields[19])
        uid = Path(proc_root, str(pid)).stat().st_uid
    except (OSError, IndexError, ValueError) as exc:
        raise LockError(f"cannot pin PID {pid} identity") from exc
    if pid <= 0 or ppid < 0 or start_time < 0:
        raise LockError(f"invalid PID {pid} identity")
    return ProcessIdentity(pid, ppid, start_time, uid)


def process_state(pid: int, proc_root: str = "/proc") -> str:
    try:
        raw = Path(proc_root, str(pid), "stat").read_text(encoding="ascii")
        return raw.rsplit(")", 1)[1].strip().split()[0]
    except (OSError, IndexError) as exc:
        raise LockError(f"cannot read PID {pid} state") from exc


def signal_process(identity: ProcessIdentity, signum: int) -> None:
    try:
        fd = os.pidfd_open(identity.pid)
    except ProcessLookupError:
        return
    try:
        try:
            current = process_identity(identity.pid)
        except LockError:
            if Path(f"/proc/{identity.pid}").exists():
                raise
            return
        if current == identity:
            signal.pidfd_send_signal(fd, signum)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def process_cgroup(pid: int, proc_root: str = "/proc") -> str:
    try:
        lines = Path(proc_root, str(pid), "cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise LockError(f"cannot read PID {pid} cgroup") from exc
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise LockError(f"PID {pid} has no unambiguous cgroup v2 identity")
    return lines[0][3:]


class AgentProcesses:
    def __init__(
        self,
        uid: int,
        proc_root: str = "/proc",
        eligible: Callable[[ProcessIdentity], bool] | None = None,
        manager_pid: int | None = None,
    ) -> None:
        self.uid = uid
        self.proc_root = proc_root
        self.eligible = eligible
        self.manager_pid = manager_pid
        self.manager_prefix = f"/user.slice/user-{uid}.slice/user@{uid}.service/"

    def is_workload(self, pid: int) -> bool:
        cgroup = process_cgroup(pid, self.proc_root)
        if not cgroup.startswith(self.manager_prefix):
            return False
        relative = cgroup[len(self.manager_prefix):]
        parts = relative.split("/")
        if parts[0] == "init.scope":
            return self.manager_pid is None or pid != self.manager_pid
        if any(part.endswith(".scope") for part in parts):
            return True
        if len(parts) >= 2 and parts[0] == "app.slice":
            return parts[1].endswith(".service")
        return False

    def processes(self) -> list[ProcessIdentity]:
        result: list[ProcessIdentity] = []
        try:
            entries = list(Path(self.proc_root).iterdir())
        except OSError as exc:
            raise LockError("cannot enumerate agent processes") from exc
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                identity = process_identity(int(entry.name), self.proc_root)
                state = process_state(identity.pid, self.proc_root)
            except LockError:
                if not entry.exists():
                    continue
                raise
            if identity.uid != self.uid or state in {"X", "Z"}:
                continue
            if not self.is_workload(identity.pid):
                continue
            if self.eligible is not None and not self.eligible(identity):
                continue
            result.append(identity)
        return result

    def require_empty(self) -> None:
        if self.processes():
            raise LockError("agent processes remain after cancellation")

    def stop_and_kill(self) -> int:
        seen: set[ProcessIdentity] = set()
        for _attempt in range(100):
            current = self.processes()
            if not current:
                return len(seen)
            for identity in current:
                seen.add(identity)
                signal_process(identity, signal.SIGSTOP)
            for identity in self.processes():
                signal_process(identity, signal.SIGKILL)
            time.sleep(0.01)
        raise LockError("agent processes could not be quiesced")


class SystemdUser:
    def __init__(self, user: str | None, runner: Callable[..., Any] = subprocess.run) -> None:
        if user is not None and re.fullmatch(r"[a-z_][a-z0-9_-]*", user) is None:
            raise LockError("invalid agent user")
        self.user = user
        self.runner = runner

    def _run(self, tool: str, args: list[str]) -> Any:
        if tool not in {"systemctl", "busctl"}:
            raise LockError("unsupported systemd tool")
        argv = [f"/usr/bin/{tool}", "--user"]
        if self.user is not None:
            argv.append(f"--machine={self.user}@")
        argv.extend(args)
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        if self.user is None:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.geteuid()}"
        try:
            return self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemdUnavailable("systemd user operation failed") from exc

    def _run_system(self, args: list[str]) -> Any:
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
            raise SystemdUnavailable("could not recover the agent user manager") from exc

    def ensure_manager(self) -> None:
        try:
            self.all_scopes()
            return
        except SystemdUnavailable:
            pass
        uid = os.geteuid() if self.user is None else pwd.getpwnam(self.user).pw_uid
        result = self._run_system(["start", f"user@{uid}.service"])
        if result.returncode != 0:
            raise SystemdUnavailable("could not recover the agent user manager")
        for _attempt in range(100):
            try:
                self.all_scopes()
                return
            except SystemdUnavailable:
                time.sleep(0.05)
        raise SystemdUnavailable("agent user manager did not become ready")

    def manager_pid(self) -> int | None:
        uid = os.geteuid() if self.user is None else pwd.getpwnam(self.user).pw_uid
        result = self._run_system(["show", f"user@{uid}.service", "--property=MainPID", "--value"])
        try:
            pid = int(result.stdout.strip())
        except (AttributeError, ValueError) as exc:
            raise LockError("cannot identify the agent user manager") from exc
        if result.returncode != 0:
            raise SystemdUnavailable("cannot identify the agent user manager")
        return pid if pid > 0 else None

    def active(self, unit: str) -> bool:
        result = self._run("systemctl", ["is-active", "--quiet", unit])
        if result.returncode == 0:
            return True
        if result.returncode in {3, 4}:
            return False
        raise SystemdUnavailable(f"cannot verify {unit}")

    def main_pid(self, service: str) -> int:
        result = self._run("systemctl", ["show", service, "--property=MainPID", "--value"])
        try:
            pid = int(result.stdout.strip())
        except (AttributeError, ValueError) as exc:
            raise LockError(f"cannot read {service} main PID") from exc
        if result.returncode != 0 or pid <= 0:
            raise LockError(f"{service} has no running main PID")
        return pid

    def service_identities(
        self, service: str, wait: bool = False,
    ) -> tuple[ProcessIdentity, ProcessIdentity]:
        for attempt in range(100 if wait else 1):
            supervisor_pid = self.main_pid(service)
            supervisor = process_identity(supervisor_pid)
            try:
                children = Path(f"/proc/{supervisor_pid}/task/{supervisor_pid}/children").read_text(
                    encoding="ascii"
                ).split()
                pids = [int(value) for value in children]
            except (OSError, ValueError) as exc:
                raise LockError(f"cannot identify {service} runtime child") from exc
            if len(pids) == 1 and pids[0] > 0:
                runtime = process_identity(pids[0])
                if runtime.ppid != supervisor.pid or runtime.uid != supervisor.uid:
                    raise LockError(f"{service} runtime has unexpected parentage or identity")
                return supervisor, runtime
            if len(pids) > 1:
                raise LockError(f"{service} must have exactly one runtime child")
            time.sleep(0.05)
        raise LockError(f"{service} must have exactly one runtime child")

    def runtime_pid(self, service: str) -> int:
        return self.service_identities(service)[1].pid

    def verify_identity(self, identity: ProcessIdentity) -> None:
        if process_identity(identity.pid) != identity:
            raise LockError(f"PID {identity.pid} was replaced during turn handling")

    def unit_path(self, unit: str) -> str:
        result = self._run("busctl", [
            "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit,
        ])
        if result.returncode != 0:
            raise SystemdUnavailable(f"cannot resolve {unit}")
        return result.stdout.strip()

    def pid_unit_path(self, pid: int) -> str:
        result = self._run("busctl", [
            "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "GetUnitByPID", "u", str(pid),
        ])
        if result.returncode != 0:
            raise SystemdUnavailable(f"cannot resolve PID {pid}")
        return result.stdout.strip()

    def verify_membership(self, unit: str, pid: int) -> None:
        expected = self.unit_path(unit)
        if self.pid_unit_path(pid) != expected:
            raise LockError(f"PID {pid} is not in {unit}")

    def start_scope(self, scope: str, pid: int) -> None:
        result = self._run("busctl", [
            "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartTransientUnit",
            "ssa(sv)a(sa(sv))", scope, "fail", "4",
            "PIDs", "au", "1", str(pid),
            "KillMode", "s", "control-group",
            "SendSIGKILL", "b", "1",
            "TimeoutStopUSec", "t", "10000000",
            "0",
        ])
        if result.returncode != 0:
            raise SystemdUnavailable(f"could not create {scope}")
        for _attempt in range(100):
            if self.active(scope):
                self.verify_membership(scope, pid)
                return
            time.sleep(0.05)
        raise LockError(f"{scope} did not become active")

    def attach(self, service: str, pid: int) -> None:
        result = self._run("busctl", [
            "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "AttachProcessesToUnit",
            "ssau", service, "", "1", str(pid),
        ])
        if result.returncode != 0:
            raise SystemdUnavailable(f"could not reattach PID {pid} to {service}")
        self.verify_membership(service, pid)

    def workload_services(self) -> list[str]:
        result = self._run("systemctl", [
            "list-units", "--type=service", "--all", "--plain", "--no-legend",
        ])
        if result.returncode != 0:
            raise SystemdUnavailable("cannot list agent workload services")
        return [parts[0] for line in result.stdout.splitlines() if (parts := line.split())]

    def all_scopes(self) -> list[str]:
        result = self._run("systemctl", [
            "list-units", "--type=scope", "--all", "--plain", "--no-legend",
        ])
        if result.returncode != 0:
            raise SystemdUnavailable("cannot list agent scopes")
        return [
            parts[0]
            for line in result.stdout.splitlines()
            if (parts := line.split()) and parts[0] != "init.scope"
        ]

    def scopes(self, runtime: str) -> list[str]:
        prefix = f"diary-{runtime}-turn-"
        return [
            scope for scope in self.all_scopes()
            if scope.startswith(prefix) and scope.endswith(".scope")
        ]

    def stop(self, unit: str, no_block: bool = False) -> None:
        args = ["stop"]
        if no_block:
            args.append("--no-block")
        result = self._run("systemctl", [*args, unit])
        if result.returncode != 0 and not no_block and self.active(unit):
            raise LockError(f"could not stop {unit}")
        if not no_block and self.active(unit):
            raise LockError(f"could not stop {unit}")

    def wait_inactive(self, unit: str) -> None:
        for _attempt in range(100):
            if not self.active(unit):
                return
            time.sleep(0.05)
        raise LockError(f"{unit} remains active after cancellation")

    def start(self, service: str) -> None:
        if self.active(service):
            raise LockError(f"refusing to reuse active {service}")
        result = self._run("systemctl", ["start", service])
        if result.returncode != 0 or not self.active(service):
            raise LockError(f"could not start {service}")

    def ensure_service(self, service: str) -> None:
        if not self.active(service):
            result = self._run("systemctl", ["start", service])
            if result.returncode != 0 or not self.active(service):
                raise LockError(f"could not restore {service}")


@dataclass(frozen=True)
class Turn:
    runtime: str
    session: str
    scope: str
    identity: ProcessIdentity

    @property
    def pid(self) -> int:
        return self.identity.pid


class SystemdTurns:
    def __init__(
        self,
        agent_user: str | None,
        attended: Callable[[], bool],
        runtimes: dict[str, str] | None = None,
        systemd: Any = None,
        processes: Any = None,
        contain_all_scopes: bool = True,
    ) -> None:
        self.runtimes = runtimes or {"dsh": "dsh-web.service", "kimi": "kimi-web.service"}
        for runtime, service in self.runtimes.items():
            if re.fullmatch(r"[a-z][a-z0-9-]*", runtime) is None:
                raise LockError("invalid agent runtime")
            if re.fullmatch(r"[a-z][a-z0-9-]*\.service", service) is None:
                raise LockError("invalid agent service")
        self.attended = attended
        self.systemd = systemd or SystemdUser(agent_user)
        if processes is None:
            uid = os.geteuid() if agent_user is None else pwd.getpwnam(agent_user).pw_uid
            processes = AgentProcesses(uid, manager_pid=self.systemd.manager_pid())
        self.processes = processes
        self.contain_all_scopes = contain_all_scopes
        self.current: dict[str, Turn] = {}
        self.retired: dict[str, tuple[ProcessIdentity, ProcessIdentity]] = {}
        self.lock = threading.RLock()

    def managed_scopes(self) -> set[str]:
        if self.contain_all_scopes:
            return set(self.systemd.all_scopes())
        return {
            scope
            for runtime in self.runtimes
            for scope in self.systemd.scopes(runtime)
        }

    def start(self, runtime: str, session: str) -> dict[str, Any]:
        with self.lock:
            if not self.attended():
                raise LockError("agent turns are not attended")
            if runtime not in self.runtimes:
                raise RequestRejected("unknown agent runtime")
            existing = self.current.get(runtime)
            if existing is not None:
                if existing.session != session:
                    raise LockError("concurrent in-process turns cannot have separate scopes")
                self.systemd.verify_identity(existing.identity)
                self.systemd.verify_membership(existing.scope, existing.pid)
                return {"ok": True, "scope": existing.scope}
            service = self.runtimes[runtime]
            _supervisor, identity = self.systemd.service_identities(service, wait=True)
            scope = f"diary-{runtime}-turn-{uuid.uuid4().hex}.scope"
            self.systemd.start_scope(scope, identity.pid)
            self.systemd.verify_identity(identity)
            turn = Turn(runtime, session, scope, identity)
            self.current[runtime] = turn
            return {"ok": True, "scope": scope}

    def finish(self, runtime: str, session: str) -> dict[str, Any]:
        with self.lock:
            if runtime not in self.runtimes:
                raise RequestRejected("unknown agent runtime")
            turn = self.current.get(runtime)
            if turn is None:
                return {"ok": True, "scope": None}
            if turn.session != session:
                raise LockError("turn completion does not match the active session")
            self.systemd.verify_identity(turn.identity)
            self.systemd.attach(self.runtimes[runtime], turn.pid)
            self.systemd.verify_identity(turn.identity)
            del self.current[runtime]
            return {"ok": True, "scope": turn.scope}

    def _capture_retired(self) -> None:
        for service in self.runtimes.values():
            try:
                self.retired[service] = self.systemd.service_identities(service)
            except (LockError, OSError):
                pass

    def _cancel_systemd(self) -> set[str]:
        self._capture_retired()
        seen: set[str] = set()
        for _attempt in range(100):
            errors: list[str] = []
            services = set(self.systemd.workload_services())
            active_services = {service for service in services if self.systemd.active(service)}
            for service in sorted(active_services):
                try:
                    self.systemd.stop(service, no_block=True)
                except LockError as exc:
                    errors.append(str(exc))
            for service in services:
                try:
                    self.systemd.wait_inactive(service)
                except LockError as exc:
                    errors.append(str(exc))
            active_scopes = {scope for scope in self.managed_scopes() if self.systemd.active(scope)}
            seen.update(active_scopes)
            for scope in sorted(active_scopes):
                try:
                    self.systemd.stop(scope)
                except LockError as exc:
                    errors.append(str(exc))
            if errors:
                raise LockError("; ".join(errors))
            if not active_services and not active_scopes:
                return seen
        raise LockError("agent workloads could not be quiesced")

    def cancel(self) -> int:
        with self.lock:
            try:
                seen = self._cancel_systemd()
            except LockError:
                seen = set()
            self.processes.stop_and_kill()
            self.processes.require_empty()
            try:
                seen = self._cancel_systemd() | seen
            except SystemdUnavailable:
                pass
            except LockError as exc:
                raise LockError(f"turn cancellation failed: {exc}") from exc
            self.current.clear()
            return len(seen)

    def attend(self) -> None:
        with self.lock:
            self.systemd.ensure_manager()
            self._cancel_systemd()
            self.processes.stop_and_kill()
            self.processes.require_empty()
            self._cancel_systemd()
            for service in self.systemd.workload_services():
                if self.systemd.active(service):
                    raise LockError(f"refusing to resume active {service}")
            self.systemd.ensure_service("dbus-broker.service")
            for service in self.runtimes.values():
                self.systemd.start(service)
                fresh = self.systemd.service_identities(service, wait=True)
                previous = self.retired.get(service)
                if previous is not None:
                    if fresh[0].pid == previous[0].pid or fresh[1].pid == previous[1].pid:
                        raise LockError(f"{service} reused a supervisor or runtime PID")
                    if fresh == previous:
                        raise LockError(f"{service} reused a previous process identity")
                self.retired[service] = fresh
            self.current.clear()


class TurnLifecycle(Protocol):
    def cancel(self) -> int: ...

    def attend(self) -> None: ...


class ModeManager(Protocol):
    def activate(self, mode: str) -> None: ...

    def verify_network_audit(self) -> None: ...


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


class CgroupIdentity(Protocol):
    def verify(self) -> None: ...

    def verify_scope(self, unit: str) -> None: ...


class Controller:
    def __init__(
        self,
        store: StateStore,
        modes: ModeManager,
        audit: AuditSink,
        attended_seconds: int = DEFAULT_ATTENDED_SECONDS,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        identity: CgroupIdentity | None = None,
    ) -> None:
        if not 60 <= attended_seconds <= 3600:
            raise LockError("attended duration must be between 60 and 3600 seconds")
        self.store = store
        self.modes = modes
        self.audit = audit
        self.attended_seconds = attended_seconds
        self.clock = clock
        self.monotonic = monotonic
        self.identity = identity
        self.state = "LOCKING"
        self.expires_at: float | None = None
        self.deadline_monotonic: float | None = None
        self.review_since = clock()
        self.lock = threading.RLock()
        self.next_audit_check = 0.0
        self.turns: TurnLifecycle | None = None

    def _activate(self, mode: str) -> None:
        self.modes.activate(mode)
        if self.identity is not None:
            self.identity.verify()

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
                "review_since": self.review_since,
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
            if self.state != "LOCKED":
                self.review_since = self.clock()
            self._save("LOCKING")
            cancelled = self.turns.cancel() if self.turns is not None else 0
            self._activate("locked")
            self.modes.verify_network_audit()
            self._save("LOCKED")
            if self.turns is not None:
                self.audit.emit(
                    "controller.turn_cancellation", "cancel", "observed", "LOCKED",
                    f"{reason}_scopes_{cancelled}",
                )
            self.audit.emit("controller.transition", "lock", "observed", "LOCKED", reason)
            self.next_audit_check = self.monotonic() + AUDIT_HEALTH_INTERVAL

    def _enter_attended(self, event: str, reason: str, peer: dict[str, int]) -> dict[str, Any]:
        self.audit.emit(event, "request", "observed", self.state, reason, peer)
        self._save("LOCKING")
        self.modes.verify_network_audit()
        if self.turns is not None:
            self.turns.attend()
        self._activate("attended")
        self.modes.verify_network_audit()
        self.deadline_monotonic = self.monotonic() + self.attended_seconds
        self._save("ATTENDED", self.clock() + self.attended_seconds)
        self.audit.emit(event, "attended", "allow", "ATTENDED", reason, peer)
        self.next_audit_check = self.monotonic() + AUDIT_HEALTH_INTERVAL
        return self.status()

    def break_glass(self, peer: dict[str, int]) -> dict[str, Any]:
        with self.lock:
            if self.state == "ATTENDED":
                raise RequestRejected("break-glass cannot extend attendance")
            return self._enter_attended(
                "controller.break_glass", "local_owner_override", peer,
            )

    def unlock(self, peer: dict[str, int]) -> dict[str, Any]:
        with self.lock:
            if self.state != "LOCKED":
                raise RequestRejected("fresh unlock requires locked state")
            return self._enter_attended("controller.unlock", "fresh_owner_ui", peer)

    def extend(self, peer: dict[str, int]) -> dict[str, Any]:
        with self.lock:
            if self.state != "ATTENDED" or self.deadline_monotonic is None:
                raise RequestRejected("attendance is not active")
            self.modes.verify_network_audit()
            self._activate("attended")
            self.modes.verify_network_audit()
            self.deadline_monotonic = self.monotonic() + self.attended_seconds
            self._save("ATTENDED", self.clock() + self.attended_seconds)
            self.audit.emit(
                "controller.attendance", "extend", "allow", "ATTENDED",
                "independent_owner_ui", peer,
            )
            return self.status()

    def maintenance(self) -> None:
        with self.lock:
            now = self.monotonic()
            if (
                self.state == "ATTENDED" and self.deadline_monotonic is not None
                and now >= self.deadline_monotonic
            ):
                self.enter_locked("attendance_expired")
            if self.identity is not None:
                self.identity.verify()
            if now >= self.next_audit_check:
                self.audit.ping()
                self.modes.verify_network_audit()
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
            if self.turns is not None:
                try:
                    self.turns.cancel()
                except (LockError, OSError) as exc:
                    errors.append(exc)
            try:
                self._activate("locked")
            except (LockError, OSError) as exc:
                errors.append(exc)
            else:
                try:
                    self.modes.verify_network_audit()
                except (LockError, OSError) as exc:
                    errors.append(exc)
                if not errors:
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

    def handle(
        self, value: object, peer: dict[str, int], owner_uid: int, ui_uid: int | None = None,
    ) -> dict[str, Any]:
        if peer["uid"] not in {0, owner_uid, ui_uid}:
            raise RequestRejected("caller is not authorized")
        if not isinstance(value, dict) or set(value) != {"cmd"}:
            raise RequestRejected("invalid controller request")
        value = cast(dict[str, Any], value)
        if value["cmd"] == "status":
            return self.status()
        if value["cmd"] == "lock":
            self.enter_locked("explicit_lock")
            return self.status()
        if value["cmd"] == "break_glass" and peer["uid"] in {0, owner_uid}:
            return self.break_glass(peer)
        if value["cmd"] == "unlock" and peer["uid"] == ui_uid:
            return self.unlock(peer)
        if value["cmd"] == "extend" and peer["uid"] == ui_uid:
            return self.extend(peer)
        raise RequestRejected("unknown or unauthorized controller request")


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
                reply = server.controller.handle(value, peer, server.owner_uid, server.ui_uid)
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
        ui_uid: int | None = None,
    ) -> None:
        self.path = path
        self.controller = controller
        self.group = group
        self.owner_uid = owner_uid
        self.dashboard_user = dashboard_user
        self.ui_uid = ui_uid
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
        if self.dashboard_user is None or peer["uid"] in {0, self.owner_uid, self.ui_uid}:
            return False
        try:
            name = pwd.getpwuid(peer["uid"]).pw_name
        except KeyError:
            return False
        return name == self.dashboard_user and name not in {"agent", "diary-pipeline"}

    def authorized(self, peer: dict[str, int]) -> bool:
        if peer["uid"] in {0, self.owner_uid, self.ui_uid}:
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


class TurnRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        conn = self.request
        server = cast(TurnDaemon, self.server)
        conn.settimeout(10.0)
        peer = peer_credentials(conn)
        try:
            if peer["uid"] != server.agent_uid:
                raise RequestRejected("turn caller is not the agent identity")
            line = conn.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
            if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise RequestRejected("turn request is missing, oversized, or incomplete")
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RequestRejected("turn request is not valid JSON") from exc
            if not isinstance(value, dict):
                raise RequestRejected("invalid turn request")
            value = cast(dict[str, Any], value)
            cmd = value.get("cmd")
            expected = {"cmd", "runtime", "session"}
            if set(value) != expected or cmd not in {"start", "finish"}:
                raise RequestRejected("invalid turn request fields")
            runtime = value["runtime"]
            session = value["session"]
            if (
                not isinstance(runtime, str)
                or re.fullmatch(r"[a-z][a-z0-9-]*", runtime) is None
                or not isinstance(session, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", session) is None
            ):
                raise RequestRejected("invalid turn identity")
            if cmd == "start":
                reply = server.turns.start(runtime, session)
            else:
                reply = server.turns.finish(runtime, session)
        except RequestRejected as exc:
            reply = {"ok": False, "error": str(exc)}
        except (LockError, OSError) as exc:
            server.fail(exc)
            reply = {"ok": False, "error": str(exc)}
        try:
            conn.sendall(json.dumps(reply, separators=(",", ":")).encode("ascii") + b"\n")
        except OSError:
            pass


class TurnDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: str,
        turns: SystemdTurns,
        agent_uid: int,
        group: str,
        fail: Callable[[Exception], None],
    ) -> None:
        self.path = path
        self.turns = turns
        self.agent_uid = agent_uid
        self.fail = fail
        parent = Path(path).parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise LockError("turn socket directory must be private to root")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if info.st_mode & 0o170000 != 0o140000 or info.st_uid != os.geteuid():
                raise LockError("refusing to replace a non-socket or foreign turn socket")
            os.unlink(path)
        super().__init__(path, TurnRequestHandler)
        os.chown(path, -1, grp.getgrnam(group).gr_gid)
        os.chmod(path, 0o660)

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
    parser.add_argument("--ui-user")
    parser.add_argument("--attended-seconds", type=int, default=DEFAULT_ATTENDED_SECONDS)
    parser.add_argument("--boot-id-file", default="/proc/sys/kernel/random/boot_id")
    parser.add_argument(
        "--cgroup-set", type=cgroup_set, action="append",
        help="required FAMILY:TABLE:SET agent identity; repeat for audit and enforcement sets",
    )
    parser.add_argument("--cgroup-root", default="/sys/fs/cgroup")
    parser.add_argument("--agent-user", help="enable cancellation for this user's DSH/Kimi services")
    parser.add_argument("--turn-socket", default="/run/diary-lock/turns.sock")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-lockd must run as root")
    server: LockDaemon | None = None
    turn_server: TurnDaemon | None = None
    turn_thread: threading.Thread | None = None
    controller: Controller | None = None
    try:
        controller = Controller(
            StateStore(args.state, boot_id(args.boot_id_file)),
            SystemdModes(),
            AuditClient(args.audit_socket),
            args.attended_seconds,
            identity=NftCgroupIdentity(
                tuple(args.cgroup_set or DEFAULT_CGROUP_SETS), args.cgroup_root,
            ),
        )
        turns: SystemdTurns | None = None
        if args.agent_user is not None:
            turns = SystemdTurns(args.agent_user, lambda: controller.state == "ATTENDED")
            controller.turns = turns
        controller.startup()
        ui_uid = pwd.getpwnam(args.ui_user).pw_uid if args.ui_user else None
        server = LockDaemon(
            args.listen,
            controller,
            args.group,
            pwd.getpwnam(args.owner).pw_uid,
            args.dashboard_user,
            ui_uid,
        )
        server.timeout = 1.0
        if turns is not None:
            agent = pwd.getpwnam(args.agent_user)
            agent_group = grp.getgrgid(agent.pw_gid).gr_name

            def turn_failed(error: Exception) -> None:
                try:
                    controller.failsafe("turn_request_failed")
                except Exception as failsafe_error:
                    server.latch_failure(failsafe_error)
                else:
                    server.latch_failure(error)

            turn_server = TurnDaemon(args.turn_socket, turns, agent.pw_uid, agent_group, turn_failed)
            turn_thread = threading.Thread(target=turn_server.serve_forever, daemon=True)
            turn_thread.start()
        sd_notify("READY=1")
    except (KeyError, LockError, OSError) as exc:
        if controller is not None:
            try:
                controller.failsafe("startup_failed")
            except (LockError, OSError) as failsafe_error:
                print(f"diary-lockd: {failsafe_error}", file=sys.stderr)
        if turn_server is not None:
            turn_server.server_close()
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
        if turn_server is not None:
            turn_server.shutdown()
            turn_server.server_close()
        if turn_thread is not None:
            turn_thread.join(timeout=5)
        if controller.turns is not None:
            try:
                controller.failsafe("controller_stopped")
            except (LockError, OSError) as failsafe_error:
                print(f"diary-lockd: {failsafe_error}", file=sys.stderr)
        server.server_close()
    return 0


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "diary-lockd":
        raise SystemExit(daemon_main())
    raise SystemExit(main())
