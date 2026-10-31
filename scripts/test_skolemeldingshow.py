"""Tests for skolemeldingshow.py. Run with `uv run pytest scripts/`.

The reader is what a sjekk actually looks at, so the field names its headers
render are pinned here: a silent rename upstream shows up as an empty column,
and these tests turn that into a failure instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import skolemeldingshow as sw

MSG_ID = 12345678


def message_record(*, fetched: str = "2026-10-13T06:00:00Z", kind: str = "message",
                   body: str = "Husk å svare på spørsmålet",
                   attachments: list[dict[str, Any]] | None = None,
                   threads: list[dict[str, Any]] | None = None,
                   **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": MSG_ID,
        "header": "Ukeplan uke 42",
        "sender": "Eksempel skole",
        "timestamp": "2026-10-12T10:00:00+02:00",
        "body": body,
        "threads": threads or [],
        "attachments": attachments or [],
    }
    data.update(over)
    return {"kind": kind, "fetched_at": fetched, "data": data}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    jsonl = tmp_path / "skolemelding"
    jsonl.mkdir()
    state = tmp_path / "skolemelding-state.json"
    monkeypatch.setattr(sw, "STATE_PATH", state)
    monkeypatch.setattr(sw, "JSONL_DIR", jsonl)
    monkeypatch.setattr(sw, "ATTACH_DIR", tmp_path)
    return {"jsonl": jsonl, "state": state, "root": tmp_path}


def write_records(workspace: dict[str, Path], *recs: dict[str, Any],
                  day: str = "2026-10-13") -> None:
    path = workspace["jsonl"] / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- Headers ----------------------------------------------------------------


def test_message_header_carries_sender_and_subject():
    line = sw.render_header(message_record())
    assert "Eksempel skole" in line
    assert "Ukeplan uke 42" in line
    assert "2026-10-12T10:00:00+02:00" in line
    assert "[U message" in line


def test_update_header_is_marked_apart_from_new():
    assert "[↩ message_update" in sw.render_header(message_record(kind="message_update"))


def test_header_marks_attachments():
    rec = message_record(attachments=[{"name": "skjema.pdf", "mimeType": "application/pdf"}])
    assert "📎1" in sw.render_header(rec)


def test_header_marks_a_feedback_question():
    assert "❓" in sw.render_header(message_record(feedback_question=True))
    assert "❓" not in sw.render_header(message_record())


def test_unknown_kind_does_not_crash():
    assert "mystery" in sw.render_header({"kind": "mystery", "data": {"a": 1}})


# --- Bodies, threads and attachments ----------------------------------------


def test_body_is_truncated_without_full_and_whole_with_it():
    long = "Lang tekst. " * 200
    rec = message_record(body=long)
    short = "\n".join(sw.render_full(rec, with_attachments=False))
    assert f"+{len(long) - sw.BODY_CHARS} chars — --full" in short
    whole = "\n".join(sw.render_full(rec, with_attachments=True))
    assert "--full]" not in whole
    assert whole.count("Lang tekst.") == 200


def test_thread_replies_are_rendered():
    rec = message_record(threads=[{"sender": "Forelder", "timestamp": "2026-10-13T07:00:00+02:00",
                                   "body": "Takk for beskjed", "attachments": []}])
    out = "\n".join(sw.render_full(rec, with_attachments=False))
    assert "--- Forelder (2026-10-13T07:00:00+02:00) ---" in out
    assert "Takk for beskjed" in out


def test_missing_attachment_is_said_not_invented(workspace: dict[str, Path]):
    rec = message_record(attachments=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                       "source": "/globalassets/attachments/x/uke.pdf",
                                       "path": str(workspace["root"] / "nope.pdf")}])
    out = "\n".join(sw.render_full(rec, with_attachments=True))
    assert "ikke lastet ned" in out


def test_attachment_paths_cannot_escape_the_store(workspace: dict[str, Path]):
    outside = workspace["root"].parent / "outside"
    outside.write_text("secret")
    linked = workspace["root"] / "linked"
    linked.symlink_to(outside)
    for path in (outside, linked, "../outside"):
        rec = message_record(attachments=[{
            "name": "secret.txt", "mimeType": "text/plain", "path": str(path)}])
        out = "\n".join(sw.render_full(rec, with_attachments=True))
        assert "ikke lastet ned" in out
        assert "secret\n" not in out


def test_downloaded_attachment_path_is_shown(workspace: dict[str, Path]):
    f = workspace["root"] / "uke.pdf"
    f.write_bytes(b"%PDF-1.4")
    rec = message_record(attachments=[{"name": "uke.pdf", "mimeType": "application/pdf",
                                       "path": str(f)}])
    out = "\n".join(sw.render_full(rec, with_attachments=False))
    assert str(f) in out


def test_text_attachment_is_rendered_indented(workspace: dict[str, Path]):
    f = workspace["root"] / "notat.txt"
    f.write_text("linje en\nlinje to\n", encoding="utf-8")
    rec = message_record(attachments=[{"name": "notat.txt", "mimeType": "text/plain",
                                       "path": str(f)}])
    out = "\n".join(sw.render_full(rec, with_attachments=True))
    assert "linje en" in out and "linje to" in out


def test_binary_attachment_says_so(workspace: dict[str, Path]):
    f = workspace["root"] / "bilde.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    rec = message_record(attachments=[{"name": "bilde.jpg", "mimeType": "image/jpeg",
                                       "path": str(f)}])
    out = "\n".join(sw.render_full(rec, with_attachments=True))
    assert "ikke-tekstlig" in out


def test_extract_text_dispatch():
    assert sw.extract_text("hei".encode(), "text/plain", "a.txt") == "hei"
    assert sw.extract_text(b"\x00\x01", "application/octet-stream", "a.bin") is None
    calls: list[str] = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sw, "_pdf_text", lambda data: calls.append("pdf") or "PDF-TEKST")
    monkey.setattr(sw, "_docx_text", lambda data: calls.append("docx") or "DOCX-TEKST")
    assert sw.extract_text(b"x", "application/pdf", "a.pdf") == "PDF-TEKST"
    assert sw.extract_text(b"x", "", "notater.docx") == "DOCX-TEKST"
    assert calls == ["pdf", "docx"]
    monkey.undo()


def test_attachment_text_is_truncated(workspace: dict[str, Path]):
    f = workspace["root"] / "huge.txt"
    f.write_text("x" * (sw.ATTACH_CHARS + 100), encoding="utf-8")
    out = sw.render_attachment_text(f.read_bytes(), "text/plain", "huge.txt")
    assert f"truncated at {sw.ATTACH_CHARS}" in out


# --- Cursor semantics -------------------------------------------------------


def test_cursor_is_strict_and_excludes_the_last_tripped_position(workspace: dict[str, Path]):
    """The bug the split cursor exists to avoid: a record exactly at the
    cursor is already triaged, and a `>=` would show it again forever."""
    write_records(workspace, message_record(fetched="2026-10-13T06:00:00Z"))
    workspace["state"].write_text(json.dumps({"cursor": "2026-10-13T06:00:00Z"}))
    assert list(sw.iter_records(sw.parse_iso("2026-10-13T06:00:00Z"), None, None, None)) == []
    assert len(list(sw.iter_records(sw.parse_iso("2026-10-13T05:00:00Z"),
                                    None, None, None))) == 1


def test_until_windows_a_range(workspace: dict[str, Path]):
    write_records(workspace,
                  message_record(fetched="2026-10-12T06:00:00Z"),
                  message_record(fetched="2026-10-13T06:00:00Z"),
                  day="2026-10-12")
    got = list(sw.iter_records(sw.parse_iso("2026-10-11"),
                               sw.parse_iso("2026-10-12T12:00:00Z"), None, None))
    assert [r["fetched_at"] for r in got] == ["2026-10-12T06:00:00Z"]


def test_kind_and_id_filters(workspace: dict[str, Path]):
    write_records(workspace, message_record(),
                  message_record(kind="message_update"),
                  message_record(id=999))
    only_updates = list(sw.iter_records(None, None, {"message_update"}, None))
    assert [r["kind"] for r in only_updates] == ["message_update"]
    only_one = list(sw.iter_records(None, None, None, "999"))
    assert [r["data"]["id"] for r in only_one] == [999]
    assert list(sw.iter_records(None, None, None, "444")) == []


def test_broken_lines_are_skipped(workspace: dict[str, Path]):
    (workspace["jsonl"] / "2026-10-13.jsonl").write_text(
        "{not json}\n" + json.dumps(message_record()) + "\n\n", encoding="utf-8"
    )
    assert len(list(sw.iter_records(None, None, None, None))) == 1


# --- CLI --------------------------------------------------------------------


def test_headers_only_is_one_line_per_record(workspace: dict[str, Path], capsys):
    write_records(workspace, message_record(), message_record(id=999))
    assert sw.main(["--headers-only"]) == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(lines) == 2


def test_no_records_exits_one_silently(workspace: dict[str, Path], capsys):
    assert sw.main([]) == 1
    err = capsys.readouterr().err
    assert "no matching records" in err


def test_bump_advances_the_cursor_and_the_next_read_is_empty(workspace: dict[str, Path],
                                                             capsys):
    write_records(workspace, message_record(fetched="2026-10-13T06:00:00Z"))
    assert sw.main(["--headers-only"]) == 0
    assert sw.main(["--headers-only", "--bump"]) == 0
    state = json.loads(workspace["state"].read_text())
    assert state["cursor"] == "2026-10-13T06:00:00Z"
    capsys.readouterr()
    assert sw.main(["--since-cursor", "--headers-only"]) == 1
    assert "no matching records" in capsys.readouterr().err


def test_missing_state_file_is_not_an_error(workspace: dict[str, Path], capsys):
    write_records(workspace, message_record())
    assert sw.main(["--since-cursor", "--headers-only"]) == 0
    assert "cursor: none yet" in capsys.readouterr().err


def test_empty_and_no_cursor_is_a_forgiven_quiet_track(workspace: dict[str, Path], capsys):
    """Before the first login (or first --bump) there is no state and nothing
    to show — sjekk_collect must read that as a quiet track, not FAILED."""
    from scripts.sjekk_collect import _is_nothing_new

    assert sw.main(["--since-cursor", "--headers-only"]) == 1
    quiet = capsys.readouterr()
    assert _is_nothing_new(quiet.out + quiet.err), (
        "every line must match sjekk_collect's (cursor: …)/(no matching …) shape"
    )


def test_auth_required_with_no_records_stays_loud(workspace: dict[str, Path], capsys):
    """The opposite case: a dead login must surface as FAILED in the bundle."""
    from scripts.sjekk_collect import _is_nothing_new

    workspace["state"].write_text(json.dumps({"auth_required": True}))
    assert sw.main(["--since-cursor", "--headers-only"]) == 1
    loud = capsys.readouterr()
    assert "SKOLEMELDING AUTH REQUIRED" in loud.err
    assert not _is_nothing_new(loud.out + loud.err)


def test_expired_auth_shouts_before_anything_else(workspace: dict[str, Path], capsys):
    workspace["state"].write_text(json.dumps({"auth_required": True}))
    write_records(workspace, message_record())
    sw.main(["--headers-only", "--bump"])
    err = capsys.readouterr().err
    assert "SKOLEMELDING AUTH REQUIRED" in err
    assert "skolemelding-login" in err


def test_all_ignores_the_cursor(workspace: dict[str, Path], capsys):
    workspace["state"].write_text(json.dumps({"cursor": "2026-10-13T06:00:00Z"}))
    write_records(workspace, message_record())
    assert sw.main(["--all", "--headers-only"]) == 0
    assert "ignoring the cursor" in capsys.readouterr().err
