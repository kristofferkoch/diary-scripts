"""Tests for vigilo_sync.py. Run with `uv run pytest scripts/`.

Regression focus: reply re-emission (upstream's threadUid-set model never
forwards an answer), attachment-failure requeue (upstream marks seen anyway
and loses the file), and the exit-0 path when the login simply expires.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import httpx

from scripts import vigilo_sync as vs
from scripts.vigilo_api import ConfigError, TokenStore, VigiloApi

from .test_vigilo_api import (
    CHILD_ID,
    Resp,
    make_jwt,
    token_pair,
    transport,
)

THREAD_UID = "6f6f6f6f-1111-2222-3333-444444444444"
UNIT_ID = "99999999-8888-7777-6666-555555555555"


def thread_item(*, time: str = "2026-10-09T07:30:00Z", count: int = 1) -> dict[str, Any]:
    return {
        "threadUid": THREAD_UID,
        "title": "Sjakkurs onsdag 15-16",
        "organizationalUnit": {"id": UNIT_ID, "name": "AKS Eksempelby"},
        "organizationalUnitId": UNIT_ID,
        "timeCreated": time,
        "messageCount": count,
    }


def thread_detail(text: str = "Husk å svare", *, atts: list[dict] | None = None) -> dict:
    return {
        "messages": [
            {
                "body": text,
                "timeCreated": "2026-10-09T07:30:00Z",
                "sender": {"firstName": "AKS", "lastName": "Eksempelby"},
                "attachments": atts or [],
            }
        ]
    }


SAS_URL = "https://vigiloblob.blob.core.windows.net/c/skjema.pdf?sv=2024&sig=secret"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the sink at a scratch workspace (the real one is the diary repo)."""
    root = tmp_path / "memory"
    (root / "vigilo").mkdir(parents=True)
    monkeypatch.setattr(vs, "STATE_PATH", root / "vigilo-state.json")
    monkeypatch.setattr(vs, "OUT_DIR", root / "vigilo")
    monkeypatch.setattr(vs, "ATTACH_DIR", root / "vigilo" / "attachments")
    monkeypatch.setenv("VIGILO_TOKEN_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("VIGILO_CLIENT_ID", "id.ANDROID")
    monkeypatch.setenv("VIGILO_CLIENT_SECRET", "secret")
    return {"root": root, "jsonl": root / "vigilo", "state": root / "vigilo-state.json"}


@pytest.fixture
def serve(workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    """Install a stubbed gateway behind the real VigiloApi object graph.

    Everything below ``make_api`` is the production code path — including the
    401-retry and refresh logic — only the wire is faked.
    """

    def _serve(routes: dict[str, Any], *, tokens: dict[str, Any] | None = None) -> None:
        t = transport({"POST /connect/token": Resp(400, {"error": "invalid_grant"}), **routes})
        TokenStore(Path(os.environ["VIGILO_TOKEN_FILE"]), transport=t).save(
            tokens if tokens is not None else token_pair()
        )
        monkeypatch.setattr(vs, "make_api", lambda: VigiloApi(transport=t))

    return _serve


def records(jsonl_dir: Path) -> list[dict[str, Any]]:
    out = []
    for fp in sorted(jsonl_dir.glob("*.jsonl")):
        out += [json.loads(line) for line in fp.read_text().splitlines() if line.strip()]
    return out


CHILDREN: list[dict[str, Any]] = [{"childId": CHILD_ID, "firstName": "Jente", "lastName": "Prøve"}]


def gateway(*, detail: dict | None = None, threads: list | None = None) -> dict[str, Any]:
    return {
        "GET /api/children": CHILDREN,
        "GET /api/message-threads": {"messageThreads": threads if threads is not None
                                     else [thread_item()]},
        "GET /api/messages/threads/" + THREAD_UID: detail or thread_detail(),
    }


# --- Messages ---------------------------------------------------------------


def test_first_run_emits_a_message_and_records_the_thread(serve, workspace):
    serve(gateway())
    assert vs.main(["--once"]) == 0
    recs = records(workspace["jsonl"])
    assert [r["kind"] for r in recs] == ["message"]
    data = recs[0]["data"]
    assert data["threadUid"] == THREAD_UID
    assert data["unit"] == "AKS Eksempelby"
    assert data["title"] == "Sjakkurs onsdag 15-16"
    assert data["messages"][0]["sender"] == "AKS Eksempelby"
    assert data["messages"][0]["body"] == "Husk å svare"
    state = json.loads(workspace["state"].read_text())
    assert state["threads"][THREAD_UID]["message_count"] == 1
    assert state["auth_required"] is False


def test_second_run_without_changes_emits_nothing(serve, workspace):
    serve(gateway())
    vs.main(["--once"])
    first = len(records(workspace["jsonl"]))
    # Past the poll floor, otherwise the run is skipped for being too soon.
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))
    assert vs.main(["--once"]) == 0
    assert len(records(workspace["jsonl"])) == first


