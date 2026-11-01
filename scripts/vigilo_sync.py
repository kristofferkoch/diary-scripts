#!/usr/bin/env python3
"""vigilo-sync — pull Vigilo messages / oppslag / samtykker / hentemeldinger into JSONL.

Sink of the Vigilo pipeline; the reader is ``vigiloshown``. Skeleton follows
``spond_sync``: ``--once``, one stderr line, state bumped only on a clean run.

The app gateway offers **no** date filter, no cursor and no pagination — just
``pageSize`` (ceiling unknown, we ask for 100 and log when that many come back).
So "new" and "answered" are our own judgements, made from the thread-list
signature (see :func:`vigilo_api.thread_signature`): a thread we have not seen
is a ``message``, one whose signature moved is a ``message_update``. Upstream's
threadUid-set-only model never forwards a reply — that would silently drop
every answer an AKS teacher gives.

Output: ``/var/lib/diary/legacy-state/vigilo/YYYY-MM-DD.jsonl``, one record per line::

    {"kind": "message"|"message_update"|"news"|"consent"|"check_out_note",
     "fetched_at": "…Z", "data": {…}}

``data`` is deliberately *distilled* (threadUid, unit, title, per-message
sender/time/body, attachment refs) rather than raw: the raw thread carries
pre-signed Azure Blob URLs that expire in ~6 h, and persisting those would put
a usable URL for someone else's school documents into a git-tracked file.
Attachments are downloaded immediately — in the same run that fetches the
thread detail, because the SAS URLs expire in ~6 h — to
``/var/lib/diary/legacy-state/vigilo/attachments/<threadUid>/<name>``, which is git-ignored.

State: ``/var/lib/diary/legacy-state/vigilo-state.json``::

    {
      "threads":   { "<threadUid>": {"sig": "…", "time_created": "…", "message_count": 2} },
      "news":      { "<postId>": "<signature>" },
      "consents":  { "<id>": "<signature>" },
      "check_out_notes": { "<noteId>": {"sig": "…", "emitted": "…Z",
                                        "childId": "…", "title": "…",
                                        "unit": "…"} },
      "attachment_retries": { "<threadUid>": 1 },
      "web_available": true,          # Phase B2 verdict, learned at runtime
      "auth_required": false,
      "last_successful_run": "…Z"
    }

A thread whose attachment download failed is **not** recorded as fully seen —
it is retried for a few cycles and only then given up on with a note
(vigilo2smtp marks seen anyway and loses the file forever; not copying that).

Auth loss is a normal condition, not a crash: ``auth_required`` is set, the
cursor is left alone, and we **exit 0** so a sjekk never breaks.

Examples::

    uv run vigilo-sync --once
    uv run vigilo-sync --once --kinds message,message_update
    uv run vigilo-sync --once --dry-run
    uv run vigilo-sync --once --force        # ignore the 5-minute poll floor
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mail_reader.config import cfg, legacy_state_root

from scripts.attachment_store import find_attachment, write_attachment
from scripts.safe_io import append_text, ensure_dir
from scripts.vigilo_api import (
    AuthExpiredError,
    ConfigError,
    POLL_FLOOR_SECONDS,
    ApiError,
    VigiloApi,
    VigiloError,
    person_name,
    thread_signature,
    unit_name,
)

STATE_PATH = legacy_state_root() / "vigilo-state.json"
OUT_DIR = legacy_state_root() / "vigilo"
ATTACH_DIR = OUT_DIR / "attachments"

VALID_KINDS = {"message", "message_update", "news", "consent", "check_out_note"}
PAGE_SIZE = 100
ATTACH_RETRY_BUDGET = 3
NEWS_WINDOW_DAYS = 30
# A hentemelding is a rule with a validity span, so the window only has to
# *overlap* it: today → today + lookahead catches the standing note today and
# a note written for next term before the term starts.
CHECKOUT_LOOKAHEAD_DAYS = 120
# `toDate` is a hard stop — the day after it AKS has no written permission at
# all and only lets the child out to an adult. So an expiring note is re-shot
# on a drumbeat inside the horizon instead of once, or the warning scrolls out
# of the sjekk before anyone acts on it.
CHECKOUT_EXPIRY_WARN_DAYS = 21
CHECKOUT_REMINDER_GAP_DAYS = 6
# Cap on per-post detail fetches when the feed doesn't inline attachments:
# without it, the first sync after a long silence fires one extra request
# per post in the window. Beyond the cap the ids/names stay empty and the
# reader just shows the header.
NEWS_DETAIL_FETCH_CAP = 200

_UNSAFE = re.compile(r"[^\w.()\[\]-]+")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> date:
    """Local today, through one seam.

    The expiry maths is all ``today``-relative, and a fixture that hardcodes
    ``toDate: "2026-12-24"`` starts failing on its own terms once the calendar
    walks past that date — so the tests pin this instead of the data. One
    function, one place, no CLI surface pretending it can move the clock.
    """
    return date.today()


def safe_filename(name: str, index: int) -> str:
    """A filesystem-safe attachment name — server-supplied, so assume nothing.

    >>> safe_filename("Uke 42.pdf", 0)
    'Uke_42.pdf'
    >>> safe_filename("../../etc/passwd", 1)
    '1_passwd'
    >>> safe_filename("", 2)
    '2_attachment'
    """
    cleaned = _UNSAFE.sub("_", Path(name or "").name).strip("._") or "attachment"
    return cleaned if index == 0 else f"{index}_{cleaned}"


LEGACY_STATE_PATH = STATE_PATH


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for key in ("threads", "news", "consents", "check_out_notes", "attachment_retries"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    for key in ("threads", "check_out_notes"):
        data[key] = {
            str(item_id): entry for item_id, entry in data[key].items()
            if isinstance(entry, dict)
        }
    data["attachment_retries"] = {
        str(item_id): attempts for item_id, attempts in data["attachment_retries"].items()
        if type(attempts) is int and attempts >= 0
    }
    return data


def save_state(
    state: dict[str, Any], *, bump: bool = True,
    path: Path | None = None, private: bool = False,
) -> None:
    from scripts.safe_io import atomic_write_text
    path = path or STATE_PATH
    if bump:
        state["last_successful_run"] = utcnow_iso()
        state["auth_required"] = False
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        mode=0o600 if private else 0o664,
    )


def mirror_legacy_state(state: dict[str, Any]) -> None:
    from scripts.safe_io import write_text_nofollow
    try:
        write_text_nofollow(
            LEGACY_STATE_PATH,
            json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            mode=0o664,
        )
    except OSError as exc:
        print(f"!! vigilo legacy state mirror failed: {exc}", file=sys.stderr)


def child_labels() -> dict[str, str]:
    """``[vigilo].child_labels`` — childId → short label (private config)."""
    labels = cfg("vigilo.child_labels", {})
    return labels if isinstance(labels, dict) else {}


def unit_label(unit_id: Any, fallback: str) -> str:
    """Config label first, the API's own name otherwise."""
    labels = cfg("vigilo.unit_labels", {})
    if isinstance(labels, dict) and unit_id is not None and str(unit_id) in labels:
        return str(labels[str(unit_id)])
    return fallback


