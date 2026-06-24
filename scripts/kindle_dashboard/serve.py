"""FastAPI HTTP server for the Kindle dashboard.

Endpoints:
    GET /                    - HTML preview, embeds the PNG (handy on a phone).
    GET /dashboard.png       - the live PNG the Kindle polls.
    GET /control.sh          - signed device control script (X-Control-Sig);
                               503 if signing is broken (see KINDLE_ALLOW_UNSIGNED).
    GET /control/maintenance - "1"/"0": should the device stay awake?
    GET /healthz             - real health: 503 when render or signing is broken,
                               with diagnostics. Backed by health.py + a JSON
                               state file ($KINDLE_HEALTH_FILE) for monitors.

Binding:
    Listens on [::]:8801 (IPv6 dual-stack — accepts IPv4-mapped too, so the
    Kindle on the LAN reaches it over either family) so the Kindle (LAN, no
    tailnet) can reach it. Caddy on server reverse-proxies /kindle/* to
    127.0.0.1:8801 for tailnet access from user's phone.

The PNG itself contains no secrets — calendar/weather/Spond status that's
already on the family's shared calendar. LAN bind is intentional.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import render, view
from .health import HEALTH

log = logging.getLogger("kindle_dashboard")

app = FastAPI(title="kindle_dashboard", docs_url=None, redoc_url=None)

# --- device control plane ---------------------------------------------------
#
# control.sh is fetched + executed by the wall Kindle on every wake (see
# device/dashboard.sh). We serve the script body with an ECDSA P-256
# SHA-256 signature in the X-Control-Sig header; the device verifies it
# against the public key deployed over SSH before running anything, so the
# curl|exec is authenticated rather than trusted-because-LAN. Edit control.sh
# in the repo and it goes live on the device's next wake.
#
# The private key lives OUTSIDE the repo (it's a credential). Override with
# $KINDLE_SIGN_KEY; default ~/.config/kindle-dashboard/sign-ec.key.
# Maintenance: touch $KINDLE_MAINT_FLAG (default the repo MAINTENANCE file)
# to make the device stay awake on its next wake — a reachable window for
# SSH deploys without racing the ~10 s suspend cycle. rm it to resume normal
# suspend.
_CONTROL_PATH = Path(__file__).parent / "control.sh"
_SIGN_KEY = Path(
    os.environ.get("KINDLE_SIGN_KEY", Path.home() / ".config/kindle-dashboard/sign-ec.key")
)
# Let the health snapshot report whether the signing key is actually on disk.
HEALTH.signing_key = _SIGN_KEY
_MAINT_FLAG = Path(os.environ.get("KINDLE_MAINT_FLAG", Path(__file__).parent / "MAINTENANCE"))

# Battery telemetry: the wall Kindle sends its charge state as X-Batt-* headers
# on each /dashboard.png fetch; we append a line here. This log lives on
# server (real disk, no trim) so "when was it unplugged?" stays
# answerable indefinitely — unlike the device's own /var/log, which is tmpfs
# and overwrites a charger-disconnect event within ~30 min. Override with
# $KINDLE_BATT_LOG. ~40 B/line, ~96 lines/day at off-peak cadence ⇒ negligible.
_BATT_LOG = Path(
    os.environ.get("KINDLE_BATT_LOG", Path.home() / ".local/state/kindle-dashboard/battery.log")
)


def _log_battery(cap: str | None, status: str | None, ac: str | None) -> None:
    """Append one battery telemetry line (server-stamped local time). Never raises."""
    if cap is None and status is None and ac is None:
        return  # not a device fetch (e.g. phone preview) — nothing to log
    try:
        _BATT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with _BATT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{cap or '?'}%\t{status or '?'}\tac={ac or '?'}\n")
    except Exception:
        log.exception("battery telemetry write failed")


def _sign_control(data: bytes) -> str | None:
    """ECDSA-SHA256 sign `data`, return base64 DER. None on any failure.

    Records the outcome in HEALTH so a missing/broken key surfaces in /healthz
    and the state file instead of being a lone journal line.
    """
    if not _SIGN_KEY.exists():
        log.error("signing key missing at %s — cannot sign control.sh", _SIGN_KEY)
        HEALTH.sign_failed(f"signing key missing at {_SIGN_KEY}")
        return None
    try:
        p = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(_SIGN_KEY)],
            input=data,
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        log.exception("openssl sign invocation failed")
        HEALTH.sign_failed(f"openssl invocation failed: {exc!r}")
        return None
    if p.returncode != 0:
        log.error("openssl sign rc=%s stderr=%r", p.returncode, p.stderr[:200])
        HEALTH.sign_failed(f"openssl rc={p.returncode}")
        return None
    HEALTH.sign_ok()
    return base64.b64encode(p.stdout).decode("ascii")


# Escape hatch: serving control.sh unsigned is a HARD fault by default (503) so
# the failure is visible server-side, not a silent 200 the device quietly
# rejects. Set KINDLE_ALLOW_UNSIGNED=1 only for deliberate key-rotation windows.
def _allow_unsigned() -> bool:
    return os.environ.get("KINDLE_ALLOW_UNSIGNED", "") not in ("", "0", "false")


@app.get("/control.sh")
def control_sh() -> Response:
    """Serve the device control script with an ECDSA signature header.

    If signing fails (missing/broken key) we return **503**, not a 200 with an
    unsigned body: the device rejects unsigned anyway, so a 200 only hid the
    fault from the server's own logs/health. `X-Control-Unsigned: 1` flags it
    explicitly for any client that sees it.
    """
    try:
        body = _CONTROL_PATH.read_bytes()
    except FileNotFoundError:
        return Response(status_code=404, content=b"# control.sh missing\n",
                        media_type="text/plain")
    sig = _sign_control(body)
    if sig is not None:
        return Response(
            content=body,
            media_type="text/x-shellscript",
            headers={"Cache-Control": "no-store", "X-Control-Sig": sig},
        )
    # Unsigned: fault loudly unless an operator explicitly opted in.
    if not _allow_unsigned():
        return Response(
            status_code=503,
            content=b"# control.sh unsigned (signing key missing/broken) - refusing to serve\n",
            media_type="text/plain",
            headers={"Cache-Control": "no-store", "X-Control-Unsigned": "1"},
        )
    return Response(
        content=body,
        media_type="text/x-shellscript",
        headers={"Cache-Control": "no-store", "X-Control-Unsigned": "1"},
    )


@app.get("/control/maintenance")
def control_maintenance() -> Response:
    """1 if a maintenance window is requested (device should stay awake), else 0."""
    val = b"1\n" if _MAINT_FLAG.exists() else b"0\n"
    return Response(content=val, media_type="text/plain",
                    headers={"Cache-Control": "no-store"})

_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "j2", "html.j2"]),
)


def _strike(text: str) -> Markup:
    """Render `~~struck~~` spans as `<del>` for the wall display.

    Escapes everything first, then promotes the `~~…~~` markers to real
    strikethrough — so an inline correction (`Pinsefri ~~mandag~~ tirsdag`)
    shows the old value crossed out rather than as plain text or raw tildes.
    The captured span is already escaped, so the returned Markup is safe.

    >>> str(_strike("Pinsefri ~~mandag~~ tirsdag"))
    'Pinsefri <del>mandag</del> tirsdag'
    >>> str(_strike("Lek & moro"))
    'Lek &amp; moro'
    """
    escaped = str(escape(text))
    return Markup(re.sub(r"~~(.+?)~~", r"<del>\1</del>", escaped))


_TEMPLATES.filters["strike"] = _strike

# --- material-hash cache ----------------------------------------------------
#
# The wall display only wants to *refresh* the e-ink panel when something
# actually changed. We compute a hash over the part of the rendered context
# that represents content (calendar, weather, spond, …) and skip the
# Chromium roundtrip when it matches the previous render. Crucially, the
# cached PNG keeps the OLD "Sist oppdatert HH:MM" baked in, so the wall
# shows the time the content last changed — which doubles as a debugging
# signal ("the picture froze at 14:32; that's when the last update landed").
#
# Fields in this set are explicitly excluded from the hash because they are
# display of when we rendered, not what we rendered. Stamping them through
# would force a refresh per minute and defeat the whole optimisation.
_MATERIAL_EXCLUDE = {"rendered_at", "build_tag"}

_cache_lock = asyncio.Lock()
_render_cache: dict[str, Any] = {
    "material_hash": None,
    "png": None,
    "etag": None,
}


def _material_hash(ctx: dict[str, Any]) -> str:
    material = {k: v for k, v in ctx.items() if k not in _MATERIAL_EXCLUDE}
    blob = json.dumps(material, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Real liveness: exercises actual state, returns 503 when degraded.

    Reports render + signing health with diagnostics (last-ok/last-fail,
    consecutive failures, last error). A monitor or Caddy probe that watches
    this now goes red when renders fail or control.sh can't be signed — the two
    failures that previously hid behind 200/placeholder/hardcoded-ok.
    """
    snap = HEALTH.snapshot()
    code = 200 if snap["status"] == "ok" else 503
    return JSONResponse(snap, status_code=code)