def test_a_reply_re_emits_the_thread_as_message_update(serve, workspace):
    """The bug we must not copy: uid-set state never forwards a reply."""
    serve(gateway())
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))

    serve(gateway(threads=[thread_item(count=2, time="2026-10-09T07:30:00Z")],
                  detail=thread_detail("Takk for svar!")))
    vs.main(["--once"])
    kinds = [r["kind"] for r in records(workspace["jsonl"])]
    assert kinds == ["message", "message_update"], "AKS replies must surface"


def test_a_thread_without_any_change_signal_is_emitted_once(serve, workspace):
    blind = {"threadUid": THREAD_UID, "title": "Uten tidsstempel"}
    serve(gateway(threads=[blind]))
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))
    vs.main(["--once"])
    assert [r["kind"] for r in records(workspace["jsonl"])] == ["message"]


def test_unit_label_from_config_beats_the_api_name(monkeypatch: pytest.MonkeyPatch):
    """Anonymised labels live in the private config; the API name is the fallback."""

    def _cfg(key: str, default: Any) -> Any:
        return {UNIT_ID: "Aksjer på Eksempelby"} if key == "vigilo.unit_labels" else default

    monkeypatch.setattr(vs, "cfg", _cfg)
    assert vs.unit_label(UNIT_ID, "AKS Eksempelby") == "Aksjer på Eksempelby"
    assert vs.unit_label("annen-enhet", "AKS Eksempelby") == "AKS Eksempelby"


# --- Attachments ------------------------------------------------------------


def test_attachments_are_downloaded_and_the_local_path_recorded(serve, workspace,
                                                                monkeypatch):
    detail = thread_detail(atts=[{"name": "skjema.pdf", "mimeType": "application/pdf",
                                  "url": SAS_URL}])
    serve(gateway(detail=detail))
    monkeypatch.setattr(vs.VigiloApi, "download_attachment",
                        staticmethod(lambda url, **kw: b"%PDF-1.4"))
    assert vs.main(["--once"]) == 0
    rec = records(workspace["jsonl"])[0]
    att = rec["data"]["messages"][0]["attachments"][0]
    assert att["name"] == "skjema.pdf"
    assert Path(att["path"]).exists()
    assert Path(att["path"]).read_bytes() == b"%PDF-1.4"


def test_the_sas_url_never_reaches_the_jsonl(serve, workspace, monkeypatch):
    """A pre-signed URL in a committed file is a usable link to someone's documents."""
    detail = thread_detail(atts=[{"name": "skjema.pdf", "mimeType": "application/pdf",
                                  "url": SAS_URL}])
    serve(gateway(detail=detail))
    monkeypatch.setattr(vs.VigiloApi, "download_attachment",
                        staticmethod(lambda url, **kw: b"%PDF-1.4"))
    vs.main(["--once"])
    blob = "".join(fp.read_text() for fp in workspace["jsonl"].glob("*.jsonl"))
    assert "core.windows.net" not in blob
    assert "sig=secret" not in blob


def test_a_failed_attachment_is_retried_and_not_marked_seen(serve, workspace, monkeypatch):
    detail = thread_detail(atts=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                  "url": SAS_URL}])
    serve(gateway(detail=detail))
    monkeypatch.setattr(vs.VigiloApi, "download_attachment",
                        staticmethod(lambda url, **kw: (_ for _ in ()).throw(OSError("boom"))))
    assert vs.main(["--once"]) == 0
    state = json.loads(workspace["state"].read_text())
    assert THREAD_UID not in state["threads"], "a thread with a missing file is not fully seen"
    assert state["attachment_retries"][THREAD_UID] == 1
    rec = records(workspace["jsonl"])[0]
    assert "attachment download failed" in rec["data"]["note"]


def test_attachment_retries_are_bounded_then_given_up(serve, workspace, monkeypatch,
                                                      capsys):
    detail = thread_detail(atts=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                  "url": SAS_URL}])
    serve(gateway(detail=detail))
    monkeypatch.setattr(vs.VigiloApi, "download_attachment",
                        staticmethod(lambda url, **kw: (_ for _ in ()).throw(OSError("boom"))))
    for _ in range(vs.ATTACH_RETRY_BUDGET + 1):
        vs.main(["--once", "--force"])
    state = json.loads(workspace["state"].read_text())
    assert THREAD_UID in state["threads"], "after the budget the thread must stop looping"
    assert THREAD_UID not in state["attachment_retries"]
    assert "giving up" in capsys.readouterr().err


