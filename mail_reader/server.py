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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agenda as agenda_mod
from . import calendar_md as calendar_mod
from . import db, inbox as inbox_mod, message as message_mod, related as related_mod
from . import note_images as note_images_mod
from . import notes as notes_mod
from . import shopping as shopping_mod
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


# --- notes queue -----------------------------------------------------------
# A capture inbox for "things I don't want to forget", digested by the daily
# sjekk. The page is a textarea + a list of pending notes with inline edit /
# delete. Form bodies are parsed by hand (urlencoded) so we avoid pulling in
# python-multipart — same dependency-avoidance as the agenda-dismiss endpoint.


async def _form_field(request: Request, name: str) -> str:
    raw = await request.body()
    parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    values = parsed.get(name)
    return values[0] if values else ""


@app.get("/notes/", response_class=HTMLResponse)
def get_notes(request: Request):
    with db.connect() as conn:
        notes = notes_mod.list_pending(conn)
    return TEMPLATES.TemplateResponse(request, "notes.html", {"notes": notes})


@app.post("/notes/", response_class=HTMLResponse)
async def post_note(
    request: Request,
    body: str = Form(""),
    image: UploadFile | None = File(None),
):
    """Add a note, optionally with a photo. Returns the new note's <li> for
    HTMX to prepend.

    The capture form is multipart (it carries a file input), so this is the
    one notes endpoint that uses python-multipart rather than the hand-parsed
    urlencoded helper. A note may carry text, an image, or both — an
    image-only note (blank text) is valid. Bad image bytes fail loudly with
    400 before any row is written."""
    raw = await image.read() if image is not None and image.filename else b""
    processed = None
    if raw:
        try:
            processed = note_images_mod.process(raw)
        except note_images_mod.NotAnImage:
            raise HTTPException(400, "ugyldig bildefil")
    with db.connect() as conn:
        try:
            note = notes_mod.add(conn, body, allow_empty=processed is not None)
        except ValueError:
            raise HTTPException(400, "empty note")
        if processed is not None:
            notes_mod.add_attachment(conn, note["id"], processed)
            note = notes_mod.get(conn, note["id"])
    return TEMPLATES.TemplateResponse(request, "_note.html", {"note": note})


@app.get("/notes/attachment/{attachment_id:int}")
def get_note_attachment(attachment_id: int):
    """Full web-size image for a note attachment. Content is immutable for a
    given id, so it caches hard."""
    return _serve_attachment(attachment_id, thumb=False)


@app.get("/notes/attachment/{attachment_id:int}/thumb")
def get_note_attachment_thumb(attachment_id: int):
    """Small thumbnail for the note list."""
    return _serve_attachment(attachment_id, thumb=True)