@app.get("/dashboard.png")
async def dashboard_png(
    request: Request,
    if_none_match: str | None = Header(default=None, alias="if-none-match"),
    x_batt_cap: str | None = Header(default=None, alias="x-batt-cap"),
    x_batt_status: str | None = Header(default=None, alias="x-batt-status"),
    x_batt_ac: str | None = Header(default=None, alias="x-batt-ac"),
) -> Response:
    """Return the current dashboard PNG with conditional-GET support."""
    client = request.client.host if request.client else "?"
    _log_battery(x_batt_cap, x_batt_status, x_batt_ac)  # device piggybacks charge state here
    try:
        png, etag = await _render_current()
        HEALTH.render_ok()
    except Exception as exc:
        log.exception("render failed — falling back to placeholder")
        HEALTH.render_failed(repr(exc))
        # Bake the staleness onto the wall display itself: it now *says* it's
        # stuck and since when, instead of silently showing old content.
        png = render.placeholder_png(HEALTH.stale_message())
        etag = f'"{hashlib.sha256(png).hexdigest()[:16]}"'
    if if_none_match and if_none_match.strip() == etag:
        # Explicit fetch log: access_log is off, so without this a device poll
        # left no trace — exactly the blind spot that hid the 2026-06-24 outage.
        log.info("dashboard.png 304 client=%s batt=%s", client, x_batt_cap or "?")
        return Response(status_code=304, headers={"ETag": etag})
    log.info("dashboard.png 200 client=%s batt=%s etag=%s", client, x_batt_cap or "?", etag)
    return Response(
        content=png,
        media_type="image/png",
        headers={"ETag": etag, "Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
def preview() -> str:
    return """<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kindle dashboard</title>
  <style>
    html, body { margin: 0; background: #222; color: #ddd;
                 font-family: system-ui, sans-serif; }
    main { max-width: 540px; margin: 1rem auto; padding: 0 .5rem; }
    img { display: block; width: 100%; height: auto;
          background: #fff; border-radius: 6px; }
    p { font-size: .9rem; opacity: .7; }
    a { color: #8cf; }
  </style>
</head>
<body>
  <main>
    <img src="dashboard.png" alt="Kindle dashboard">
    <p>Live mirror of the wall Kindle. Refresh to update.
       Source: <a href="https://github.com/exampleuser/diary/tree/master/scripts/kindle_dashboard">scripts/kindle_dashboard</a>.</p>
  </main>
</body>
</html>"""


def _strict_flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0", "false")


def _sd_notify(state: str) -> None:
    """Best-effort sd_notify to systemd (Type=notify). No-op outside systemd.

    Lets us hold off `READY=1` until the startup self-check has actually passed
    and we're serving — so `systemctl start` blocks until the service is *truly*
    up, and "active (running)" stops being a lie about a half-broken process.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            # Abstract-namespace sockets start with '@' on the wire -> NUL byte.
            s.connect("\0" + addr[1:] if addr.startswith("@") else addr)
            s.sendall(state.encode())
    except Exception:
        log.exception("sd_notify failed")


@app.on_event("startup")
async def _startup_selfcheck() -> None:
    """Exercise both hard dependencies at boot so restore gaps surface early.

    A `Type=simple` unit reports "active (running)" the moment the process is
    up — it can't tell that Chromium is missing or the signing key vanished.
    This check renders once (real end-to-end Playwright roundtrip, which also
    warms the browser + cache) and test-signs a probe, recording both in
    HEALTH. Failures are logged at ERROR and exposed via /healthz.

    Opt-in strictness turns a silent degradation into a *visible* systemd
    failure: KINDLE_REQUIRE_SIGNING=1 and/or KINDLE_REQUIRE_RENDER=1 raise here,
    which makes uvicorn exit non-zero so `Restart=on-failure` crash-loops the
    unit (red in `systemctl status`) instead of serving broken output.
    """
    broken: list[str] = []

    # Signing: does the key exist and actually produce a signature?
    if _sign_control(b"healthcheck") is None:
        broken.append("signing")
        log.error("startup self-check: control.sh signing is BROKEN")
    else:
        log.info("startup self-check: signing OK")

    # Render: full HTML→PNG via Chromium (warms the browser + render cache).
    try:
        await _render_current()
        HEALTH.render_ok()
        log.info("startup self-check: render OK")
    except Exception:
        HEALTH.render_failed("startup self-check render failed")
        broken.append("render")
        log.exception("startup self-check: render is BROKEN")

    required = [
        dep
        for dep, flag in (("signing", "KINDLE_REQUIRE_SIGNING"), ("render", "KINDLE_REQUIRE_RENDER"))
        if dep in broken and _strict_flag(flag)
    ]
    if required:
        # Never signals READY -> with Type=notify systemd fails the start
        # instead of reporting a broken process as "active (running)".
        raise RuntimeError(
            f"startup self-check failed for required dependencies: {', '.join(required)} "
            "(set by KINDLE_REQUIRE_*). Refusing to start with a broken dependency."
        )

    # Self-check passed and the listen socket is already bound -> tell systemd
    # we're genuinely ready (Type=notify). Degraded-but-allowed startups (deps
    # broken without KINDLE_REQUIRE_*) still report ready, since /healthz is the
    # right surface for ongoing degradation; the unit only hard-fails on a
    # *required* dep.
    status = "ok" if not broken else f"degraded (broken: {', '.join(broken)})"
    _sd_notify(f"READY=1\nSTATUS=self-check {status}; serving")


@app.on_event("startup")
async def _start_watcher() -> None:
    """Kick off the precipitation watcher in the background."""
    from .watcher import precipitation_watcher

    asyncio.create_task(precipitation_watcher())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await render.shutdown_browser()


async def _render_current() -> tuple[bytes, str]:
    """Build context, hash the material parts, reuse cache when unchanged."""
    ctx = view.build_context()
    h = _material_hash(ctx)

    async with _cache_lock:
        if _render_cache["material_hash"] == h and _render_cache["png"] is not None:
            return _render_cache["png"], _render_cache["etag"]

        html = _TEMPLATES.get_template("dashboard.html.j2").render(**ctx)
        png = await render.html_to_png(html)
        etag = f'"{hashlib.sha256(png).hexdigest()[:16]}"'
        _render_cache.update(material_hash=h, png=png, etag=etag)
        log.info(
            "re-rendered (material changed) etag=%s rendered_at=%s",
            etag,
            ctx.get("rendered_at"),
        )
        return png, etag


def _make_listen_socket(host: str, port: int) -> socket.socket:
    """Bind a listen socket, forcing IPv6 dual-stack for `::`.

    uvicorn's own `::` bind comes up IPv6-ONLY on this host (it doesn't clear
    IPV6_V6ONLY), which would silently cut off the IPv4 LAN Kindle. Creating the
    socket ourselves and clearing V6ONLY guarantees one listener serves both
    families (IPv4 arrives v4-mapped). Verified: a raw `::` socket here answers
    127.0.0.1 and ::1 alike.
    """
    family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # No try/except: if we can't clear V6ONLY the IPv4 Kindle is cut off, so
        # crash loudly (full stacktrace, unit fails) rather than serve half-up.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


def main() -> None:
    import uvicorn

    # IPv6 dual-stack by default: one `::` listener serves both the IPv4 LAN
    # Kindle and tailnet/IPv6 clients. See _make_listen_socket for why we build
    # the socket ourselves instead of letting uvicorn bind `::`.
    host = os.environ.get("HOST", "::")
    port = int(os.environ.get("PORT", "8801"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("kindle_dashboard starting on %s:%s", host, port)

    # Type=notify: the startup self-check sends READY=1 once it's genuinely up
    # (see _startup_selfcheck), so systemd doesn't mark us ready prematurely.
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="info"))
    server.run(sockets=[_make_listen_socket(host, port)])


if __name__ == "__main__":
    main()