def test_successful_retry_closes_the_loop(serve, workspace, monkeypatch):
    detail = thread_detail(atts=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                  "url": SAS_URL}])
    serve(gateway(detail=detail))
    fails = {"n": 0}

    def _flaky(url: str, **kw: Any) -> bytes:
        fails["n"] += 1
        if fails["n"] == 1:
            raise OSError("transient")
        return b"%PDF-1.4"

    monkeypatch.setattr(vs.VigiloApi, "download_attachment", staticmethod(_flaky))
    vs.main(["--once"])
    vs.main(["--once", "--force"])
    state = json.loads(workspace["state"].read_text())
    assert THREAD_UID in state["threads"]
    assert state["attachment_retries"] == {}
    last = records(workspace["jsonl"])[-1]
    assert Path(last["data"]["messages"][0]["attachments"][0]["path"]).exists()


# --- Auth and politeness ----------------------------------------------------


def test_expired_login_exits_zero_and_flags_the_state(serve, workspace, capsys):
    serve(gateway(), tokens={"access_token": make_jwt(), "refresh_token": "dead",
                             "expires_in": 1,
                             "obtained_at": (datetime.now() - timedelta(hours=5)).timestamp()})
    assert vs.main(["--once"]) == 0, "a dead login must not break a sjekk"
    err = capsys.readouterr().err
    assert "vigilo auth expired — run vigilo-login" in err
    state = json.loads(workspace["state"].read_text())
    assert state["auth_required"] is True
    assert "last_successful_run" not in state, "a failed run must not move the cursor"
    assert records(workspace["jsonl"]) == []


def test_bad_credentials_are_loud_and_not_reported_as_expired(serve, workspace,
                                                              monkeypatch, capsys):
    """Missing/bad client credentials only surface on a token call — with a
    fresh access token they are never read at all. So stage the failure where
    it actually happens: an EXPIRED access token forces a refresh, and the
    refresh needs the credentials. That must exit loudly non-zero (a config
    error is our bug to fix, not a login to redo) and must NOT set
    ``auth_required`` — that flag is reserved for ``invalid_grant``."""
    serve(gateway(), tokens={"access_token": make_jwt(), "refresh_token": "rt-1",
                             "expires_in": 1,
                             "obtained_at": (datetime.now() - timedelta(hours=5)).timestamp()})
    monkeypatch.delenv("VIGILO_CLIENT_ID")
    monkeypatch.delenv("VIGILO_CLIENT_SECRET")
    import scripts.vigilo_api as va

    def _nope(cmd: str, what: str) -> str:
        raise va.ConfigError(f"no {what}")

    monkeypatch.setattr(va, "_secret", _nope)
    assert vs.main(["--once"]) == 2
    err = capsys.readouterr().err
    assert "config error" in err
    assert "auth expired" not in err
    assert not workspace["state"].exists(), "a config error must not touch state"


def test_the_poll_floor_gently_skips_a_second_run(serve, workspace, capsys):
    serve(gateway())
    assert vs.main(["--once"]) == 0
    out = capsys.readouterr()
    assert "new records" in out.err
    assert vs.main(["--once"]) == 0
    second = capsys.readouterr()
    assert "nothing to do" in second.out + second.err


def test_force_ignores_the_floor(serve, workspace, capsys):
    serve(gateway())
    vs.main(["--once"])
    capsys.readouterr()
    vs.main(["--once", "--force"])
    assert "new records" in capsys.readouterr().err


def test_page_size_ceiling_is_reported(serve, workspace, capsys):
    many = []
    routes: dict[str, Any] = {}
    for i in range(vs.PAGE_SIZE):
        item = thread_item()
        item["threadUid"] = f"tr-{i}"
        many.append(item)
        routes[f"GET /api/messages/threads/tr-{i}"] = thread_detail()
    serve({**gateway(), "GET /api/message-threads": {"messageThreads": many}, **routes})
    vs.main(["--once"])
    assert "pageSize ceiling" in capsys.readouterr().err


# --- Web gateway kinds ------------------------------------------------------


