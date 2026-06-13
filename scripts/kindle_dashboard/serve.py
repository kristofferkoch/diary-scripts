"""FastAPI HTTP server for the Kindle dashboard.

Endpoints:
    GET /                    - HTML preview, embeds the PNG (handy on a phone).
    GET /dashboard.png       - the live PNG the Kindle polls.
    GET /control.sh          - signed device control script (X-Control-Sig).
    GET /control/maintenance - "1"/"0": should the device stay awake?
    GET /healthz             - liveness, used by caddy + manual checks.

Binding:
    Listens on 0.0.0.0:8801 so the Kindle (LAN, no tailnet) can reach it.
    Caddy on server reverse-proxies /kindle/* to 127.0.0.1:8801 for
    tailnet access from user's phone.

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
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import render, view

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
    """ECDSA-SHA256 sign `data`, return base64 DER. None on any failure."""
    if not _SIGN_KEY.exists():
        log.error("signing key missing at %s — control.sh served UNSIGNED", _SIGN_KEY)
        return None
    try:
        p = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(_SIGN_KEY)],
            input=data,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        log.exception("openssl sign invocation failed")
        return None
    if p.returncode != 0:
        log.error("openssl sign rc=%s stderr=%r", p.returncode, p.stderr[:200])
        return None
    return base64.b64encode(p.stdout).decode("ascii")


@app.get("/control.sh")
def control_sh() -> Response:
    """Serve the signed device control script (text + X-Control-Sig header)."""
    try:
        body = _CONTROL_PATH.read_bytes()
    except FileNotFoundError:
        return Response(status_code=404, content=b"# control.sh missing\n",
                        media_type="text/plain")
    headers = {"Cache-Control": "no-store"}
    sig = _sign_control(body)
    if sig is not None:
        headers["X-Control-Sig"] = sig
    return Response(content=body, media_type="text/x-shellscript", headers=headers)


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
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/dashboard.png")
async def dashboard_png(
    if_none_match: str | None = Header(default=None, alias="if-none-match"),
    x_batt_cap: str | None = Header(default=None, alias="x-batt-cap"),
    x_batt_status: str | None = Header(default=None, alias="x-batt-status"),
    x_batt_ac: str | None = Header(default=None, alias="x-batt-ac"),
) -> Response:
    """Return the current dashboard PNG with conditional-GET support."""
    _log_battery(x_batt_cap, x_batt_status, x_batt_ac)  # device piggybacks charge state here
    try:
        png, etag = await _render_current()
    except Exception:
        log.exception("render failed — falling back to placeholder")
        png = render.placeholder_png("render error — see journalctl")
        etag = f'"{hashlib.sha256(png).hexdigest()[:16]}"'
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})
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


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8801"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("kindle_dashboard starting on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
