#!/usr/bin/env python3
"""vigilo-sync — pull Vigilo messages / oppslag / samtykker into JSONL.

Sink of the Vigilo pipeline; the reader is ``vigiloshown``. Skeleton follows
``spond_sync``: ``--once``, one stderr line, state bumped only on a clean run.

The app gateway offers **no** date filter, no cursor and no pagination — just
``pageSize`` (ceiling unknown, we ask for 100 and log when that many come back).
So "new" and "answered" are our own judgements, made from the thread-list
signature (see :func:`vigilo_api.thread_signature`): a thread we have not seen
is a ``message``, one whose signature moved is a ``message_update``. Upstream's
threadUid-set-only model never forwards a reply — that would silently drop
every answer an AKS teacher gives.

Output: ``memory/vigilo/YYYY-MM-DD.jsonl``, one record per line::

    {"kind": "message"|"message_update"|"news"|"consent", "fetched_at": "…Z", "data": {…}}

``data`` is deliberately *distilled* (threadUid, unit, title, per-message
sender/time/body, attachment refs) rather than raw: the raw thread carries
pre-signed Azure Blob URLs that expire in ~6 h, and persisting those would put
a usable URL for someone else's school documents into a git-tracked file.
Attachments are downloaded next run… immediately — to
``memory/vigilo/attachments/<threadUid>/<name>``, which is git-ignored.

State: ``memory/vigilo-state.json``::

    {
      "threads":   { "<threadUid>": {"sig": "…", "time_created": "…", "message_count": 2} },
      "news":      { "<postId>": "<signature>" },
      "consents":  { "<id>": "<signature>" },
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

from mail_reader.config import cfg, workspace_root

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

PROJECT_ROOT = workspace_root()
STATE_PATH = PROJECT_ROOT / "memory" / "vigilo-state.json"
OUT_DIR = PROJECT_ROOT / "memory" / "vigilo"
ATTACH_DIR = OUT_DIR / "attachments"

VALID_KINDS = {"message", "message_update", "news", "consent"}
PAGE_SIZE = 100
ATTACH_RETRY_BUDGET = 3
NEWS_WINDOW_DAYS = 30

_UNSAFE = re.compile(r"[^\w.()\[\]-]+")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], *, bump: bool = True) -> None:
    if bump:
        state["last_successful_run"] = utcnow_iso()
        state["auth_required"] = False
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


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
            f"({PAGE_SIZE}) — older threads are invisible; see PLAN-VIGILO.md",
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
            if target.exists() and target.stat().st_size > 0:
                paths[(i, j)] = target
                continue
            try:
                data = VigiloApi.download_attachment(str(url))
            except Exception as e:  # noqa: BLE001 — one bad file must not kill the run
                print(f"!! vigilo: attachment {name!r}: {e}", file=sys.stderr)
                failed.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
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
    today = date.today()
    frm = (today - timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
    to = (today + timedelta(days=1)).isoformat()
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
            if not data["attachments"] and len(seen) < 200:
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
    """Samtykker, with deadlines surfaced — the kind most likely to need action."""
    seen: dict[str, Any] = state.setdefault("consents", {})
    fetched_at = utcnow_iso()
    new = 0
    for child_id, unit_id, unit_label_text in units:
        for form in api.consent_forms(child_id, unit_id):
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
    return {"consent": new}


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

        web_kinds = kinds & {"news", "consent"}
        if web_kinds:
            if state.get("web_available") is False:
                print(
                    "vigilo: skipping web-gateway kinds (news/consent) — the app "
                    "token was refused there. Re-run vigilo-login, or remove "
                    "web_available from memory/vigilo-state.json to probe again.",
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
                        "news/consent are out of reach (PLAN-VIGILO.md Phase B2 "
                        "fallback). Messages still work.",
                        file=sys.stderr,
                    )
                except (ApiError, VigiloError) as e:
                    print(f"!! vigilo: web gateway probe failed: {e}", file=sys.stderr)
                    units = []

                if units:
                    if "news" in kinds:
                        counts.update(fetch_news(api, state, records, child_ids))
                    if "consent" in kinds:
                        counts.update(fetch_consents(api, state, records, units))
    finally:
        api.close()
    return sum(counts.values()), records, counts


def today_jsonl() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


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
    return datetime.now(timezone.utc) - then < timedelta(seconds=floor)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--once", action="store_true",
                    help="One-shot fetch (only mode; polling is systemd's job).")
    ap.add_argument("--kinds", default="message,message_update,news,consent",
                    help=f"Comma-separated subset of {sorted(VALID_KINDS)}.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print records to stdout; write no JSONL, touch no state.")
    ap.add_argument("--force", action="store_true",
                    help=f"Ignore the {POLL_FLOOR_SECONDS}s poll floor between runs.")
    args = ap.parse_args(argv)

    if not args.once:
        ap.error("--once is required (no daemon mode)")

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    bad = kinds - VALID_KINDS
    if bad:
        ap.error(f"unknown kinds: {sorted(bad)}; valid: {sorted(VALID_KINDS)}")

    state = load_state()
    if not args.force and not args.dry_run and recently_ran(state):
        print(f"vigilo: ran less than {poll_floor_seconds()}s ago — nothing to do")
        return 0

    try:
        total, records, counts = run_once(kinds, state)
    except AuthExpiredError as e:
        # Normal end-of-life for a refresh token (30-90 days). Loud, but a
        # success as far as the sjekk is concerned.
        state["auth_required"] = True
        save_state(state, bump=False)
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
    if total:
        append_jsonl(today_jsonl(), records)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