def test_web_kinds_are_recorded_when_the_token_works_there(serve, workspace):
    routes = {
        "GET /api/children/my": [{"id": CHILD_ID, "organizationalUnits": [
            {"id": UNIT_ID, "name": "AKS Eksempelby"}]}],
        "GET /api/news-feed": [{"id": "post-1", "title": "Ukeplan uke 42",
                                "created": "2026-10-10T08:00:00Z",
                                "attachments": [{"id": "a1", "name": "uke42.pdf",
                                                 "mimeType": "application/pdf"}]}],
        "GET /api/consent-forms": [{"id": "c-1", "title": "Samtykke tur",
                                    "status": "pending", "deadline": "2026-10-20"}],
        "GET /api/consent-forms/counts": {"totalConsentFormsCount": 1,
                                          "unreadConsentFormsCount": 1},
        "GET /api/check-in-notes": [{"id": "n-1", "noteText": "Hentes av bestemor",
                                     "toDate": "2026-12-24T00:00:00"}],
    }
    serve({**gateway(), **routes})
    assert vs.main(["--once"]) == 0
    by_kind = {r["kind"]: r for r in records(workspace["jsonl"])}
    assert {"message", "news", "consent"} <= set(by_kind)
    assert by_kind["news"]["data"]["attachments"][0]["name"] == "uke42.pdf"
    assert by_kind["consent"]["data"]["deadline"] == "2026-10-20"
    assert json.loads(workspace["state"].read_text())["web_available"] is True


def test_a_refused_web_gateway_degrades_to_app_only(serve, workspace, capsys):
    serve({**gateway(),
           "GET /api/children/my": Resp(401, {"message": "denied"}),
           "GET /api/news-feed": Resp(401, {"message": "denied"}),
           "GET /api/consent-forms": Resp(401, {"message": "denied"})})
    assert vs.main(["--once"]) == 0
    err = capsys.readouterr().err
    assert "refused the app token" in err
    state = json.loads(workspace["state"].read_text())
    assert state["web_available"] is False
    kinds = {r["kind"] for r in records(workspace["jsonl"])}
    assert kinds == {"message"}, "messages must survive a web-gateway refusal"


