"""FastAPI app: inbox + message detail + tankekart fragment.

Run directly (`uv run -m mail_reader.server`) or via the systemd unit.
Behind Caddy at `/mail/` — root_path is set on uvicorn so url_for()
generates `/mail/...` while the proxy-stripped inbound path matches
routes registered at `/`.

Summary *generation* is retired (2026-08-02): no workers, no enqueueing.
Already-stored `done` summaries still render; mails without one simply
show no summary card.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agenda as agenda_mod
from .auth import MailAuthMiddleware
from . import calendar_md as calendar_mod
from . import db, inbox as inbox_mod, message as message_mod, related as related_mod
from . import entities as entities_mod
from . import summarize as summarize_mod
from .config import workspace_root
from .date_format import relative_day, short_date, short_datetime
from .thread_id import ThreadId


REPO_ROOT = workspace_root()
CALENDAR_MD = REPO_ROOT / "CALENDAR.md"
CALENDAR_PAST_MD = REPO_ROOT / "CALENDAR-PAST.md"


_AGENDA_KINDS = frozenset({"deadline", "event", "valid_until", "mentioned"})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
TEMPLATES.env.filters["short_date"] = short_date
TEMPLATES.env.filters["short_datetime"] = short_datetime
TEMPLATES.env.filters["relative_day"] = relative_day


app = FastAPI()
app.add_middleware(MailAuthMiddleware)

app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _renderable_state(
    state: summarize_mod.SummaryState | None,
) -> summarize_mod.SummaryState | None:
    """Map a stored summary state to what the UI should render, now that
    generation is retired: any done flavour renders as `done` (no better
    pass will ever arrive), `failed` stays a static error chip, and legacy
    `pending` / `streaming` residue renders as no summary at all."""
    if state is None:
        return None
    if state["status"] in ("done", "done_draft", "done_stale"):
        return {
            "status": "done",
            "short": state["short"],
            "error": None,
            "action_required": state["action_required"],
        }
    if state["status"] == "failed":
        return state
    return None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def get_inbox(request: Request, limit: int = 50):
    threads = inbox_mod.list_inbox(limit=limit)
    # Attach the best stored summary to each thread row. Nothing is
    # scheduled anymore — threads without a done summary render no card.
    with db.connect() as conn:
        tids = [t["thread"] for t in threads]
        thread_to_mid = inbox_mod.thread_latest_mids(conn, tids)
        for t in threads:
            mid = thread_to_mid.get(t["thread"])
            state = (_renderable_state(summarize_mod.read_state(conn, mid))
                     if mid is not None else None)
            t["summary_status"] = state["status"] if state else None
            t["summary"] = state["short"] if state else None
            t["summary_error"] = state["error"] if state else None
            t["summary_action_required"] = (
                state["action_required"] if state else False)
        agenda = agenda_mod.list_upcoming(conn)
    return TEMPLATES.TemplateResponse(
        request, "inbox.html", {"threads": threads, "agenda": agenda},
    )

@app.get("/t/{thread_id}", response_class=HTMLResponse)
def get_thread(request: Request, thread_id: str):
    tid = ThreadId(thread_id)
    msg_id = inbox_mod.latest_message_id_in_thread(tid)
    if msg_id is None:
        raise HTTPException(404, "thread not found")
    return _render_message(request, msg_id, tid)


@app.get("/m/{message_id:path}", response_class=HTMLResponse)
def get_message(request: Request, message_id: str):
    return _render_message(request, urllib.parse.unquote(message_id), thread_id=None)


def _render_message(request: Request, msg_id: str, thread_id: ThreadId | None):
    msg = message_mod.fetch_message(msg_id)
    with db.connect() as conn:
        chips = entities_mod.chips_for_message(conn, msg_id)
    return TEMPLATES.TemplateResponse(
        request, "message.html",
        {
            "msg": msg,
            "msg_id_quoted": urllib.parse.quote(msg_id, safe=""),
            "thread_id": thread_id,
            "entity_chips": chips,
        },
    )


@app.get("/e/{entity_id:int}", response_class=HTMLResponse)
def get_entity(request: Request, entity_id: int):
    """Entity detail: a list of mails where tier-2 extracted this entity.
    One row per thread (latest mention wins). Reuses the same row look
    as the inbox — same shape, same .summary card behaviour."""
    with db.connect() as conn:
        ent = entities_mod.entity_by_id(conn, entity_id)
        if ent is None:
            raise HTTPException(404, "entity not found")
        rows = entities_mod.messages_for_entity(conn, entity_id)
    return TEMPLATES.TemplateResponse(
        request, "entity.html",
        {"entity": ent, "rows": rows},
    )


@app.post("/api/agenda/dismiss", response_class=HTMLResponse)
def post_agenda_dismiss(thread_id: str, kind: str, occurs_at: str):
    """Suppress an agenda card. Params come in as the query string (HTMX
    encodes hx-vals into the POST URL when there's no body) so we don't
    need python-multipart. Returns an empty fragment — paired with
    `hx-swap="outerHTML"` on the card, this removes it from the strip."""
    if kind not in _AGENDA_KINDS:
        raise HTTPException(400, "bad kind")
    if not _ISO_DATE.match(occurs_at):
        raise HTTPException(400, "bad occurs_at")
    if not thread_id:
        raise HTTPException(400, "bad thread_id")
    with db.connect() as conn:
        agenda_mod.dismiss(conn, ThreadId(thread_id), kind, occurs_at)
    return HTMLResponse("")


@app.get("/api/tankekart/{message_id:path}", response_class=HTMLResponse)
def get_tankekart(request: Request, message_id: str, mode: str = "chunks"):
    """Render branches in the requested mode. For each leaf, attach the
    best stored summary (generation is retired — leaves without a done
    summary render no card)."""
    if mode not in ("chunks", "emergent"):
        mode = "chunks"
    valid_mode = cast(related_mod.Mode, mode)
    msg_id = urllib.parse.unquote(message_id)
    with db.connect() as conn:
        branches = related_mod.tankekart(conn, msg_id, mode=valid_mode)
        for branch in branches:
            for leaf in branch["leaves"]:
                state = _renderable_state(
                    summarize_mod.read_state(conn, leaf["message_id"]))
                leaf["summary_status"] = state["status"] if state else "absent"
                leaf["summary"] = state["short"] if state else None
                leaf["summary_error"] = state["error"] if state else None
                leaf["summary_action_required"] = (
                    state["action_required"] if state else False)
    return TEMPLATES.TemplateResponse(
        request, "_tankekart.html",
        {
            "branches": branches,
            "mode": mode,
            "msg_id_quoted": urllib.parse.quote(msg_id, safe=""),
        },
    )


@app.get("/cal/", response_class=HTMLResponse)
def get_calendar(request: Request):
    """Markdown-driven calendar.

    Top: focused list of events in the next 14 days (parsed from
    `CALENDAR.md` per the parser-kontrakt in CALENDAR-RULES.md).
    Bottom: the full CALENDAR.md rendered as HTML, so prose sections
    (Pågående, Recurring, "ikke avklart" play-dates) stay visible
    even though they don't match the strict parser."""
    import datetime as _dt
    events = calendar_mod.parse_calendar(CALENDAR_MD)
    today = _dt.date.today()
    days = calendar_mod.upcoming_by_day(events, today=today, horizon_days=14)
    rendered = calendar_mod.render_markdown(CALENDAR_MD)
    return TEMPLATES.TemplateResponse(
        request, "calendar.html",
        {
            "view": "upcoming",
            "days": days,
            "today": today,
            "rendered_md": rendered,
            "source_name": "CALENDAR.md",
            "past_url": request.url_for("get_calendar_past"),
        },
    )


@app.get("/cal/past/", response_class=HTMLResponse)
def get_calendar_past(request: Request):
    """Archived calendar — just the rendered markdown; no week view."""
    rendered = calendar_mod.render_markdown(CALENDAR_PAST_MD)
    return TEMPLATES.TemplateResponse(
        request, "calendar.html",
        {
            "view": "past",
            "days": [],
            "today": None,
            "rendered_md": rendered,
            "source_name": "CALENDAR-PAST.md",
            "past_url": None,
        },
    )


# The notes queue and shopping list pages (routes, templates, CRUD modules)
# were removed 2026-10 — capture moved to Signal Note-to-Self. The Postgres
# tables (notes_queue, note_attachments, shopping_items) are kept; the data
# stays queryable in mailvec.


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
