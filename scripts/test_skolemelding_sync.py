"""Tests for skolemelding_sync.py. Run with `uv run pytest scripts/`.

Regression focus: duplicate-Id dedupe (the live search answers some messages
twice), feedback-counter changes NOT counting as updates (they move when
other parents answer), attachment-failure requeue, and the exit-0 path when
the login simply expires.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import skolemelding_sync as ss
from scripts.skolemelding_api import SkolemeldingApi, TokenStore

from .test_skolemelding_api import (
    REFRESH_OK,
    Resp,
    login_payload,
    transport,
)


def message(mid: int, *, header: str = "Ukeplan uke 42",
            body: str = "<p>Husk å svare</p>", sender: str = "Eksempel skole",
            timestamp: str = "2026-10-12T08:00:00+02:00",
            attachments: list | None = None, threads: list | None = None,
            **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "Id": mid, "Header": header, "Body": body, "From": sender,
        "Timestamp": timestamp, "IsMessageSeen": False, "IsFavourite": False,
        "Attachments": attachments or [], "Threads": threads or [],
        "IsFeedBackQuestion": False, "IsFeedBackReadConfirmation": False,
        "TotalYes": 0, "TotalNo": 0, "TotalNotAnswered": 5,
    }
    msg.update(extra)
    return msg


def search_route(pages: dict[int, list[dict[str, Any]]], total: int | None = None):
    """A ``POST /api/messagesearchmobile/`` stub answering page N from a dict."""
    if total is None:
        total = sum(len(v) for v in pages.values())

    def _route(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["Page"]
        return httpx.Response(
            200, json={"Result": pages.get(page, []), "TotalMatching": total})

    return _route


ATT_PATH = "/globalassets/attachments/abc/skjema.pdf"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the sink at a scratch workspace (the real one is the diary repo)."""
    root = tmp_path / "memory"
    (root / "skolemelding").mkdir(parents=True)
    monkeypatch.setattr(ss, "STATE_PATH", root / "skolemelding-state.json")
    monkeypatch.setattr(ss, "OUT_DIR", root / "skolemelding")
    monkeypatch.setattr(ss, "ATTACH_DIR", root / "skolemelding" / "attachments")
    monkeypatch.setenv("SKOLEMELDING_TOKEN_FILE", str(tmp_path / "tokens.json"))
    return {"root": root, "jsonl": root / "skolemelding",
            "state": root / "skolemelding-state.json"}


@pytest.fixture
def serve(workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    """Install a stubbed portal behind the real SkolemeldingApi object graph.

    Everything below ``make_api`` is the production code path — including the
    401-retry and refresh logic — only the wire is faked.
    """

    def _serve(routes: dict[str, Any], *, tokens: dict[str, Any] | None = None) -> None:
        t = transport({"POST /token": REFRESH_OK, **routes})
        TokenStore(Path(os.environ["SKOLEMELDING_TOKEN_FILE"]), transport=t).save(
            tokens if tokens is not None else login_payload()
        )
        monkeypatch.setattr(ss, "make_api", lambda: SkolemeldingApi(transport=t))

    return _serve


def gateway(*, pages: dict[int, list[dict[str, Any]]] | None = None,
            total: int | None = None) -> dict[str, Any]:
    return {"POST /api/messagesearchmobile/": search_route(
        pages if pages is not None else {1: [message(1)]}, total)}


def records(jsonl_dir: Path) -> list[dict[str, Any]]:
    out = []
    for fp in sorted(jsonl_dir.glob("*.jsonl")):
        out += [json.loads(line) for line in fp.read_text().splitlines() if line.strip()]
    return out


def age_state(workspace: dict[str, Path]) -> None:
    """Push last_successful_run past the poll floor so the next run proceeds."""
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))


# --- Classification ---------------------------------------------------------