def test_web_kinds_stay_skipped_once_known_unavailable(serve, workspace, capsys):
    state_path = workspace["state"]
    serve({**gateway(), "GET /api/children/my": Resp(401, {"message": "denied"})})
    vs.main(["--once"])
    state = json.loads(state_path.read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(json.dumps(state))
    capsys.readouterr()
    vs.main(["--once"])
    assert "skipping web-gateway kinds" in capsys.readouterr().err


def test_news_that_already_rolled_over_is_not_repeated(serve, workspace):
    routes = {
        "GET /api/children/my": [{"id": CHILD_ID,
                                  "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]}],
        "GET /api/news-feed": [{"id": "post-1", "title": "Ukeplan", "created": "2026-10-10"}],
        "GET /api/consent-forms": [],
        "GET /api/consent-forms/counts": {"totalConsentFormsCount": 0},
        "GET /api/check-in-notes": [],
    }
    serve({**gateway(), **routes})
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))
    vs.main(["--once"])
    assert len([r for r in records(workspace["jsonl"]) if r["kind"] == "news"]) == 1


def web_routes(**overrides: Any) -> dict[str, Any]:
    routes: dict[str, Any] = {
        "GET /api/children/my": [{"id": CHILD_ID,
                                  "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]}],
        "GET /api/news-feed": [],
        "GET /api/consent-forms": [],
        "GET /api/consent-forms/counts": {"totalConsentFormsCount": 0},
        "GET /api/check-in-notes": [],
    }
    routes.update(overrides)
    return routes


def test_consent_404_is_empty_not_fatal(serve, workspace):
    """Live 2026-10-11: no forms → 404 with an empty body, and that 404 used
    to kill the whole run before any record or state was written."""
    serve({**gateway(), **web_routes(
        **{"GET /api/consent-forms": lambda r: httpx.Response(404)})})
    assert vs.main(["--once"]) == 0
    kinds = {r["kind"] for r in records(workspace["jsonl"])}
    assert kinds == {"message"}, kinds
    assert json.loads(workspace["state"].read_text())["last_successful_run"]


def test_a_dying_consent_endpoint_does_not_lose_the_run(serve, workspace, capsys):
    serve({**gateway(), **web_routes(
        **{"GET /api/consent-forms": Resp(400, {"error": "bogus"}),
           "GET /api/news-feed": [{"id": "post-1", "title": "Ukeplan",
                                   "created": "2026-10-10"}]})})
    assert vs.main(["--once"]) == 0
    assert "consent fetch failed" in capsys.readouterr().err
    kinds = {r["kind"] for r in records(workspace["jsonl"])}
    assert kinds == {"message", "news"}, kinds
    assert json.loads(workspace["state"].read_text())["last_successful_run"]


def test_counts_vs_empty_list_mismatch_is_loud(serve, workspace, capsys):
    serve({**gateway(), **web_routes(
        **{"GET /api/consent-forms": lambda r: httpx.Response(404),
           "GET /api/consent-forms/counts": {"totalConsentFormsCount": 2}})})
    assert vs.main(["--once"]) == 0
    assert "counts say 2 form(s) exist" in capsys.readouterr().err


def test_auth_expiry_mid_web_kinds_still_sets_auth_required(serve, workspace, capsys):
    """Per-kind isolation must not swallow AuthExpiredError: the serve
    fixture's token endpoint answers invalid_grant, so a 401 here means the
    refresh token is dead — main() owns that, not the kind guard."""
    serve({**gateway(), **web_routes(
        **{"GET /api/news-feed": Resp(401, {"message": "expired"})})})
    assert vs.main(["--once"]) == 0
    assert "auth expired" in capsys.readouterr().err
    state = json.loads(workspace["state"].read_text())
    assert state["auth_required"] is True
    assert "last_successful_run" not in state


def test_dry_run_touches_nothing(serve, workspace):
    serve(gateway())
    assert vs.main(["--once", "--dry-run"]) == 0
    assert records(workspace["jsonl"]) == []
    assert not workspace["state"].exists()


# --- Hentemeldinger (check_out_note) ----------------------------------------

NOTE_ID = "44444444-5555-6666-7777-888888888888"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
# The day the endpoint was probed live. Every hentemelding below is dated
# against *this* day and the clock is pinned to it, so "2026-12-24" stays
# exactly 71 days away whether pytest runs in October or next July — the
# expiry arithmetic is what is under test, not the calendar of the machine
# running it. (Only the new kind reads `today()`; messages and news keep the
# real clock, they only ever compare timestamps they were handed.)
TODAY = date(2026, 10, 14)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr(vs, "today", lambda: TODAY)
    return TODAY


def note_item(**over: Any) -> dict[str, Any]:
    """One hentemelding in the shape the endpoint answers (live, 2026-10-14)."""
    item: dict[str, Any] = {
        "id": NOTE_ID,
        "category": "afterSchoolCheckOut",
        "type": {"id": "t-1", "name": "Hentes av:"},
        "childPersonId": CHILD_ID,
        "fromDate": "2026-08-17T00:00:00",
        "toDate": "2026-12-24T00:00:00",
        "recurrencePattern": {"recurrenceType": "weekly", "interval": 1,
                              "daysOfWeek": list(WEEKDAYS)},
        "isRecurring": True,
        "isEnabled": True,
        "timeOfDay": "15:00:00",
        "noteText": "Går hjem selv etter kl. 14.",
        "registeredTime": "2026-08-17T07:07:38Z",
        "lastUpdatedTime": "2026-08-17T07:07:38Z",
        "organizationalUnitId": UNIT_ID,
        # The rule expanded to one entry per day — 93 of them for a school
        # term. Nothing here may reach the JSONL.
        "dateOccurrences": [{"time": f"2026-10-{d:02d}T15:00:00"} for d in range(1, 29)],
    }
    item.update(over)
    return item


def _ran_cleanly(workspace: dict[str, Path]) -> None:
    """Leave state as a finished run would, so the next --once passes the floor."""
    state = json.loads(workspace["state"].read_text())
    state["last_successful_run"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace["state"].write_text(json.dumps(state))


def _notes(workspace: dict[str, Path]) -> list[dict[str, Any]]:
    return [r for r in records(workspace["jsonl"]) if r["kind"] == "check_out_note"]


def test_a_standing_hentemelding_is_recorded_without_the_occurrence_blob(serve, workspace):
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    assert vs.main(["--once"]) == 0
    recs = _notes(workspace)
    assert len(recs) == 1
    data = recs[0]["data"]
    assert data["noteId"] == NOTE_ID
    assert data["unit"] == "AKS", "the unit label comes from /api/children/my"
    assert data["days"] == "man,tir,ons,tor,fre"
    assert data["timeOfDay"] == "15:00:00"
    assert data["fromDate"] == "2026-08-17"
    assert data["toDate"] == "2026-12-24"
    assert data["change"] == "ny"
    assert data["recurrenceType"] == "weekly"
    # 14.10.2026 → 24.12.2026. Pinned clock, so this stays true in July.
    assert data["daysLeft"] == (date(2026, 12, 24) - TODAY).days
    assert "dateOccurrences" not in data


def test_a_broken_unit_labels_config_is_loud_and_writes_nothing(serve, workspace, capsys,
                                                                monkeypatch):
    """`unit_labels` feeds every kind's payload, so a broken one is a *config*
    failure, not a kind failure: exit 2, nothing half-written, no cursor moved.
    Loud is the point — silently falling back to raw AKS names would be the
    quieter bug. Shared by all kinds since before check_out_note existed (a
    review disputed this, so it is pinned here rather than argued)."""
    def boom(*args: Any, **kwargs: Any) -> str:
        raise ConfigError("vigilo.unit_labels is not a mapping")

    monkeypatch.setattr(vs, "unit_label", boom)
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    assert vs.main(["--once"]) == 2
    assert "config error" in capsys.readouterr().err
    assert not records(workspace["jsonl"])
    state = json.loads(workspace["state"].read_text()) if workspace["state"].exists() else {}
    assert not state.get("last_successful_run")


def test_an_unchanged_hentemelding_is_not_repeated(serve, workspace):
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    vs.main(["--once"])
    assert len(_notes(workspace)) == 1


def test_an_edited_hentemelding_comes_back(serve, workspace):
    """A new hentetid is a changed pickup plan — the whole reason for the kind."""
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    later = note_item(noteText="Går hjem selv etter kl. 16.",
                      lastUpdatedTime="2026-10-14T09:12:00Z")
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [later]})})
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny", "endret"]
    assert recs[1]["data"]["title"] == "Går hjem selv etter kl. 16."


