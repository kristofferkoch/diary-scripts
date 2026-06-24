"""Runtime health tracking for the Kindle dashboard service.

The service is a long-running uvicorn process whose two hard dependencies — a
working Playwright/Chromium and the control-script signing key — can vanish
(e.g. a backup restore that drops ``~/.cache`` or misses a ``~/.config``
subdir) *without* the process dying. Before this module every such failure was
masked: render exceptions fell back to a 200 placeholder, a missing signing key
served a 200 *unsigned* script, and ``/healthz`` was a hardcoded ``"ok"``. A
monitor (or systemd) saw green while the wall display silently froze for days
(this actually happened 2026-06-22 → 06-24; see backup/RESTORE-2026-06-22.md).

This module is the single source of truth for "is the service actually
working", surfaced three ways from one state object:
    - ``/healthz`` returns 503 when render or signing is broken,
    - the startup self-check (optionally) fails the unit, and
    - a JSON state file (``$KINDLE_HEALTH_FILE``) any external monitor can poll.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("kindle_dashboard")

# Machine-readable health snapshot, rewritten on every render/sign event. A
# monitor (heartbeat, cron, Caddy probe) polls this instead of inferring health
# from "is the process alive". Override with $KINDLE_HEALTH_FILE.
_HEALTH_FILE = Path(
    os.environ.get(
        "KINDLE_HEALTH_FILE",
        Path.home() / ".local/state/kindle-dashboard/health.json",
    )
)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(ts: _dt.datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


class Health:
    """Thread-safe health state. One module-level instance (`HEALTH`) below.

    `signing_key` is set by the server once the key path is resolved so the
    snapshot can report whether the key is actually present on disk.
    """

    def __init__(self, signing_key: Path | None = None) -> None:
        self._lock = threading.Lock()
        self.signing_key = signing_key
        self.last_render_ok: _dt.datetime | None = None
        self.last_render_fail: _dt.datetime | None = None
        self.render_failing_since: _dt.datetime | None = None
        self.consecutive_render_failures = 0
        self.last_render_err: str | None = None
        self.last_sign_ok: _dt.datetime | None = None
        self.last_sign_fail: _dt.datetime | None = None
        self.last_sign_err: str | None = None

    # -- mutators (each persists the snapshot) -----------------------------

    def render_ok(self) -> None:
        with self._lock:
            self.last_render_ok = _now()
            self.render_failing_since = None
            self.consecutive_render_failures = 0
            self.last_render_err = None
        self._persist()

    def render_failed(self, err: str) -> None:
        with self._lock:
            now = _now()
            self.last_render_fail = now
            if self.render_failing_since is None:
                self.render_failing_since = now
            self.consecutive_render_failures += 1
            self.last_render_err = err
        self._persist()

    def sign_ok(self) -> None:
        with self._lock:
            self.last_sign_ok = _now()
            self.last_sign_err = None
        self._persist()

    def sign_failed(self, err: str) -> None:
        with self._lock:
            self.last_sign_fail = _now()
            self.last_sign_err = err
        self._persist()

    # -- views -------------------------------------------------------------

    def _key_present(self) -> bool:
        return bool(self.signing_key and self.signing_key.exists())

    def stale_message(self) -> str:
        """Human string baked into the placeholder PNG while render is failing.

        Drawn on the wall display itself so a stuck dashboard *says* it's stuck
        (and since when), rather than silently showing old content.
        """
        with self._lock:
            since = _iso(self.render_failing_since) or "?"
            n = self.consecutive_render_failures
        return f"STALE - render feiler siden {since} ({n} forsok). Se journalctl."

    def snapshot(self) -> dict[str, Any]:
        """Full health verdict + diagnostics. Drives /healthz and the state file."""
        with self._lock:
            key_present = self._key_present()
            render_healthy = (
                self.consecutive_render_failures == 0 and self.last_render_ok is not None
            )
            signing_healthy = key_present and self.last_sign_err is None
            return {
                "status": "ok" if (render_healthy and signing_healthy) else "degraded",
                "render": {
                    "healthy": render_healthy,
                    "last_ok": _iso(self.last_render_ok),
                    "last_fail": _iso(self.last_render_fail),
                    "failing_since": _iso(self.render_failing_since),
                    "consecutive_failures": self.consecutive_render_failures,
                    "last_error": self.last_render_err,
                },
                "signing": {
                    "healthy": signing_healthy,
                    "key_present": key_present,
                    "last_ok": _iso(self.last_sign_ok),
                    "last_fail": _iso(self.last_sign_fail),
                    "last_error": self.last_sign_err,
                },
                "checked_at": _iso(_now()),
            }

    def _persist(self) -> None:
        """Atomically write the snapshot to the state file. Never raises."""
        try:
            snap = self.snapshot()
            _HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _HEALTH_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
            tmp.replace(_HEALTH_FILE)
        except Exception:
            log.exception("health state-file write failed")


# Single shared instance. The server sets `.signing_key` once it resolves the
# key path (see serve.py).
HEALTH = Health()