def _serve_attachment(attachment_id: int, *, thumb: bool) -> Response:
    with db.connect() as conn:
        blob = notes_mod.get_attachment_blob(conn, attachment_id, thumb=thumb)
    if blob is None:
        raise HTTPException(404, "attachment not found")
    mime_type, data = blob
    return Response(
        content=data,
        media_type=mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get("/notes/{note_id:int}", response_class=HTMLResponse)
def get_note(request: Request, note_id: int):
    """Display fragment for one note — used to cancel an inline edit."""
    with db.connect() as conn:
        note = notes_mod.get(conn, note_id)
    if note is None:
        raise HTTPException(404, "note not found")
    return TEMPLATES.TemplateResponse(request, "_note.html", {"note": note})


@app.get("/notes/{note_id:int}/edit", response_class=HTMLResponse)
def get_note_edit(request: Request, note_id: int):
    with db.connect() as conn:
        note = notes_mod.get(conn, note_id)
    if note is None:
        raise HTTPException(404, "note not found")
    return TEMPLATES.TemplateResponse(request, "_note_edit.html", {"note": note})


@app.post("/notes/{note_id:int}", response_class=HTMLResponse)
async def post_note_update(request: Request, note_id: int):
    body = await _form_field(request, "body")
    with db.connect() as conn:
        try:
            note = notes_mod.update_body(conn, note_id, body)
        except ValueError:
            raise HTTPException(400, "empty note")
        except KeyError:
            raise HTTPException(404, "note not found")
    return TEMPLATES.TemplateResponse(request, "_note.html", {"note": note})


@app.post("/notes/{note_id:int}/delete", response_class=HTMLResponse)
def post_note_delete(note_id: int):
    with db.connect() as conn:
        try:
            notes_mod.delete(conn, note_id)
        except KeyError:
            raise HTTPException(404, "note not found")
    return HTMLResponse("")


# --- shopping list ---------------------------------------------------------
# A standing, categorised checklist worked through in the store. Items group
# by category (Netthandel last); a checkbox tap toggles `checked` and greys
# the row in place. Checks persist and are reversible here; the sjekk routine
# sweeps bought items out with `scripts/shopping.py sweep`. Same hand-parsed
# urlencoded form bodies as the notes endpoints (no python-multipart).


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _shopping_ctx(conn) -> dict:
    """Template context shared by the page and every list-fragment response."""
    items = shopping_mod.list_items(conn)
    return {
        "groups": shopping_mod.grouped(items),
        "any_checked": any(i["checked"] for i in items),
        "categories": shopping_mod.CATEGORIES,
        "default_category": shopping_mod.DEFAULT_CATEGORY,
    }


@app.get("/shopping/", response_class=HTMLResponse)
def get_shopping(request: Request):
    with db.connect() as conn:
        ctx = _shopping_ctx(conn)
    return TEMPLATES.TemplateResponse(request, "shopping.html", ctx)


@app.post("/shopping/", response_class=HTMLResponse)
async def post_shopping_item(request: Request):
    """Add an item, then re-render the whole list so it lands in its group."""
    form = await _form(request)
    with db.connect() as conn:
        try:
            shopping_mod.add(
                conn, form.get("name", ""),
                form.get("category", shopping_mod.DEFAULT_CATEGORY),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        ctx = _shopping_ctx(conn)
    return TEMPLATES.TemplateResponse(request, "_shopping_list.html", ctx)


@app.post("/shopping/uncheck-all", response_class=HTMLResponse)
def post_shopping_uncheck_all(request: Request):
    with db.connect() as conn:
        shopping_mod.uncheck_all(conn)
        ctx = _shopping_ctx(conn)
    return TEMPLATES.TemplateResponse(request, "_shopping_list.html", ctx)


@app.get("/shopping/{item_id:int}", response_class=HTMLResponse)
def get_shopping_item(request: Request, item_id: int):
    """Display fragment for one item — used to cancel an inline edit."""
    with db.connect() as conn:
        item = shopping_mod.get(conn, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return TEMPLATES.TemplateResponse(request, "_shopping_item.html", {"item": item})


@app.get("/shopping/{item_id:int}/edit", response_class=HTMLResponse)
def get_shopping_item_edit(request: Request, item_id: int):
    with db.connect() as conn:
        item = shopping_mod.get(conn, item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return TEMPLATES.TemplateResponse(
        request, "_shopping_item_edit.html",
        {"item": item, "categories": shopping_mod.CATEGORIES},
    )


@app.post("/shopping/{item_id:int}", response_class=HTMLResponse)
async def post_shopping_item_update(request: Request, item_id: int):
    """Save an edit (name + category), then re-render the whole list since
    the category may have moved the item to another group."""
    form = await _form(request)
    with db.connect() as conn:
        try:
            shopping_mod.update(
                conn, item_id,
                name=form.get("name", ""),
                category=form.get("category", shopping_mod.DEFAULT_CATEGORY),
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "item not found")
        ctx = _shopping_ctx(conn)
    return TEMPLATES.TemplateResponse(request, "_shopping_list.html", ctx)


@app.post("/shopping/{item_id:int}/toggle", response_class=HTMLResponse)
def post_shopping_toggle(request: Request, item_id: int):
    """Flip the checkbox; swap just this <li> (greyed, in place)."""
    with db.connect() as conn:
        try:
            item = shopping_mod.toggle(conn, item_id)
        except KeyError:
            raise HTTPException(404, "item not found")
    return TEMPLATES.TemplateResponse(request, "_shopping_item.html", {"item": item})


@app.post("/shopping/{item_id:int}/delete", response_class=HTMLResponse)
def post_shopping_item_delete(item_id: int):
    with db.connect() as conn:
        try:
            shopping_mod.delete(conn, item_id)
        except KeyError:
            raise HTTPException(404, "item not found")
    return HTMLResponse("")


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
