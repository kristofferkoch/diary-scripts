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
