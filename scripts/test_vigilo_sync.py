"""Tests for vigilo_sync.py. Run with `uv run pytest scripts/`.

Regression focus: reply re-emission (upstream's threadUid-set model never
forwards an answer), attachment-failure requeue (upstream marks seen anyway
and loses the file), and the exit-0 path when the login simply expires.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import httpx

from scripts import vigilo_sync as vs
from scripts.vigilo_api import TokenStore, VigiloApi

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