def test_a_hentemelding_near_expiry_is_resent_on_a_drumbeat(serve, workspace):
    """toDate is a hard stop; one warning at the start of the horizon would
    have scrolled out of the sjekk long before the date did."""
    soon = (TODAY + timedelta(days=5)).isoformat()
    routes = {**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item(toDate=soon)]})}
    serve(routes)
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    assert state["check_out_notes"][NOTE_ID]["sig"]
    # A week of sjekker later: same note, same signature — but now close to
    # expiring, so it has to come back around.
    state["check_out_notes"][NOTE_ID]["emitted"] = "2026-10-07T09:00:00Z"
    workspace["state"].write_text(json.dumps(state))
    _ran_cleanly(workspace)
    serve(routes)
    vs.main(["--once"])
    changes = [r["data"]["change"] for r in _notes(workspace)]
    assert changes[0] == "ny"
    assert "utløper om 5 dager" in changes[-1], changes
    _ran_cleanly(workspace)
    vs.main(["--once"])
    assert len(_notes(workspace)) == len(changes), "no double reminder inside the gap"


def test_an_expired_note_still_returned_is_not_said_to_expire_in_minus_three_days(
    serve, workspace,
):
    """The endpoint should stop returning it; if it doesn't, the sentence must
    still be true. (The vanish test uses this same shape, from the other end.)"""
    past = (TODAY - timedelta(days=3)).isoformat()
    routes = {**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item(toDate=past)]})}
    serve(routes)
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    state["check_out_notes"][NOTE_ID]["emitted"] = "2026-10-01T09:00:00Z"
    workspace["state"].write_text(json.dumps(state))
    _ran_cleanly(workspace)
    serve(routes)
    vs.main(["--once"])
    changes = [r["data"]["change"] for r in _notes(workspace)]
    # Exact, not startswith: the doc names this sentence, and "utløpt…" plus
    # "no minus sign" was satisfiable by wording the doc does not describe.
    assert changes[-1] == "utløpt — returneres fortsatt av AKS", changes
    assert _notes(workspace)[-1]["data"]["daysLeft"] == -3


def test_an_open_ended_hentemelding_never_warns(serve, workspace):
    """toDate missing = no expiry at all: quiet forever, not a warning with a
    None in it."""
    routes = {**gateway(), **web_routes(**{
        "GET /api/check-in-notes": [note_item(toDate=None)]})}
    serve(routes)
    vs.main(["--once"])
    state = json.loads(workspace["state"].read_text())
    state["check_out_notes"][NOTE_ID]["emitted"] = "2020-01-01T09:00:00Z"
    workspace["state"].write_text(json.dumps(state))
    _ran_cleanly(workspace)
    serve(routes)
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny"], recs
    assert recs[0]["data"]["daysLeft"] is None
    assert recs[0]["data"]["toDate"] is None


