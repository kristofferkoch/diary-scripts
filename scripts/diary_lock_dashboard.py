#!/usr/bin/python3
"""Unprivileged read-only web dashboard for controller state and audit reports."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import pwd
import re
import socket
import socketserver
import struct
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
DEFAULT_SOCKET = "/run/diary-lock-dashboard/http.sock"
DEFAULT_CONTROLLER_SOCKET = "/run/diary-lock/socket"
DEFAULT_AUDIT_SOCKET = "/run/diary-audit/socket"
DEFAULT_CADDY_USER = "caddy"
DEFAULT_OWNER = "kek"
DEFAULT_OWNER_GROUP = "lock-owner"
ENFORCEMENT_LABEL = "Enforcement: dark / audit-only"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REPORT_LIMIT = 10_000
STATES = frozenset({"LOCKING", "LOCKED", "ATTENDED"})
GROUP_KINDS = ("network", "dns", "broker", "other")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
GROUP_FIELDS = (
    "event", "component", "operation", "decision", "lock_state",
    "destination_ip", "destination_port", "protocol", "dns_name", "caller_uid",
)


class DashboardError(ValueError):
    """A trusted status source could not provide a valid read-only response."""


def request(socket_path: str, value: dict[str, Any], timeout: float = 40.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_BYTES:
                raise DashboardError("trusted response is too large")
        reply = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardError("trusted status source is unavailable") from exc
    finally:
        sock.close()
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise DashboardError("trusted status source rejected the read-only request")
    return cast(dict[str, Any], reply)


def _number(value: object, field: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DashboardError(f"invalid {field}")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DashboardError(f"invalid {field}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise DashboardError(f"invalid {field}")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DashboardError(f"invalid {field}") from exc
    return text


def _controller(reply: dict[str, Any]) -> dict[str, Any]:
    state = reply.get("state")
    if state not in STATES:
        raise DashboardError("invalid controller state")
    expires_at = _number(reply.get("expires_at"), "attendance expiry")
    remaining = _number(reply.get("remaining_seconds"), "attendance remainder")
    if (state == "ATTENDED") != (expires_at is not None):
        raise DashboardError("inconsistent attendance expiry")
    if state == "ATTENDED" and (remaining is None or remaining < 0):
        raise DashboardError("invalid attendance remainder")
    if state != "ATTENDED" and remaining is not None:
        raise DashboardError("unexpected attendance remainder")
    return {
        "available": True,
        "state": state,
        "expires_at": expires_at,
        "remaining_seconds": remaining,
        "boot_id": _text(reply.get("boot_id"), "boot ID"),
    }


def _group(entry: object) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise DashboardError("invalid audit group")
    entry = cast(dict[str, Any], entry)
    fingerprint = entry.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
        raise DashboardError("invalid audit fingerprint")
    clean: dict[str, Any] = {
        "fingerprint": fingerprint,
        "count": _count(entry.get("count"), "group count"),
        "records": _count(entry.get("records"), "group records"),
        "first_seen": _timestamp(entry.get("first_seen"), "group first_seen"),
        "last_seen": _timestamp(entry.get("last_seen"), "group last_seen"),
    }
    for field in GROUP_FIELDS:
        if field not in entry:
            continue
        value = entry[field]
        if field in {"destination_port", "caller_uid"}:
            clean[field] = _count(value, field)
        else:
            clean[field] = _text(value, field)
    if "repeated" in entry:
        if not isinstance(entry["repeated"], bool):
            raise DashboardError("invalid repeated marker")
        clean["repeated"] = entry["repeated"]
    return clean


def _kind(group: dict[str, Any]) -> str:
    event = group.get("event", "")
    component = group.get("component", "")
    if "dns_name" in group or event.startswith("dns."):
        return "dns"
    if event.startswith("broker.") or "broker" in component:
        return "broker"
    if any(field in group for field in ("destination_ip", "destination_port", "protocol")):
        return "network"
    if event.startswith(("network.", "firewall.")):
        return "network"
    return "other"


def _report(reply: dict[str, Any]) -> dict[str, Any]:
    if reply.get("schema_version") != SCHEMA_VERSION or reply.get("report") != "baseline":
        raise DashboardError("unsupported audit report")
    groups = reply.get("groups")
    if not isinstance(groups, list) or len(groups) > REPORT_LIMIT:
        raise DashboardError("invalid audit groups")
    categorized: dict[str, list[dict[str, Any]]] = {kind: [] for kind in GROUP_KINDS}
    for entry in groups:
        group = _group(entry)
        categorized[_kind(group)].append(group)
    if not isinstance(reply.get("truncated"), bool):
        raise DashboardError("invalid truncation marker")
    return {
        "available": True,
        "since": _timestamp(reply.get("since"), "report since"),
        "until": _timestamp(reply.get("until"), "report until"),
        "generated_at": _timestamp(reply.get("generated_at"), "report generated_at"),
        "event_count": _count(reply.get("event_count"), "event count"),
        "record_count": _count(reply.get("record_count"), "record count"),
        "duplicate_record_count": _count(reply.get("duplicate_record_count"), "duplicate count"),
        "truncated": reply["truncated"],
        "groups": categorized,
    }


class SnapshotReader:
    def __init__(
        self,
        controller_socket: str = DEFAULT_CONTROLLER_SOCKET,
        audit_socket: str = DEFAULT_AUDIT_SOCKET,
        reader: Callable[[str, dict[str, Any]], dict[str, Any]] = request,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.controller_socket = controller_socket
        self.audit_socket = audit_socket
        self.reader = reader
        self.clock = clock

    def snapshot(self) -> dict[str, Any]:
        try:
            controller = _controller(self.reader(self.controller_socket, {"cmd": "status"}))
        except (DashboardError, KeyError, TypeError, ValueError):
            controller = {
                "available": False,
                "state": None,
                "expires_at": None,
                "remaining_seconds": None,
                "boot_id": None,
            }
        try:
            report = _report(self.reader(self.audit_socket, {
                "cmd": "baseline",
                "since": "24h",
                "until": "now",
                "limit": REPORT_LIMIT,
            }))
        except (DashboardError, KeyError, TypeError, ValueError):
            report = {
                "available": False,
                "since": None,
                "until": None,
                "generated_at": None,
                "event_count": None,
                "record_count": None,
                "duplicate_record_count": None,
                "truncated": None,
                "groups": {kind: [] for kind in GROUP_KINDS},
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.clock().isoformat(),
            "enforcement": {"mode": "dark", "label": ENFORCEMENT_LABEL, "active": False},
            "controller": controller,
            "components": [
                {"name": "Controller status", "available": controller["available"]},
                {"name": "Audit report", "available": report["available"]},
            ],
            "report": report,
        }


def authorized(headers: Any, owner: str, owner_group: str) -> bool:
    users = headers.get_all("Remote-User", [])
    groups = headers.get_all("Remote-Groups", [])
    if len(users) != 1 or len(groups) != 1 or users[0] != owner:
        return False
    return owner_group in {item for item in re.split(r"[\s,]+", groups[0]) if item}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _expiry(controller: dict[str, Any]) -> str:
    if not controller["available"]:
        return "Unavailable"
    if controller["state"] != "ATTENDED":
        return "Not applicable"
    timestamp = dt.datetime.fromtimestamp(controller["expires_at"], dt.UTC).isoformat()
    remaining = controller["remaining_seconds"]
    return f"{_escape(timestamp)} ({_escape(remaining)} seconds remaining)"


def _group_html(group: dict[str, Any]) -> str:
    labels = {
        "event": "Event",
        "component": "Component",
        "operation": "Operation",
        "decision": "Decision",
        "lock_state": "Recorded lock state",
        "destination_ip": "Destination IP",
        "destination_port": "Destination port",
        "protocol": "Protocol",
        "dns_name": "DNS name",
        "caller_uid": "Caller UID",
    }
    details = "".join(
        f"<dt>{_escape(labels[field])}</dt><dd>{_escape(group[field])}</dd>"
        for field in GROUP_FIELDS if field in group
    )
    return (
        "<article class=\"finding\">"
        f"<h4>{_escape(group.get('event', 'Audit finding'))}: "
        f"{_escape(group['count'])} occurrence(s)</h4>"
        f"<p>{_escape(group['records'])} stored record(s); "
        f"first {_escape(group['first_seen'])}; last {_escape(group['last_seen'])}</p>"
        f"<dl>{details}</dl><p class=\"fingerprint\">Fingerprint "
        f"{_escape(group['fingerprint'])}</p></article>"
    )


def render(snapshot: dict[str, Any]) -> str:
    controller = snapshot["controller"]
    report = snapshot["report"]
    state = controller["state"] if controller["available"] else "UNAVAILABLE"
    state_class = str(state).lower()
    components = "".join(
        f"<tr><th scope=\"row\">{_escape(component['name'])}</th>"
        f"<td class=\"{'ok' if component['available'] else 'bad'}\">"
        f"{'Available' if component['available'] else 'Unavailable'}</td></tr>"
        for component in snapshot["components"]
    )
    sections = []
    for kind in GROUP_KINDS:
        title = {"network": "Network", "dns": "DNS", "broker": "Broker", "other": "Other"}[kind]
        if not report["available"]:
            content = "<p class=\"bad\">Findings unavailable: the trusted audit report could not be read.</p>"
        elif not report["groups"][kind]:
            content = "<p>No findings in the returned records."
            if report["truncated"]:
                content += " The report is truncated, so omitted records may contain findings."
            content += "</p>"
        else:
            content = "".join(_group_html(group) for group in report["groups"][kind])
        sections.append(f"<section><h3>{title} findings</h3>{content}</section>")
    if not report["available"]:
        report_status = "<p class=\"bad\">Audit report unavailable. Empty findings are not being inferred.</p>"
    else:
        report_status = (
            f"<p>Window {_escape(report['since'])} to {_escape(report['until'])}; "
            f"{_escape(report['event_count'])} events in {_escape(report['record_count'])} returned records.</p>"
        )
        if report["truncated"]:
            report_status += (
                "<p class=\"truncation\"><strong>Report truncated:</strong> "
                "Only returned records are shown; omitted records may contain additional findings.</p>"
            )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Lockdown status</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui,sans-serif; }}
body {{ max-width: 1100px; margin: 0 auto; padding: 1rem; line-height: 1.45; }}
.banner {{ padding: 1rem; border: 3px solid #b45309; background: #fef3c7; color: #451a03; }}
.banner strong {{ display:block; font-size:1.5rem; }}
.truncation {{ padding:1rem; border:3px solid #b91c1c; font-size:1.15rem; }}
.state {{ font-size: clamp(2.5rem,8vw,5rem); margin:.4rem 0; font-weight:800; }}
.state.unavailable,.bad {{ color:#b91c1c; }} .ok {{ color:#15803d; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem; }}
section,.panel {{ border:1px solid #888; border-radius:.4rem; padding:1rem; margin:1rem 0; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ padding:.5rem; border-bottom:1px solid #888; text-align:left; }}
.finding {{ border-top:1px solid #888; padding:.5rem 0; overflow-wrap:anywhere; }}
dl {{ display:grid; grid-template-columns:minmax(8rem,max-content) 1fr; gap:.2rem .8rem; }}
dt {{ font-weight:650; }} dd {{ margin:0; }} .fingerprint {{ font-family:monospace; font-size:.8rem; }}
a {{ color:inherit; }}
</style></head><body>
<header><h1>Lockdown status</h1>
<div class="banner"><strong>{ENFORCEMENT_LABEL}</strong>
Controller state is not active general-agent containment. Network, DNS, broker, and turn
enforcement remain dark until ACT-01. This dashboard is read-only.</div></header>
<main><section><h2>Authoritative controller state</h2>
<div class="state {state_class}">{_escape(state)}</div>
<p><strong>Attendance expiry:</strong> {_expiry(controller)}</p>
<p><strong>Boot ID:</strong> {_escape(controller['boot_id'] or 'Unavailable')}</p></section>
<section><h2>Component health</h2><table><tbody>{components}</tbody></table></section>
<section><h2>Audit findings</h2>{report_status}{''.join(sections)}</section>
<p><a href="/lock/">Refresh read-only snapshot</a> (automatic every 30 seconds).</p>
</main></body></html>"""


class DashboardServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path: str,
        reader: SnapshotReader,
        caddy_uid: int,
        socket_gid: int,
        owner: str = DEFAULT_OWNER,
        owner_group: str = DEFAULT_OWNER_GROUP,
    ) -> None:
        self.path = path
        self.reader = reader
        self.caddy_uid = caddy_uid
        self.owner = owner
        self.owner_group = owner_group
        parent = Path(path).parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise DashboardError("dashboard socket directory must be private to the service and Caddy")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if info.st_mode & 0o170000 != 0o140000 or info.st_uid != os.geteuid():
                raise DashboardError("refusing to replace a non-socket or foreign socket")
            os.unlink(path)
        super().__init__(path, DashboardHandler)
        os.chown(path, -1, socket_gid)
        os.chmod(path, 0o660)

    def server_close(self) -> None:
        super().server_close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        raw = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, self.peer_uid, _gid = struct.unpack("3i", raw)

    def address_string(self) -> str:
        return "unix-peer"

    def _send(self, status: int, content_type: str, payload: bytes, head: bool = False) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def _deny(self) -> None:
        self._send(403, "text/plain; charset=utf-8", b"Owner two-factor session required\n")

    def _read(self, head: bool) -> None:
        if self.peer_uid != self.server.caddy_uid or not authorized(
            self.headers, self.server.owner, self.server.owner_group,
        ):
            self._deny()
            return
        path = urlsplit(self.path)
        if path.query or path.fragment or path.path not in {"/lock/", "/lock/api/status"}:
            self._send(404, "text/plain; charset=utf-8", b"Not found\n", head)
            return
        snapshot = self.server.reader.snapshot()
        if path.path == "/lock/api/status":
            payload = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload, head)
        else:
            self._send(200, "text/html; charset=utf-8", render(snapshot).encode("utf-8"), head)

    def do_GET(self) -> None:
        self._read(False)

    def do_HEAD(self) -> None:
        self._read(True)

    def _method_not_allowed(self) -> None:
        if self.peer_uid != self.server.caddy_uid:
            self._deny()
            return
        self._send(405, "text/plain; charset=utf-8", b"Read-only dashboard: GET or HEAD required\n")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--caddy-user", default=DEFAULT_CADDY_USER)
    parser.add_argument("--controller-socket", default=DEFAULT_CONTROLLER_SOCKET)
    parser.add_argument("--audit-socket", default=DEFAULT_AUDIT_SOCKET)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--owner-group", default=DEFAULT_OWNER_GROUP)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("diary-lock-dashboard must run as an unprivileged service identity")
    try:
        caddy = pwd.getpwnam(args.caddy_user)
    except KeyError:
        parser.error(f"unknown Caddy identity: {args.caddy_user}")
    server = DashboardServer(
        args.socket,
        SnapshotReader(args.controller_socket, args.audit_socket),
        caddy.pw_uid,
        caddy.pw_gid,
        args.owner,
        args.owner_group,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