# --- Messages ---------------------------------------------------------------


def _messages_brief(
    messages: list[dict[str, Any]], paths: dict[tuple[int, int], Path]
) -> list[dict[str, Any]]:
    """The only shape we persist per message: who, when, what, which files.

    ``paths`` is keyed by (message index, attachment index) — the local file
    name is ours (sanitised, de-duplicated), so recording where each attachment
    actually landed beats making the reader guess it later.
    """
    brief: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        atts: list[dict[str, Any]] = []
        for j, att in enumerate(msg.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            entry: dict[str, Any] = {"name": att.get("name"), "mimeType": att.get("mimeType")}
            if (i, j) in paths:
                entry["path"] = str(paths[(i, j)])
            atts.append(entry)
        brief.append(
            {
                "sender": person_name(msg.get("sender")),
                "timeCreated": msg.get("timeCreated"),
                "body": msg.get("body") or "",
                "attachments": atts,
            }
        )
    return brief


def _thread_record(
    thread: dict[str, Any], child_id: str, brief: list[dict[str, Any]],
    messages: list[dict[str, Any]], note: str | None = None,
) -> dict[str, Any]:
    """One JSONL record for a thread — distilled, never raw.

    The raw thread embeds pre-signed SAS URLs; those stay out of the file.
    """
    unit_id = thread.get("organizationalUnitId")
    data: dict[str, Any] = {
        "threadUid": thread.get("threadUid") or thread.get("id"),
        "unit": unit_label(unit_id, unit_name(thread.get("organizationalUnit"))),
        "title": thread.get("title") or "",
        "childId": child_id,
        "messages": brief,
    }
    if note:
        data["note"] = note
    data["attachments"] = [a for m in brief for a in m["attachments"]]
    return data


def fetch_threads(
    api: VigiloApi, state: dict[str, Any], out: list[dict[str, Any]], child_ids: list[str]
) -> dict[str, int]:
    """New threads + threads whose signature moved, with attachments pulled."""
    seen: dict[str, Any] = state.setdefault("threads", {})
    retries: dict[str, int] = state.setdefault("attachment_retries", {})
    fetched_at = utcnow_iso()
    counts = {"message": 0, "message_update": 0}
    hit_ceiling = False

    for child_id in child_ids:
        threads = api.message_threads(child_id, page_size=PAGE_SIZE)
        if len(threads) >= PAGE_SIZE:
            hit_ceiling = True
        for thread in threads:
            uid = thread.get("threadUid") or thread.get("id")
            if not uid:
                continue
            sig = thread_signature(thread)
            known = seen.get(str(uid))
            if known is not None and (not sig or known.get("sig") == sig):
                continue
            kind = "message" if known is None else "message_update"

            # SAS URLs are ~6 h old the moment they are signed: fetch the
            # detail here and download in the same breath.
            detail = api.thread(str(uid))
            messages = [m for m in (detail.get("messages") or []) if isinstance(m, dict)]
            failed, paths = _download_attachments(str(uid), messages)
            brief = _messages_brief(messages, paths)
            if failed:
                attempts = int(retries.get(str(uid), 0)) + 1
                retries[str(uid)] = attempts
                if attempts <= ATTACH_RETRY_BUDGET:
                    # Signature deliberately left unrecorded: next cycle
                    # re-emits and retries the file instead of losing it.
                    print(
                        f"!! vigilo: {len(failed)} attachment(s) failed for "
                        f"{uid} (attempt {attempts}), retrying next run",
                        file=sys.stderr,
                    )
                    record = _thread_record(
                        thread, child_id, brief, messages,
                        note=f"attachment download failed "
                             f"(attempt {attempts}/{ATTACH_RETRY_BUDGET}): "
                             f"{', '.join(failed)}",
                    )
                    out.append({"kind": kind, "fetched_at": fetched_at, "data": record})
                    counts[kind] += 1
                    continue
                print(
                    f"!! vigilo: giving up on {len(failed)} attachment(s) for "
                    f"{uid} after {ATTACH_RETRY_BUDGET} attempts",
                    file=sys.stderr,
                )
            retries.pop(str(uid), None)

            newest = max((str(m.get("timeCreated") or "") for m in messages), default="")
            seen[str(uid)] = {
                "sig": sig,
                "time_created": newest or thread.get("timeCreated"),
                "message_count": len(messages),
            }
            out.append({
                "kind": kind,
                "fetched_at": fetched_at,
                "data": _thread_record(thread, child_id, brief, messages),
            })
            counts[kind] += 1
    if hit_ceiling:
        print(
            f"!! vigilo: the thread list returned the pageSize ceiling "
            f"({PAGE_SIZE}) — older threads are invisible; see docs/vigilo-pipeline.md",
            file=sys.stderr,
        )
    return counts


def _download_attachments(
    uid: str, messages: list[dict[str, Any]]
) -> tuple[list[str], dict[tuple[int, int], Path]]:
    """Download every message attachment into ``ATTACH_DIR/<uid>/``.

    Returns ``(failed_names, {(msg, att): local_path})`` — an empty failure
    list is what lets the thread be recorded as fully seen. Files already on
    disk are kept as-is, so a retry after a partial failure does not
    re-download what already arrived.
    """
    failed: list[str] = []
    paths: dict[tuple[int, int], Path] = {}
    used: set[str] = set()
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        for j, att in enumerate(msg.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            url = att.get("url")
            name = str(att.get("name") or "attachment")
            if not url:
                continue
            target = ATTACH_DIR / uid / safe_filename(name, 0 if not used else i + j + 1)
            while target.name in used:  # two attachments, one name
                target = target.with_name(f"_{target.name}")
            used.add(target.name)
            try:
                existing = find_attachment(ATTACH_DIR, uid, target.name)
                if existing is not None:
                    paths[(i, j)] = existing
                    continue
                data = VigiloApi.download_attachment(str(url))
                target = write_attachment(ATTACH_DIR, uid, target.name, data)
            except Exception as e:  # noqa: BLE001 — one bad file must not kill the run
                print(f"!! vigilo: attachment {name!r}: {e}", file=sys.stderr)
                failed.append(name)
                continue
            paths[(i, j)] = target
    return failed, paths


# --- Web portal kinds --------------------------------------------------------


def _signature(*parts: Any) -> str:
    return "|".join("" if p is None else str(p) for p in parts)


def fetch_news(api: VigiloApi, state: dict[str, Any], out: list[dict[str, Any]],
               child_ids: list[str]) -> dict[str, int]:
    """Oppslag / «Siste nytt» — where the ukeplan PDFs actually live."""
    seen: dict[str, Any] = state.setdefault("news", {})
    fetched_at = utcnow_iso()
    day = today()
    frm = (day - timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
    to = (day + timedelta(days=1)).isoformat()
    new = 0
    for child_id in child_ids:
        for post in api.news_feed(child_id, from_date=frm, to_date=to):
            pid = post.get("id") or post.get("postId")
            if not pid:
                continue
            sig = _signature(post.get("title") or post.get("heading"),
                             post.get("created") or post.get("timeCreated"),
                             post.get("updated"))
            if seen.get(str(pid)) == sig:
                continue
            data: dict[str, Any] = {
                "postId": pid,
                "childId": child_id,
                "title": post.get("title") or post.get("heading") or "",
                "created": post.get("created") or post.get("timeCreated"),
                "body": post.get("body") or post.get("text") or "",
                # ids + names only; these download over the Bearer endpoint at
                # show time, so unlike message attachments they have no clock.
                "attachments": [
                    {"id": a.get("id"), "name": a.get("name"), "mimeType": a.get("mimeType")}
                    for a in (post.get("attachments") or [])
                    if isinstance(a, dict)
                ],
            }
            if not data["attachments"] and len(seen) < NEWS_DETAIL_FETCH_CAP:
                try:
                    detail = api.post(str(pid))
                    data["attachments"] = [
                        {"id": a.get("id"), "name": a.get("name"), "mimeType": a.get("mimeType")}
                        for a in (detail.get("attachments") or [])
                        if isinstance(a, dict)
                    ]
                    data["body"] = data["body"] or detail.get("body") or detail.get("text") or ""
                except (ApiError, VigiloError):
                    pass
            seen[str(pid)] = sig
            out.append({"kind": "news", "fetched_at": fetched_at, "data": data})
            new += 1
    return {"news": new}


def fetch_consents(api: VigiloApi, state: dict[str, Any], out: list[dict[str, Any]],
                   units: list[tuple[str, str, str]]) -> dict[str, int]:
    """Samtykker, with deadlines surfaced — the kind most likely to need action.

    Cross-checks the per-unit lists against the app gateway's counts
    endpoint: the list answers 404-as-empty, so without the counts a rotted
    list endpoint would silently look like "no forms".
    """
    seen: dict[str, Any] = state.setdefault("consents", {})
    fetched_at = utcnow_iso()
    new = 0
    listed = 0
    for child_id, unit_id, unit_label_text in units:
        for form in api.consent_forms(child_id, unit_id):
            listed += 1
            fid = form.get("id") or form.get("consentFormId")
            if not fid:
                continue
            deadline = (form.get("deadline") or form.get("dueDate")
                        or form.get("expiresAt") or form.get("responseDeadline"))
            status = form.get("status") or form.get("state") or form.get("consentStatus")
            sig = _signature(status, deadline, form.get("title") or form.get("name"))
            if seen.get(str(fid)) == sig:
                continue
            seen[str(fid)] = sig
            out.append(
                {
                    "kind": "consent",
                    "fetched_at": fetched_at,
                    "data": {
                        "consentId": fid,
                        "childId": child_id,
                        "unit": unit_label(unit_id, unit_label_text),
                        "title": form.get("title") or form.get("name") or "",
                        "status": status,
                        "deadline": deadline,
                        "body": form.get("description") or form.get("text") or "",
                    },
                }
            )
            new += 1
    # Cross-check against the counts endpoint (the one consent route verified
    # to answer 200 live): the per-unit list answers 404-as-empty, so without
    # this a rotted list endpoint would silently look like "no forms".
    try:
        total = int(api.consent_form_counts().get("totalConsentFormsCount") or 0)
    except AuthExpiredError:
        raise  # a dead refresh token is main()'s job, not a fetch failure
    except (ApiError, VigiloError, TypeError, ValueError) as e:
        print(f"!! vigilo: consent-form counts probe failed: {e}", file=sys.stderr)
    else:
        if total and not listed:
            print(
                f"!! vigilo: consent-form counts say {total} form(s) exist but "
                "the per-unit lists came back empty — the list endpoint "
                "mapping may have rotted",
                file=sys.stderr,
            )
    return {"consent": new}


_DAY_ORDER = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_SHORT = {
    "monday": "man", "tuesday": "tir", "wednesday": "ons", "thursday": "tor",
    "friday": "fre", "saturday": "lør", "sunday": "søn",
}


def weekday_list(days: Any) -> str:
    """Vigilo's English weekday names as a compact Nordic list, in calendar order.

    >>> weekday_list(["wednesday", "monday"])
    'man,ons'
    >>> weekday_list(["monday", "tuesday", "wednesday", "thursday", "friday"])
    'man,tir,ons,tor,fre'
    >>> weekday_list(None)
    ''
    >>> weekday_list("monday,friday")
    'man,fre'
    >>> weekday_list(["Secondness"])
    'Secondness'
    """
    if isinstance(days, str):
        # A CSV string instead of a list: iterating it would hand back letters
        # and produce "m,o,n,d,a,y" in every header, forever, silently.
        days = [part for part in days.split(",") if part.strip()]
    names = [str(d).strip() for d in (days or [])]
    lower = [n.lower() for n in names]
    ordered = [_DAY_SHORT[d] for d in _DAY_ORDER if d in lower]
    return ",".join(ordered + [n for n, low in zip(names, lower) if low not in _DAY_ORDER])


def _date_only(value: Any) -> str | None:
    """``"2026-12-24T00:00:00"`` → ``"2026-12-24"``; these dates arrive as datetimes.

    >>> _date_only("2026-12-24T00:00:00")
    '2026-12-24'
    >>> _date_only(None) is None
    True
    """
    return str(value)[:10] if value else None


def _days_until(value: Any, today: date) -> int | None:
    """Days from ``today`` to ``value`` (negative once past), None when unreadable.

    >>> from datetime import date as _d
    >>> _days_until("2026-12-24T00:00:00", _d(2026, 12, 20))
    4
    >>> _days_until("2026-10-01", _d(2026, 12, 20))
    -80
    >>> _days_until(None, _d(2026, 12, 20)) is None
    True
    """
    if not value:
        return None
    try:
        return (date.fromisoformat(str(value)[:10]) - today).days
    except ValueError:
        return None


def _reminder_due(emitted: Any, today: date, *, gap: int = CHECKOUT_REMINDER_GAP_DAYS) -> bool:
    """True when a note reminder is overdue — never sent, or older than ``gap``.

    >>> from datetime import date as _d
    >>> _reminder_due(None, _d(2026, 12, 20))
    True
    >>> _reminder_due("2026-12-19T09:00:00Z", _d(2026, 12, 20))
    False
    >>> _reminder_due("2026-12-13T09:00:00Z", _d(2026, 12, 20))
    True
    """
    if not emitted:
        return True
    try:
        then = datetime.strptime(str(emitted), "%Y-%m-%dT%H:%M:%SZ").date()
    except ValueError:
        return True
    return (today - then).days >= gap


def _check_out_note_data(note: dict[str, Any], *, child_id: str, unit_names: dict[str, str],
                         change: str, days_left: int | None) -> dict[str, Any]:
    """Distil one check-in note into the record payload (no ``dateOccurrences``).

    The expansion is the one field never to persist: 93 ISO timestamps per
    note per term, all derivable from ``recurrencePattern`` + ``fromDate``,
    and it would swamp a git-tracked JSONL.
    """
    pattern = note.get("recurrencePattern") or {}
    unit_id = str(note.get("organizationalUnitId") or "")
    return {
        "noteId": note.get("id"),
        "childId": child_id,
        "unit": unit_label(unit_id, unit_names.get(unit_id, "?")),
        "title": note.get("noteText") or "",
        "timeOfDay": note.get("timeOfDay"),
        "days": weekday_list(pattern.get("daysOfWeek")),
        "recurrenceType": pattern.get("recurrenceType"),
        "recurrenceInterval": pattern.get("interval"),
        "recurring": note.get("isRecurring"),
        "enabled": note.get("isEnabled"),
        "typeName": (note.get("type") or {}).get("name") if isinstance(
            note.get("type"), dict) else note.get("type"),
        "category": note.get("category"),
        "fromDate": _date_only(note.get("fromDate")),
        "toDate": _date_only(note.get("toDate")),
        "daysLeft": days_left,
        "created": note.get("lastUpdatedTime") or note.get("registeredTime"),
        "change": change,
    }


def fetch_check_out_notes(api: VigiloApi, state: dict[str, Any],
                          out: list[dict[str, Any]], child_ids: list[str],
                          units: list[tuple[str, str, str]]) -> dict[str, int]:
    """Hentemeldinger — the standing «går hjem selv» permission, and its expiry.

    A third feed, invisible in both the message threads and the news feed, so
    this kind exists purely because the note lives on its own endpoint. Two
    things the sjekk must not miss:

    * the note was **written or edited** — a hentetid *is* a pickup plan, and
      a parent changing it mid-term is exactly the kind of thing a week of
      silence hides;
    * the note is **about to expire**. ``toDate`` is a hard stop: from the day
      after it AKS lets the child out only to an adult in person, and nothing
      in the app warns you beforehand. So a note inside the warning horizon is
      re-emitted on a weekly drumbeat, not once.

    A note that stops coming back (expired, deleted, or switched off out of
    the window) gets one record saying so — the permission is gone, which is
    the fact the whole pickup plan rests on. The text and unit are memoed in
    state for exactly that sentence.

    ``units`` is the web gateway's own ``(childId, unitId, name)`` list, which
    does double duty: the labels, and the *only* authority for announcing a
    vanishing. ``/api/check-in-notes`` answers an unknown child with 200 and an
    empty list, never a 404 — so an empty answer proves nothing on its own, and
    a child the web gateway has never named must not have its notes declared
    gone. Fetching with a bad id would otherwise announce "you lost the
    permission" on the very first run, forever.
    """
    seen: dict[str, Any] = state.setdefault("check_out_notes", {})
    fetched_at = utcnow_iso()
    day = today()
    unit_names = {unit_id: name for _, unit_id, name in units}
    confirmed = {child for child, _, _ in units if child}
    frm = day.isoformat()
    to = (day + timedelta(days=CHECKOUT_LOOKAHEAD_DAYS)).isoformat()
    new = 0

    for child_id in child_ids:
        try:
            notes = api.check_in_notes(child_id, from_date=frm, to_date=to)
        except AuthExpiredError:
            raise
        except (ApiError, VigiloError) as e:
            # Two children, one bad answer: what came back for the other one is
            # still worth a record. And this child's memoed notes must be left
            # alone — a failure is not an empty list.
            print(f"!! vigilo: hentemelding fetch failed for {child_id}: {e}",
                  file=sys.stderr)
            continue
        live: set[str] = set()
        for note in notes:
            if not isinstance(note, dict) or not note.get("id"):
                continue
            nid = str(note["id"])
            live.add(nid)
            pattern = note.get("recurrencePattern") or {}
            sig = _signature(
                note.get("noteText"), note.get("timeOfDay"),
                weekday_list(pattern.get("daysOfWeek")),
                pattern.get("recurrenceType"), pattern.get("interval"),
                note.get("isRecurring"), note.get("fromDate"),
                note.get("toDate"), note.get("isEnabled"),
                (note.get("type") or {}).get("name") if isinstance(note.get("type"), dict)
                else note.get("type"),
                note.get("lastUpdatedTime"),
            )
            entry = seen.get(nid) if isinstance(seen.get(nid), dict) else None
            days_left = _days_until(note.get("toDate"), day)
            if days_left is None and note.get("toDate"):
                # An unreadable expiry used to mean "never warn again", which
                # is the one failure this kind must not have: the date is the
                # whole reason it exists.
                print(f"!! vigilo: hentemelding {nid} has an unreadable toDate "
                      f"{note.get('toDate')!r} — no expiry warning for it",
                      file=sys.stderr)
            change: str | None = None
            if entry is None or entry.get("sig") != sig:
                change = "endret" if entry else "ny"
            elif days_left is not None and days_left <= CHECKOUT_EXPIRY_WARN_DAYS:
                if _reminder_due(entry.get("emitted"), day):
                    # A note past its toDate should not be coming back at all;
                    # if it does, "utløper om -3 dager" is not a sentence.
                    change = (f"utløper om {days_left} dager" if days_left >= 0
                              else "utløpt — returneres fortsatt av AKS")
            if change is None:
                continue
            data = _check_out_note_data(note, child_id=child_id, unit_names=unit_names,
                                        change=change, days_left=days_left)
            seen[nid] = {
                "sig": sig,
                "emitted": fetched_at,
                "childId": child_id,
                "title": data["title"],
                "unit": data["unit"],
            }
            out.append({"kind": "check_out_note", "fetched_at": fetched_at, "data": data})
            new += 1

        if child_id not in confirmed:
            # Empty or not, this child was never named by the web gateway — and
            # an unknown id answers 200 with an empty list, so nothing here may
            # be read as "the permission is gone".
            if any(isinstance(e, dict) and e.get("childId") == child_id
                   for e in seen.values()):
                print("!! vigilo: the web gateway did not name this child, so its "
                      "hentemeldinger are not checked for expiry or deletion",
                      file=sys.stderr)
            continue
        for nid, entry in list(seen.items()):
            if not isinstance(entry, dict) or entry.get("childId") != child_id:
                continue
            if nid in live:
                continue
            seen.pop(nid)
            out.append({
                "kind": "check_out_note",
                "fetched_at": fetched_at,
                "data": {
                    "noteId": nid,
                    "childId": child_id,
                    "unit": entry.get("unit"),
                    "title": entry.get("title") or "",
                    "change": "returneres ikke lenger (utløpt eller slettet)",
                },
            })
            new += 1
    return {"check_out_note": new}


# --- Run --------------------------------------------------------------------


def make_api() -> VigiloApi:
    """Factory hook — tests swap in a stubbed transport here, not on the class.

    (Patching ``VigiloApi`` wholesale would also swap out the attachment
    downloader, which is the other thing tests need to fake.)
    """
    return VigiloApi()


def run_once(
    kinds: set[str], state: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], dict[str, int]]:
    api = make_api()
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    try:
        children = api.children()
        child_ids = [str(c["childId"]) for c in children if c.get("childId")]
        if not child_ids:
            raise ApiError(
                "no children on this account — wrong account, or the app "
                "gateway changed shape"
            )
        labels = child_labels()
        who = [
            labels.get(str(c.get("childId")), person_name(c) or str(c.get("childId")))
            for c in children
            if c.get("childId")
        ]
        print(f"vigilo: {len(child_ids)} child(ren): {', '.join(who)}", file=sys.stderr)

        if kinds & {"message", "message_update"}:
            counts.update(fetch_threads(api, state, records, child_ids))

        web_kinds = kinds & {"news", "consent", "check_out_note"}
        if web_kinds:
            if state.get("web_available") is False:
                print(
                    "vigilo: skipping web-gateway kinds (news/consent/check_out_note) — "
                    "the app token was refused there. Re-run vigilo-login, or remove "
                    "web_available from /var/lib/diary/legacy-state/vigilo-state.json to probe again.",
                    file=sys.stderr,
                )
            else:
                units: list[tuple[str, str, str]] = []
                try:
                    for kid in api.web_children():
                        kid_id = str(kid.get("id") or kid.get("childId") or "")
                        for unit in kid.get("organizationalUnits") or []:
                            if isinstance(unit, dict) and unit.get("id"):
                                units.append(
                                    (kid_id, str(unit["id"]), unit_name(unit))
                                )
                    if state.get("web_available") is None:
                        print(
                            "vigilo: web gateway accepts the app token ✔ "
                            f"({len(units)} unit(s))",
                            file=sys.stderr,
                        )
                    state["web_available"] = True
                except AuthExpiredError:
                    state["web_available"] = False
                    print(
                        "!! vigilo: the web gateway refused the app token — "
                        "news/consent/check_out_note are out of reach (docs/vigilo-pipeline.md "
                        "Phase B2 fallback). Messages still work.",
                        file=sys.stderr,
                    )
                except (ApiError, VigiloError) as e:
                    print(f"!! vigilo: web gateway probe failed: {e}", file=sys.stderr)
                    units = []

                if units:
                    # Per-kind isolation: a dying endpoint (the consent 404
                    # took down whole runs on 2026-10-11) must not lose the
                    # records and state the other kinds already produced.
                    # AuthExpiredError must still propagate — main() turns it
                    # into the auth_required flag + "run vigilo-login".
                    if "news" in kinds:
                        try:
                            counts.update(fetch_news(api, state, records, child_ids))
                        except AuthExpiredError:
                            raise
                        except (ApiError, VigiloError) as e:
                            print(f"!! vigilo: news fetch failed: {e}", file=sys.stderr)
                    if "consent" in kinds:
                        try:
                            counts.update(fetch_consents(api, state, records, units))
                        except AuthExpiredError:
                            raise
                        except (ApiError, VigiloError) as e:
                            print(f"!! vigilo: consent fetch failed: {e}", file=sys.stderr)
                    if "check_out_note" in kinds:
                        # ConfigError is a VigiloError, so a config hiccup in
                        # unit_label degrades this kind like any other failure
                        # rather than killing a run that is here for the mail.
                        try:
                            counts.update(fetch_check_out_notes(
                                api, state, records, child_ids, units,
                            ))
                        except AuthExpiredError:
                            raise
                        except (ApiError, VigiloError) as e:
                            print(f"!! vigilo: check-out note fetch failed: {e}",
                                  file=sys.stderr)
    finally:
        api.close()
    return sum(counts.values()), records, counts


def today_jsonl() -> Path:
    ensure_dir(OUT_DIR)
    return OUT_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
    append_text(path, text)


def poll_floor_seconds() -> int:
    try:
        return max(0, int(cfg("vigilo.poll_floor_seconds", POLL_FLOOR_SECONDS)))
    except (TypeError, ValueError):
        return POLL_FLOOR_SECONDS


def recently_ran(state: dict[str, Any]) -> bool:
    """True when a successful run happened inside the poll floor (be gentle)."""
    floor = poll_floor_seconds()
    last = state.get("last_successful_run")
    if not floor or not last:
        return False
    try:
        raw = str(last).replace("Z", "+00:00")
        then = datetime.fromisoformat(raw)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - then
    return timedelta(0) <= age < timedelta(seconds=floor)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--once", action="store_true",
                    help="One-shot fetch (only mode; polling is systemd's job).")
    ap.add_argument("--kinds",
                    default="message,message_update,news,consent,check_out_note",
                    help=f"Comma-separated subset of {sorted(VALID_KINDS)}. "
                         f"check_out_note looks {CHECKOUT_LOOKAHEAD_DAYS} days ahead "
                         f"and re-announces a note inside "
                         f"{CHECKOUT_EXPIRY_WARN_DAYS} days of expiring — the two "
                         "web calls are why the default run takes longer.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print records to stdout; write no JSONL, touch no state.")
    ap.add_argument("--force", action="store_true",
                    help=f"Ignore the {POLL_FLOOR_SECONDS}s poll floor between runs.")
    ap.add_argument("--event-spool", type=Path,
                    help="Also emit fresh in-memory records to this trusted event spool.")
    ap.add_argument("--state-dir", type=Path,
                    help="Read trusted collector state here; mirror legacy state after success.")
    args = ap.parse_args(argv)

    if not args.once:
        ap.error("--once is required (no daemon mode)")

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    bad = kinds - VALID_KINDS
    if bad:
        ap.error(f"unknown kinds: {sorted(bad)}; valid: {sorted(VALID_KINDS)}")

    if args.state_dir:
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.state_dir.chmod(0o700)
    state_path = args.state_dir / "vigilo-state.json" if args.state_dir else STATE_PATH
    state = load_state(state_path)
    first_trusted_run = bool(args.state_dir and not state.get("last_successful_run"))
    if not args.force and not args.dry_run and recently_ran(state):
        print(f"vigilo: ran less than {poll_floor_seconds()}s ago — nothing to do")
        return 0

    def persist(*, bump: bool = True) -> None:
        save_state(state, bump=bump, path=state_path, private=bool(args.state_dir))
        if args.state_dir:
            mirror_legacy_state(state)

    try:
        total, records, counts = run_once(kinds, state)
    except AuthExpiredError as e:
        # Normal end-of-life for a refresh token (30-90 days). Loud, but a
        # success as far as the sjekk is concerned.
        state["auth_required"] = True
        persist(bump=False)
        print("!! vigilo auth expired — run vigilo-login", file=sys.stderr)
        print(f"   ({e})", file=sys.stderr)
        return 0
    except ConfigError as e:
        print(f"!! vigilo config error: {e}", file=sys.stderr)
        return 2
    except (ApiError, VigiloError) as e:
        print(f"!! vigilo sync failed: {e}", file=sys.stderr)
        return 1

    print(f"new records: total={total}, by_kind={counts}", file=sys.stderr)
    if args.dry_run:
        for rec in records:
            print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.event_spool:
        from scripts.event_export import emit_records
        emit_records(args.event_spool, "vigilo", records)
    if total and not first_trusted_run:
        append_jsonl(today_jsonl(), records)
    persist()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