def test_an_unreadable_expiry_is_said_out_loud(serve, workspace, capsys):
    """A date we cannot parse used to mean "no expiry warning, ever, quietly" —
    the one failure mode this kind exists to prevent."""
    routes = {**gateway(), **web_routes(**{
        "GET /api/check-in-notes": [note_item(toDate="24.12.2026")],
        "GET /api/news-feed": [{"id": "post-1", "title": "Ukeplan",
                                "created": "2026-10-10"}]})}
    serve(routes)
    assert vs.main(["--once"]) == 0
    assert "unreadable toDate" in capsys.readouterr().err
    assert len(_notes(workspace)) == 1  # the note itself is still recorded


def test_a_note_switched_off_is_still_a_change_worth_reporting(serve, workspace):
    """isEnabled is part of the signature: a disabled hentemelding means the
    child is *not* walking home, whatever the text still says."""
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes":
                                        [note_item(isEnabled=False)]})})
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny", "endret"]
    assert recs[1]["data"]["enabled"] is False


def test_a_different_recurrence_is_a_new_pickup_plan(serve, workspace):
    """mon–fre → every day is a different permission; the text never changes."""
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    daily = note_item(recurrencePattern={"recurrenceType": "daily", "interval": 1,
                                         "daysOfWeek": None})
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [daily]})})
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny", "endret"]
    assert recs[1]["data"]["recurrenceType"] == "daily"
    assert recs[1]["data"]["recurrenceInterval"] == 1, "in the record, not just the signature"
    assert recs[1]["data"]["days"] == ""


def test_a_note_that_only_changed_its_interval_is_still_a_changed_plan(serve, workspace):
    """Everything in the signature has to be a field the reader can read, or an
    announced change points at nothing. Annenhver uke is a real thing."""
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    every_other_week = note_item(
        recurrencePattern={"recurrenceType": "weekly", "interval": 2,
                           "daysOfWeek": ["wednesday"]})
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [every_other_week]})})
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny", "endret"]
    assert recs[1]["data"]["recurrenceInterval"] == 2
    assert recs[1]["data"]["days"] == "ons"


def test_a_note_with_no_recurrence_pattern_needs_no_days(serve, workspace):
    """A one-off note (isRecurring false, no pattern) is still a hentemelding."""
    serve({**gateway(), **web_routes(**{
        "GET /api/check-in-notes": [note_item(isRecurring=False, recurrencePattern={})]})})
    assert vs.main(["--once"]) == 0
    data = _notes(workspace)[0]["data"]
    assert data["days"] == "" and data["recurring"] is False


def test_junk_items_in_the_answer_are_skipped(serve, workspace):
    """One malformed entry must not cost the whole child's notes."""
    serve({**gateway(), **web_routes(**{
        "GET /api/check-in-notes": ["not-a-dict", None, {"noteText": "uten id"},
                                    note_item()]})})
    assert vs.main(["--once"]) == 0
    assert len(_notes(workspace)) == 1


