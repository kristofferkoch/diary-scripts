"""FastAPI app: inbox + message detail + tankekart fragment.

Run directly (`uv run -m mail_reader.server`) or via the systemd unit.
Behind Caddy at `/mail/` — `root_path=/mail` so url_for() generates
absolute paths the browser can follow.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, inbox as inbox_mod, message as message_mod, related as related_mod
from . import summarize as summarize_mod

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

# `root_path` is set on uvicorn (see main()), not the FastAPI constructor.
# With Caddy's `handle_path /mail/*` stripping the prefix, uvicorn-level
# root_path is the "proxy already stripped, here's the prefix only for URL
# generation" mode. Setting it on FastAPI() instead made routing require the
# /mail prefix on the inbound path, so static assets 404'd through Caddy.
app = FastAPI()

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def get_inbox(request: Request, limit: int = 50):
    threads = inbox_mod.list_inbox(limit=limit)
    return TEMPLATES.TemplateResponse(
        request, "inbox.html", {"threads": threads},
    )


@app.get("/t/{thread_id}", response_class=HTMLResponse)
def get_thread(request: Request, thread_id: str):
    msg_id = inbox_mod.latest_message_id_in_thread(thread_id)
    if msg_id is None:
        raise HTTPException(404, "thread not found")
    return _render_message(request, msg_id, thread_id)


@app.get("/m/{message_id:path}", response_class=HTMLResponse)
def get_message(request: Request, message_id: str):
    return _render_message(request, urllib.parse.unquote(message_id), thread_id=None)


def _render_message(request: Request, msg_id: str, thread_id: str | None):
    msg = message_mod.fetch_message(msg_id)
    return TEMPLATES.TemplateResponse(
        request, "message.html",
        {
            "msg": msg,
            "msg_id_quoted": urllib.parse.quote(msg_id, safe=""),
            "thread_id": thread_id,
        },
    )


@app.get("/api/tankekart/{message_id:path}", response_class=HTMLResponse)
def get_tankekart(request: Request, message_id: str):
    msg_id = urllib.parse.unquote(message_id)
    with db.connect() as conn:
        related = related_mod.tankekart(conn, msg_id, k=10)
        for r in related:
            r["summary"] = summarize_mod.get_or_create_summary(conn, r["message_id"])
    return TEMPLATES.TemplateResponse(
        request, "_tankekart.html", {"related": related},
    )


def main() -> None:
    import uvicorn
    uvicorn.run(
        "mail_reader.server:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8800")),
        root_path=os.environ.get("ROOT_PATH", "/mail"),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
