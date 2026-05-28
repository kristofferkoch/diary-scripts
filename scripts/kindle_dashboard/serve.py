"""FastAPI HTTP server for the Kindle dashboard.

Endpoints:
    GET /              - HTML preview, embeds the PNG (handy on a phone).
    GET /dashboard.png - the live PNG the Kindle polls.
    GET /healthz       - liveness, used by caddy + manual checks.

Binding:
    Listens on 0.0.0.0:8801 so the Kindle (LAN, no tailnet) can reach it.
    Caddy on server reverse-proxies /kindle/* to 127.0.0.1:8801 for
    tailnet access from user's phone.

The PNG itself contains no secrets — calendar/weather/Spond status that's
already on the family's shared calendar. LAN bind is intentional.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import render, view

log = logging.getLogger("kindle_dashboard")

app = FastAPI(title="kindle_dashboard", docs_url=None, redoc_url=None)

_TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html", "j2", "html.j2"]),
)

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
) -> Response:
    """Return the current dashboard PNG with conditional-GET support."""
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