def test_one_child_failing_does_not_cost_the_other_childs_notes(serve, workspace, capsys):
    """Two children, one bad answer: what was fetched is still written."""
    other = "aaaa1111-2222-3333-4444-555555555555"

    def notes_by_child(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("childPersonId") == other:
            return httpx.Response(400, json={"error": "bogus"})
        return httpx.Response(200, json=[note_item()])

    serve({
        "GET /api/children": CHILDREN + [{"childId": other, "firstName": "Gutt",
                                          "lastName": "Prøve"}],
        "GET /api/message-threads": {"messageThreads": []},
        "GET /api/children/my": [
            {"id": CHILD_ID, "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]},
            {"id": other, "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]},
        ],
        "GET /api/news-feed": [],
        "GET /api/consent-forms": [],
        "GET /api/consent-forms/counts": {"totalConsentFormsCount": 0},
        "GET /api/check-in-notes": notes_by_child,
    })
    assert vs.main(["--once"]) == 0
    assert "hentemelding fetch failed" in capsys.readouterr().err
    recs = _notes(workspace)
    assert [r["data"]["childId"] for r in recs] == [CHILD_ID], recs
    state = json.loads(workspace["state"].read_text())
    assert NOTE_ID in state["check_out_notes"]
    assert state["last_successful_run"], "a partial fetch is still a finished run"


def test_a_note_is_not_declared_gone_for_a_child_the_web_gateway_never_named(
    serve, workspace, capsys,
):
    """/api/check-in-notes answers an unknown child with 200 and an empty list,
    so an empty answer alone must never produce the "permission is gone"
    record — that sentence is only allowed for a child the web gateway has
    named as one of ours. The two gateways hand out child ids from different
    places, and this sync has no proof they are the same string except that
    they have been so far."""
    stranger = "bbbb1111-2222-3333-4444-555555555555"
    # The web gateway lists a child id that is *not* the one the notes were
    # fetched for: run 1 records the note (fetching notes needs no such match),
    # run 2 answers empty — and that is allowed to mean nothing at all.
    web_only = [{"id": stranger, "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]}]
    serve({**gateway(), **web_routes(
        **{"GET /api/children/my": web_only,
           "GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    assert [r["data"]["change"] for r in _notes(workspace)] == ["ny"]
    _ran_cleanly(workspace)
    serve({**gateway(), **web_routes(
        **{"GET /api/children/my": web_only, "GET /api/check-in-notes": []})})
    vs.main(["--once"])
    recs = _notes(workspace)
    assert [r["data"]["change"] for r in recs] == ["ny"], recs
    assert NOTE_ID in json.loads(workspace["state"].read_text())["check_out_notes"]
    assert "did not name this child" in capsys.readouterr().err


def test_a_hentemelding_that_stops_coming_back_is_reported_once(serve, workspace):
    """Expired or deleted, the permission is gone — say it, then stop."""
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": []})})
    vs.main(["--once"])
    _ran_cleanly(workspace)
    vs.main(["--once"])
    recs = _notes(workspace)
    assert len(recs) == 2, recs
    gone = recs[1]["data"]
    assert gone["change"].startswith("returneres ikke lenger")
    assert gone["unit"] == "AKS", "the memo in state exists to name what vanished"
    assert gone["title"] == "Går hjem selv etter kl. 14."
    assert not json.loads(workspace["state"].read_text())["check_out_notes"]


def test_a_child_with_nothing_memoed_is_not_brought_up_in_the_warning(serve, workspace,
                                                                      capsys):
    """The warning is about a note we might be losing. A child the web gateway
    does not name and never had a note here has nothing to lose, and must not
    put a line on every run."""
    stranger = "bbbb1111-2222-3333-4444-555555555555"
    serve({**gateway(), **web_routes(**{
        "GET /api/children/my": [
            {"id": stranger, "organizationalUnits": [{"id": UNIT_ID, "name": "AKS"}]}],
        "GET /api/check-in-notes": [],
    })})
    assert vs.main(["--once"]) == 0
    assert "did not name this child" not in capsys.readouterr().err


def test_a_dying_check_in_notes_endpoint_does_not_lose_the_run(serve, workspace, capsys):
    serve({**gateway(), **web_routes(
        **{"GET /api/check-in-notes": Resp(400, {"error": "bogus"}),
           "GET /api/news-feed": [{"id": "post-1", "title": "Ukeplan",
                                   "created": "2026-10-10"}]})})
    assert vs.main(["--once"]) == 0
    assert "hentemelding fetch failed" in capsys.readouterr().err
    kinds = {r["kind"] for r in records(workspace["jsonl"])}
    assert kinds == {"message", "news"}, kinds
    assert json.loads(workspace["state"].read_text())["last_successful_run"]


def test_a_failed_check_in_notes_fetch_does_not_bury_a_known_note(serve, workspace):
    """A 4xx must not read as "the note is gone" — that line means the pickup
    plan fell over, and it is only allowed after a fetch that worked."""
    routes = {**gateway(), **web_routes(**{"GET /api/check-in-notes": [note_item()]})}
    serve(routes)
    vs.main(["--once"])
    _ran_cleanly(workspace)
    serve({**gateway(), **web_routes(**{"GET /api/check-in-notes": Resp(400, {"error": "x"})})})
    vs.main(["--once"])
    assert len(_notes(workspace)) == 1
    assert NOTE_ID in json.loads(workspace["state"].read_text())["check_out_notes"]


# --- Small helpers ----------------------------------------------------------


def test_safe_filename_defuses_a_traversal():
    assert "/" not in vs.safe_filename("../../etc/passwd", 1)
    assert vs.safe_filename("Uke 42.pdf", 0) == "Uke_42.pdf"
    assert vs.safe_filename("###", 3) == "3_attachment"


def test_recently_ran_reads_the_cursor_leniently():
    assert vs.recently_ran({}) is False
    assert vs.recently_ran({"last_successful_run": "not a date"}) is False
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert vs.recently_ran({"last_successful_run": fresh}) is True
    old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert vs.recently_ran({"last_successful_run": old}) is False