def test_first_run_backfills_all_pages_and_dedupes(serve, workspace):
    """Empty state → every page is read; duplicate rows collapse to one record."""
    pages_seen: list[int] = []
    base = search_route({1: [message(3), message(2)],
                         2: [message(2), message(1, header="Eldre")]}, total=3)

    def _spy(request: httpx.Request) -> httpx.Response:
        pages_seen.append(json.loads(request.content)["Page"])
        return base(request)

    serve({"POST /api/messagesearchmobile/": _spy})
    assert ss.main(["--once"]) == 0
    assert pages_seen == [1, 2]
    recs = records(workspace["jsonl"])
    assert [r["kind"] for r in recs] == ["message"] * 3
    assert [r["data"]["id"] for r in recs] == [3, 2, 1], "duplicate Id row dropped"
    data = recs[0]["data"]
    assert data["header"] == "Ukeplan uke 42"
    assert data["sender"] == "Eksempel skole"
    assert data["body"] == "Husk å svare", "HTML is distilled to text"
    state = json.loads(workspace["state"].read_text())
    assert set(state["messages"]) == {"1", "2", "3"}
    assert state["auth_required"] is False


def test_missing_total_matching_does_not_truncate_the_backfill(serve, workspace):
    """An absent TotalMatching is unknown, not zero — paging must fall back
    to the empty-page stop, or a first-run backfill silently stops at page 1."""
    pages_seen: list[int] = []

    def _route(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["Page"]
        pages_seen.append(page)
        rows = {1: [message(3)], 2: [message(2), message(1)]}.get(page, [])
        return httpx.Response(200, json={"Result": rows})  # no TotalMatching

    serve({"POST /api/messagesearchmobile/": _route})
    assert ss.main(["--once"]) == 0
    assert pages_seen == [1, 2, 3], "paging ends on the empty page"
    assert [r["data"]["id"] for r in records(workspace["jsonl"])] == [3, 2, 1]


def test_unparseable_total_matching_is_unknown_not_zero(serve, workspace):
    pages_seen: list[int] = []

    def _route(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["Page"]
        pages_seen.append(page)
        rows = {1: [message(2)], 2: [message(1)]}.get(page, [])
        return httpx.Response(200, json={"Result": rows, "TotalMatching": "junk"})

    serve({"POST /api/messagesearchmobile/": _route})
    assert ss.main(["--once"]) == 0
    assert pages_seen == [1, 2, 3]
    assert len(records(workspace["jsonl"])) == 2


def test_second_run_without_changes_emits_nothing(serve, workspace):
    serve(gateway())
    assert ss.main(["--once"]) == 0
    first = len(records(workspace["jsonl"]))
    age_state(workspace)
    assert ss.main(["--once"]) == 0
    assert len(records(workspace["jsonl"])) == first


def test_a_changed_body_re_emits_as_message_update(serve, workspace):
    serve(gateway())
    ss.main(["--once"])
    age_state(workspace)
    serve(gateway(pages={1: [message(1, body="<p>Endret tekst</p>")]}))
    ss.main(["--once"])
    kinds = [r["kind"] for r in records(workspace["jsonl"])]
    assert kinds == ["message", "message_update"]
    assert records(workspace["jsonl"])[-1]["data"]["body"] == "Endret tekst"


def test_a_new_reply_in_threads_is_an_update(serve, workspace):
    serve(gateway())
    ss.main(["--once"])
    age_state(workspace)
    reply = {"From": "Forelder", "Timestamp": "2026-10-13T07:00:00+02:00",
             "Body": "<p>Takk for beskjed</p>", "Attachments": []}
    serve(gateway(pages={1: [message(1, threads=[reply])]}))
    ss.main(["--once"])
    recs = records(workspace["jsonl"])
    assert [r["kind"] for r in recs] == ["message", "message_update"]
    assert recs[-1]["data"]["threads"][0]["body"] == "Takk for beskjed"


def test_feedback_counters_and_read_receipts_do_not_trigger_updates(serve, workspace):
    """TotalYes/No/NotAnswered move when OTHER parents answer, IsMessageSeen
    when we read in the app — none of that is the school acting."""
    serve(gateway())
    ss.main(["--once"])
    age_state(workspace)
    moved = message(1, IsMessageSeen=True, IsFavourite=True,
                    TotalYes=11, TotalNo=2, TotalNotAnswered=0,
                    IsFeedBackReadConfirmation=True)
    serve(gateway(pages={1: [moved]}))
    ss.main(["--once"])
    assert [r["kind"] for r in records(workspace["jsonl"])] == ["message"]


def test_message_signature_is_stable_against_volatile_fields():
    a = message(1)
    b = message(1, IsMessageSeen=True, IsFavourite=True, TotalYes=9,
                TotalNo=4, TotalNotAnswered=0, AuthorUserCategroy="Ansatt")
    assert ss.message_signature(a) == ss.message_signature(b)
    assert ss.message_signature(message(1, header="Endret")) != ss.message_signature(a)
    assert ss.message_signature(message(1, attachments=[ATT_PATH])) != \
        ss.message_signature(a)


def test_the_sync_never_touches_the_triage_cursor(serve, workspace):
    """``cursor`` is triage-owned; only ``skolemeldingshow --bump`` writes it."""
    workspace["state"].write_text(json.dumps({"cursor": "2026-10-01T00:00:00Z"}))
    serve(gateway())
    assert ss.main(["--once"]) == 0
    state = json.loads(workspace["state"].read_text())
    assert state["cursor"] == "2026-10-01T00:00:00Z"


# --- Attachments ------------------------------------------------------------


def test_attachments_are_downloaded_and_the_local_path_recorded(serve, workspace):
    def _file(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4",
                              headers={"content-type": "application/pdf"})

    serve({**gateway(pages={1: [message(1, attachments=[ATT_PATH])]}),
           "GET " + ATT_PATH: _file})
    assert ss.main(["--once"]) == 0
    att = records(workspace["jsonl"])[0]["data"]["attachments"][0]
    assert att["name"] == "skjema.pdf"
    assert att["mimeType"] == "application/pdf"
    assert att["source"] == ATT_PATH
    assert Path(att["path"]).exists()
    assert Path(att["path"]).read_bytes() == b"%PDF-1.4"


def test_a_failed_attachment_is_retried_and_not_marked_seen(serve, workspace,
                                                            monkeypatch):
    serve(gateway(pages={1: [message(1, attachments=[ATT_PATH])]}))
    monkeypatch.setattr(SkolemeldingApi, "attachment",
                        lambda self, path, **kw: (_ for _ in ()).throw(OSError("boom")))
    assert ss.main(["--once"]) == 0
    state = json.loads(workspace["state"].read_text())
    assert "1" not in state["messages"], "a message with a missing file is not fully seen"
    assert state["attachment_retries"]["1"] == 1
    rec = records(workspace["jsonl"])[0]
    assert "attachment download failed" in rec["data"]["note"]


def test_attachment_auth_expiry_aborts_into_auth_required(serve, workspace, monkeypatch):
    """attachment() is Bearer-authed and raises AuthExpiredError on a dead
    login (terminal 401, or an HTML login page instead of the file). That is
    not an attachment failure: the run must take the auth-required path —
    the message is NOT marked seen and nothing is recorded."""
    from scripts.skolemelding_api import AuthExpiredError

    serve(gateway(pages={1: [message(1, attachments=[ATT_PATH])]}))

    def _dead(self: Any, path: str, **kw: Any) -> tuple[bytes, str]:
        raise AuthExpiredError("401 even after refreshing the token")

    monkeypatch.setattr(SkolemeldingApi, "attachment", _dead)
    assert ss.main(["--once"]) == 0, "a dead login must not break a sjekk"
    state = json.loads(workspace["state"].read_text())
    assert state["auth_required"] is True
    assert "1" not in state.get("messages", {}), "the aborted message stays unseen"
    assert "1" not in state.get("attachment_retries", {}), \
        "auth expiry is not a retry-budget attachment failure"
    assert records(workspace["jsonl"]) == []


def test_attachment_retries_are_bounded_then_given_up(serve, workspace, monkeypatch,
                                                      capsys):
    serve(gateway(pages={1: [message(1, attachments=[ATT_PATH])]}))
    monkeypatch.setattr(SkolemeldingApi, "attachment",
                        lambda self, path, **kw: (_ for _ in ()).throw(OSError("boom")))
    for _ in range(ss.ATTACH_RETRY_BUDGET + 1):
        ss.main(["--once", "--force"])
    state = json.loads(workspace["state"].read_text())
    assert "1" in state["messages"], "after the budget the message must stop looping"
    assert "1" not in state["attachment_retries"]
    assert "giving up" in capsys.readouterr().err


def test_successful_retry_closes_the_loop(serve, workspace, monkeypatch):
    serve(gateway(pages={1: [message(1, attachments=[ATT_PATH])]}))
    fails = {"n": 0}

    def _flaky(self: Any, path: str, **kw: Any) -> tuple[bytes, str]:
        fails["n"] += 1
        if fails["n"] == 1:
            raise OSError("transient")
        return b"%PDF-1.4", "application/pdf"

    monkeypatch.setattr(SkolemeldingApi, "attachment", _flaky)
    ss.main(["--once"])
    ss.main(["--once", "--force"])
    state = json.loads(workspace["state"].read_text())
    assert "1" in state["messages"]
    assert state["attachment_retries"] == {}
    last = records(workspace["jsonl"])[-1]
    assert Path(last["data"]["attachments"][0]["path"]).exists()


# --- Auth and politeness ----------------------------------------------------


def test_expired_login_exits_zero_and_flags_the_state(serve, workspace, capsys):
    """A dead refresh token (400 invalid_grant) is routine, not a crash."""
    serve({**gateway(), "POST /token": Resp(400, {"error": "invalid_grant"})},
          tokens=login_payload(expires_in=-10))
    assert ss.main(["--once"]) == 0, "a dead login must not break a sjekk"
    err = capsys.readouterr().err
    assert "skolemelding auth expired — run skolemelding-login" in err
    state = json.loads(workspace["state"].read_text())
    assert state["auth_required"] is True
    assert "last_successful_run" not in state, "a failed run must not move the cursor"
    assert records(workspace["jsonl"]) == []


def test_auth_expiry_mid_run_does_not_persist_partial_state(serve, workspace, capsys):
    """AuthExpiredError on page 2: page 1 already marked a new message seen
    in the in-memory state, but its record was never emitted — persisting
    that mark would skip the message forever. The handler must reload state
    from disk and only set auth_required."""
    known = message(1)
    old_run = "2026-10-01T06:00:00Z"
    workspace["state"].write_text(json.dumps({
        "messages": {"1": {"sig": ss.message_signature(known),
                           "timestamp": known["Timestamp"]}},
        "cursor": "2026-10-01T07:00:00Z",
        "last_successful_run": old_run,
        "auth_required": False,
    }))

    def _paged(request: httpx.Request) -> httpx.Response:
        page = json.loads(request.content)["Page"]
        if page == 1:
            return httpx.Response(
                200, json={"Result": [message(2)], "TotalMatching": 3})
        return httpx.Response(401, json={"message": "expired"})

    serve({"POST /api/messagesearchmobile/": _paged,
           # The forced refresh after the 401 is refused → AuthExpiredError.
           "POST /token": Resp(400, {"error": "invalid_grant"})})
    assert ss.main(["--once"]) == 0
    assert "auth expired" in capsys.readouterr().err
    state = json.loads(workspace["state"].read_text())
    assert state["auth_required"] is True
    assert set(state["messages"]) == {"1"}, \
        "page 1's in-memory seen-mark must not reach disk"
    assert state["cursor"] == "2026-10-01T07:00:00Z", "triage cursor untouched"
    assert state["last_successful_run"] == old_run, "an aborted run is not a success"
    assert records(workspace["jsonl"]) == []


def test_an_api_error_is_loud_and_nonzero(serve, workspace, capsys, monkeypatch):
    serve({"POST /api/messagesearchmobile/": Resp(500, {"error": "boom"})})
    # 5xx retries with backoff; skip the actual sleeping.
    monkeypatch.setattr("time.sleep", lambda *_: None)
    assert ss.main(["--once"]) == 1
    assert "skolemelding sync failed" in capsys.readouterr().err
    assert not workspace["state"].exists()


def test_the_poll_floor_gently_skips_a_second_run(serve, workspace, capsys):
    serve(gateway())
    assert ss.main(["--once"]) == 0
    assert "new records" in capsys.readouterr().err
    assert ss.main(["--once"]) == 0
    second = capsys.readouterr()
    assert "nothing to do" in second.out + second.err


def test_force_ignores_the_floor(serve, workspace, capsys):
    serve(gateway())
    ss.main(["--once"])
    capsys.readouterr()
    ss.main(["--once", "--force"])
    assert "new records" in capsys.readouterr().err


def test_dry_run_touches_nothing(serve, workspace):
    serve(gateway())
    assert ss.main(["--once", "--dry-run"]) == 0
    assert records(workspace["jsonl"]) == []
    assert not workspace["state"].exists()


class _AttachmentApi(SkolemeldingApi):
    def __init__(self):
        pass

    def attachment(self, path: str, *, max_bytes: int = 1) -> tuple[bytes, str]:
        return b"new", "application/pdf"


@pytest.mark.parametrize("mid", ["../outside", "/absolute", ".", ".."])
def test_attachment_container_ids_are_rejected_before_download(workspace, mid):
    failed, paths = ss._download_attachments(_AttachmentApi(), mid, [ATT_PATH])
    assert failed == ["skjema.pdf"]
    assert paths == {}
    assert not (workspace["root"] / "outside").exists()


def test_attachment_container_symlink_is_not_followed(workspace):
    store = workspace["jsonl"] / "attachments"
    outside = workspace["root"] / "outside"
    store.mkdir()
    outside.mkdir()
    (store / "safe").symlink_to(outside, target_is_directory=True)
    failed, paths = ss._download_attachments(_AttachmentApi(), "safe", [ATT_PATH])
    assert failed == ["skjema.pdf"]
    assert paths == {}
    assert not (outside / "skjema.pdf").exists()


def test_attachment_target_symlink_is_not_overwritten(workspace):
    store = workspace["jsonl"] / "attachments"
    outside = workspace["root"] / "outside"
    (store / "safe").mkdir(parents=True)
    outside.write_bytes(b"original")
    (store / "safe" / "skjema.pdf").symlink_to(outside)
    failed, paths = ss._download_attachments(_AttachmentApi(), "safe", [ATT_PATH])
    assert failed == ["skjema.pdf"]
    assert paths == {}
    assert outside.read_bytes() == b"original"


# --- Small helpers ----------------------------------------------------------


def test_safe_filename_defuses_a_traversal():
    assert "/" not in ss.safe_filename("../../etc/passwd", 1)
    assert ss.safe_filename("Uke 42.pdf", 0) == "Uke_42.pdf"
    assert ss.safe_filename("###", 3) == "3_attachment"


def test_attachment_name_from_portal_path():
    assert ss.attachment_name("/globalassets/attachments/x/Uke 42.pdf") == "Uke 42.pdf"
    assert ss.attachment_name("") == "attachment"


def test_recently_ran_reads_last_successful_run_leniently():
    assert ss.recently_ran({}) is False
    assert ss.recently_ran({"last_successful_run": "not a date"}) is False
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ss.recently_ran({"last_successful_run": fresh}) is True
    old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ss.recently_ran({"last_successful_run": old}) is False
