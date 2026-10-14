"""Tests for vigiloshown.py. Run with `uv run pytest scripts/`.

The reader is what a sjekk actually looks at, so the field names its headers
render are pinned here: a silent rename upstream shows up as an empty column,
and these tests turn that into a failure instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import vigiloshown as vw

THREAD_UID = "thread-1"
CHILD = "11111111-2222-3333-4444-555555555555"


def message_record(*, fetched: str = "2026-10-11T06:00:00Z", kind: str = "message",
                   body: str = "Husk å svare på samtykket",
                   attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "fetched_at": fetched,
        "data": {
            "threadUid": THREAD_UID,
            "unit": "AKS Eksempelby",
            "title": "Sjakkurs onsdag 15-16",
            "childId": CHILD,
            "messages": [
                {"sender": "AKS Eksempelby", "timeCreated": "2026-10-09T07:30:00Z",
                 "body": body, "attachments": attachments or []}
            ],
            "attachments": attachments or [],
        },
    }


def news_record(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "postId": "post-1", "childId": CHILD, "unit": "AKS Eksempelby",
        "title": "Ukeplan uke 42", "created": "2026-10-10T08:00:00Z",
        "body": "Se vedlegg", "attachments": [
            {"id": "a1", "name": "uke42.pdf", "mimeType": "application/pdf"}]
    }
    data.update(over)
    return {"kind": "news", "fetched_at": "2026-10-11T06:00:00Z", "data": data}


def consent_record(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "consentId": "c-1", "childId": CHILD, "unit": "AKS Eksempelby",
        "title": "Samtykke til tur", "status": "pending", "deadline": "2026-10-20",
        "body": "Vi skal på tur",
    }
    data.update(over)
    return {"kind": "consent", "fetched_at": "2026-10-11T06:00:00Z", "data": data}


def check_out_note_record(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "noteId": "n-1", "childId": CHILD, "unit": "AKS Eksempelby",
        "title": "Går hjem selv etter kl. 14.", "timeOfDay": "15:00:00",
        "days": "man,tir,ons,tor,fre", "recurring": True, "enabled": True,
        "recurrenceType": "weekly", "recurrenceInterval": 1,
        "typeName": "Hentes av:", "category": "afterSchoolCheckOut",
        "fromDate": "2026-08-17", "toDate": "2026-12-24", "daysLeft": 71,
        "created": "2026-08-17T07:07:38Z", "change": "ny",
    }
    data.update(over)
    return {"kind": "check_out_note", "fetched_at": "2026-10-11T06:00:00Z", "data": data}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    jsonl = tmp_path / "vigilo"
    jsonl.mkdir()
    state = tmp_path / "vigilo-state.json"
    monkeypatch.setattr(vw, "STATE_PATH", state)
    monkeypatch.setattr(vw, "JSONL_DIR", jsonl)
    monkeypatch.setattr(vw, "ATTACH_DIR", jsonl / "attachments")
    return {"jsonl": jsonl, "state": state, "root": tmp_path}


def write_records(workspace: dict[str, Path], *recs: dict[str, Any], day: str = "2026-10-11") -> None:
    path = workspace["jsonl"] / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- Headers ----------------------------------------------------------------


def test_message_header_carries_unit_sender_and_subject():
    line = vw.render_header(message_record())
    assert "AKS Eksempelby" in line
    assert "AKS Eksempelby" in line
    assert "Sjakkurs onsdag 15-16" in line
    assert "2026-10-11T06:00:00Z" in line
    assert "[U message" in line


def test_update_header_is_marked_apart_from_new():
    assert "[↩ message_update" in vw.render_header(message_record(kind="message_update"))


def test_header_marks_attachments():
    rec = message_record(attachments=[{"name": "skjema.pdf", "mimeType": "application/pdf"}])
    assert "📎1" in vw.render_header(rec)


def test_news_header_shows_title_and_attachment_count():
    line = vw.render_header(news_record())
    assert "Ukeplan uke 42" in line
    assert "news" in line
    assert "📎1" in line


def test_consent_header_surfaces_status_and_deadline():
    line = vw.render_header(consent_record())
    assert "Samtykke til tur" in line
    assert "status=pending" in line
    assert "deadline=2026-10-20" in line
    assert "consent" in line


def test_check_out_note_header_carries_window_and_validity():
    line = vw.render_header(check_out_note_record())
    assert "Går hjem selv" in line
    assert "15:00" in line, "the time is the pickup plan"
    assert "man,tir,ons,tor,fre" in line
    assert "til 2026-12-24" in line
    assert "[! check_out_note]" in line
    assert "⚠" not in line, "71 days out is nothing to warn about"


def test_a_note_near_expiry_is_flagged_in_the_header():
    line = vw.render_header(check_out_note_record(daysLeft=10,
                                                  change="utløper om 10 dager"))
    assert "⚠" in line
    assert "utløper om 10 dager" in line


def test_check_out_note_full_shows_the_rule_and_the_ids():
    out = "\n".join(vw.render_full(check_out_note_record(), with_attachments=False,
                                   api=None))
    lines = [line.strip() for line in out.splitlines()]
    assert "2026-08-17 → 2026-12-24" in out
    assert "71 dager igjen av gyldigheten" in out
    assert "recurrence=weekly" in out
    assert "×1" not in out, "weekly ×1 reads like it means something beside weekly"
    assert "ny" in lines, "the header's verdict must survive into --full, as its own line"
    assert "childId=" in out and "noteId=n-1" in out


def test_only_a_multiple_recurrence_is_printed_as_a_multiple():
    """Fortnightly is the one case the × buys, and it has to be visible."""
    weekly = "\n".join(vw.render_full(check_out_note_record(recurrenceType="daily"),
                                      with_attachments=False, api=None))
    assert "recurrence=daily" in weekly and "×" not in weekly
    fortnight = "\n".join(vw.render_full(
        check_out_note_record(recurrenceInterval=2), with_attachments=False, api=None))
    assert "recurrence=weekly ×2" in fortnight


def test_a_note_past_its_date_says_so_instead_of_counting_backwards():
    out = "\n".join(vw.render_full(check_out_note_record(daysLeft=-3, change="utløpt"),
                                   with_attachments=False, api=None))
    assert "utløpt for 3 dager siden" in out
    assert "-3 dager igjen" not in out


def test_a_vanished_note_still_renders():
    """The gone-record carries only id, title, unit — it must not crash on the
    fields it no longer has, because that line is the important one."""
    data = {"noteId": "n-1", "childId": CHILD, "unit": "AKS Eksempelby",
            "title": "Går hjem selv etter kl. 14.",
            "change": "returneres ikke lenger (utløpt eller slettet)"}
    rec = {"kind": "check_out_note", "fetched_at": "2026-10-11T06:00:00Z", "data": data}
    line = vw.render_header(rec)
    assert "returneres ikke lenger" in line and "⚠" not in line
    assert len(vw.render_full(rec, with_attachments=False, api=None)) > 1


def test_unknown_kind_does_not_crash():
    assert "mystery" in vw.render_header({"kind": "mystery", "data": {"a": 1}})


# --- Bodies and attachments -------------------------------------------------


def test_body_is_truncated_without_full_and_whole_with_it():
    long = "Lang tekst. " * 200
    rec = message_record(body=long)
    short = "\n".join(vw.render_full(rec, with_attachments=False, api=None))
    assert f"+{len(long) - vw.BODY_CHARS} chars — --full" in short
    whole = "\n".join(vw.render_full(rec, with_attachments=True, api=None))
    assert "--full]" not in whole
    assert whole.count("Lang tekst.") == 200


def test_missing_attachment_is_said_not_invented(workspace: dict[str, Path]):
    rec = message_record(attachments=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                       "path": str(workspace["root"] / "nope.pdf")}])
    out = "\n".join(vw.render_full(rec, with_attachments=True, api=None))
    assert "ikke lastet ned" in out


def test_downloaded_attachment_path_is_shown(workspace: dict[str, Path]):
    f = workspace["root"] / "uke.pdf"
    f.write_bytes(b"%PDF-1.4")
    rec = message_record(attachments=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                       "path": str(f)}])
    out = "\n".join(vw.render_full(rec, with_attachments=False, api=None))
    assert str(f) in out


def test_text_attachment_is_rendered_indented(workspace: dict[str, Path]):
    f = workspace["root"] / "notat.txt"
    f.write_text("linje en\nlinje to\n", encoding="utf-8")
    rec = message_record(attachments=[{"name": "notat.txt", "mimeType": "text/plain",
                                       "path": str(f)}])
    out = "\n".join(vw.render_full(rec, with_attachments=True, api=None))
    assert "linje en" in out and "linje to" in out


def test_binary_attachment_says_so(workspace: dict[str, Path]):
    f = workspace["root"] / "bilde.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    rec = message_record(attachments=[{"name": "bilde.jpg", "mimeType": "image/jpeg",
                                       "path": str(f)}])
    out = "\n".join(vw.render_full(rec, with_attachments=True, api=None))
    assert "ikke-tekstlig" in out


def test_extract_text_dispatch():
    assert vw.extract_text("hei".encode(), "text/plain", "a.txt") == "hei"
    assert vw.extract_text(b"\x00\x01", "application/octet-stream", "a.bin") is None
    calls: list[str] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(vw, "_pdf_text", lambda data: calls.append("pdf") or "PDF-TEKST")
    monkey.setattr(vw, "_docx_text", lambda data: calls.append("docx") or "DOCX-TEKST")
    assert vw.extract_text(b"x", "application/pdf", "a.pdf") == "PDF-TEKST"
    assert vw.extract_text(b"x", "", "notater.docx") == "DOCX-TEKST"
    assert calls == ["pdf", "docx"]
    monkey.undo()


def test_attachment_text_is_truncated(workspace: dict[str, Path]):
    f = workspace["root"] / "huge.txt"
    f.write_text("x" * (vw.ATTACH_CHARS + 100), encoding="utf-8")
    out = vw.render_attachment_text(f.read_bytes(), "text/plain", "huge.txt")
    assert f"truncated at {vw.ATTACH_CHARS}" in out


def test_news_attachment_is_fetched_over_the_api(workspace: dict[str, Path]):
    """News attachments come over the Bearer API — and a corrupt one degrades
    to a note instead of crashing the render (pypdf raises on fake bytes)."""
    class StubApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def post_attachment(self, post_id: str, att_id: str) -> bytes:
            self.calls.append((post_id, att_id))
            return b"%PDF-1.4 but not really"

    api = StubApi()
    out = "\n".join(vw.render_full(news_record(), with_attachments=True, api=api))  # ty: ignore[invalid-argument-type]
    assert api.calls == [("post-1", "a1")]
    assert "lot seg ikke tolkes" in out


def test_news_attachment_failure_is_reported_inline():
    from scripts.vigilo_api import VigiloError

    class DeadApi:
        def post_attachment(self, post_id: str, att_id: str) -> bytes:
            raise VigiloError("auth expired")

    out = "\n".join(vw.render_full(news_record(), with_attachments=True, api=DeadApi()))  # ty: ignore[invalid-argument-type]
    assert "nedlasting feilet" in out


# --- Cursor semantics -------------------------------------------------------


def test_cursor_is_strict_and_excludes_the_last_tripped_position(workspace: dict[str, Path]):
    """The bug the split cursor exists to avoid: a record exactly at the
    cursor is already triaged, and a `>=` would show it again forever."""
    write_records(workspace, message_record(fetched="2026-10-11T06:00:00Z"))
    workspace["state"].write_text(json.dumps({"cursor": "2026-10-11T06:00:00Z"}))
    assert list(vw.iter_records(vw.parse_iso("2026-10-11T06:00:00Z"), None, None, None)) == []
    assert len(list(vw.iter_records(vw.parse_iso("2026-10-11T05:00:00Z"), None, None, None))) == 1


def test_until_windows_a_range(workspace: dict[str, Path]):
    write_records(workspace,
                  message_record(fetched="2026-10-10T06:00:00Z"),
                  message_record(fetched="2026-10-11T06:00:00Z"),
                  day="2026-10-10")
    got = list(vw.iter_records(vw.parse_iso("2026-10-09"), vw.parse_iso("2026-10-10T12:00:00Z"),
                               None, None))
    assert [r["fetched_at"] for r in got] == ["2026-10-10T06:00:00Z"]


def test_kind_and_child_filters(workspace: dict[str, Path]):
    write_records(workspace, message_record(), news_record(), consent_record())
    only_consent = list(vw.iter_records(None, None, {"consent"}, None))
    assert [r["kind"] for r in only_consent] == ["consent"]
    assert list(vw.iter_records(None, None, None, "ikke-noen")) == []
    assert len(list(vw.iter_records(None, None, None, "AKS"))) == 3


def test_a_check_out_note_passes_both_filters(workspace: dict[str, Path]):
    write_records(workspace, message_record(), check_out_note_record())
    only = list(vw.iter_records(None, None, {"check_out_note"}, None))
    assert [r["kind"] for r in only] == ["check_out_note"]
    assert len(list(vw.iter_records(None, None, None, "aks"))) == 2, \
        "the unit filter must see the note too"


def test_the_cli_takes_the_new_kind(workspace: dict[str, Path], capsys):
    """``--kind`` choices come from KINDS; a missing entry exits 2, not silently."""
    write_records(workspace, message_record(), check_out_note_record())
    assert vw.main(["--kind", "check_out_note", "--headers-only"]) == 0
    out = capsys.readouterr().out
    assert "Går hjem selv" in out
    assert "Sjakkurs" not in out


def test_broken_lines_are_skipped(workspace: dict[str, Path]):
    (workspace["jsonl"] / "2026-10-11.jsonl").write_text(
        "{not json}\n" + json.dumps(message_record()) + "\n\n", encoding="utf-8"
    )
    assert len(list(vw.iter_records(None, None, None, None))) == 1


# --- CLI --------------------------------------------------------------------


def test_headers_only_is_one_line_per_record(workspace: dict[str, Path], capsys):
    write_records(workspace, message_record(), news_record())
    assert vw.main(["--headers-only"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2


def test_no_records_exits_one_silently(workspace: dict[str, Path], capsys):
    assert vw.main([]) == 1
    err = capsys.readouterr().err
    assert "no matching records" in err


def test_bump_advances_the_cursor_and_the_next_read_is_empty(workspace: dict[str, Path],
                                                             capsys):
    write_records(workspace, message_record(fetched="2026-10-11T06:00:00Z"))
    assert vw.main(["--headers-only"]) == 0
    assert vw.main(["--headers-only", "--bump"]) == 0
    state = json.loads(workspace["state"].read_text())
    assert state["cursor"] == "2026-10-11T06:00:00Z"
    capsys.readouterr()
    assert vw.main(["--since-cursor", "--headers-only"]) == 1
    assert "no matching records" in capsys.readouterr().err


def test_missing_state_file_is_not_an_error(workspace: dict[str, Path], capsys):
    write_records(workspace, message_record())
    assert vw.main(["--since-cursor", "--headers-only"]) == 0
    assert "cursor: none yet" in capsys.readouterr().err


def test_empty_and_no_cursor_is_a_forgiven_quiet_track(workspace: dict[str, Path], capsys):
    """Before the first login (or first --bump) there is no state and nothing
    to show — sjekk_collect must read that as a quiet track, not FAILED."""
    from scripts.sjekk_collect import _is_nothing_new

    assert vw.main(["--since-cursor", "--headers-only"]) == 1
    quiet = capsys.readouterr()
    assert _is_nothing_new(quiet.out + quiet.err), (
        "every line must match sjekk_collect's (cursor: …)/(no matching …) shape"
    )


def test_auth_required_with_no_records_stays_loud(workspace: dict[str, Path], capsys):
    """The opposite case: a dead login must surface as FAILED in the bundle."""
    from scripts.sjekk_collect import _is_nothing_new

    workspace["state"].write_text(json.dumps({"auth_required": True}))
    assert vw.main(["--since-cursor", "--headers-only"]) == 1
    loud = capsys.readouterr()
    assert "VIGILO AUTH REQUIRED" in loud.err
    assert not _is_nothing_new(loud.out + loud.err)


def test_expired_auth_shouts_before_anything_else(workspace: dict[str, Path], capsys):
    workspace["state"].write_text(json.dumps({"auth_required": True}))
    write_records(workspace, message_record())
    vw.main(["--headers-only", "--bump"])
    err = capsys.readouterr().err
    assert "VIGILO AUTH REQUIRED" in err
    assert "vigilo-login" in err


def test_all_ignores_the_cursor(workspace: dict[str, Path], capsys):
    workspace["state"].write_text(json.dumps({"cursor": "2026-10-11T06:00:00Z"}))
    write_records(workspace, message_record())
    assert vw.main(["--all", "--headers-only"]) == 0
    assert "ignoring the cursor" in capsys.readouterr().err
