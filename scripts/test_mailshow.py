"""Tests for mailshow.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import mailshow
from scripts.test_embed_mail import _make_docx, _make_pdf, _multipart_msg


@pytest.fixture
def pdf_msg() -> bytes:
    return _multipart_msg([("ukeplan.pdf", "application/pdf", _make_pdf("Mandag uteskole"))])


@pytest.fixture
def multi_msg() -> bytes:
    return _multipart_msg([
        ("notes.pdf", "application/pdf", _make_pdf("hello")),
        ("logo.png", "image/png", b"\x89PNG\r\n\x1a\nfake"),
        ("doc.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         _make_docx("docx body")),
    ])


def test_render_summary_only(monkeypatch, pdf_msg):
    monkeypatch.setattr(mailshow, "fetch_raw", lambda _: pdf_msg)
    out = mailshow.render_message("x@y", headers_only=False, max_chars=4000)
    assert "Attachments: ukeplan.pdf [application/pdf]" in out
    assert "Mandag uteskole" not in out  # text NOT inlined without flag


def test_render_attachment_text_inlines_pdf(monkeypatch, pdf_msg):
    monkeypatch.setattr(mailshow, "fetch_raw", lambda _: pdf_msg)
    out = mailshow.render_message(
        "x@y", headers_only=False, max_chars=4000, attachment_text=True,
    )
    assert "----- attachment: ukeplan.pdf [application/pdf]" in out
    assert "Mandag uteskole" in out


def test_headers_only_does_not_suppress_attachment_text(monkeypatch, pdf_msg):
    monkeypatch.setattr(mailshow, "fetch_raw", lambda _: pdf_msg)
    out = mailshow.render_message(
        "x@y", headers_only=True, max_chars=4000, attachment_text=True,
    )
    assert "Mandag uteskole" in out
    assert "body text" not in out  # body still suppressed


def test_render_attachment_text_marks_binary(monkeypatch, multi_msg):
    monkeypatch.setattr(mailshow, "fetch_raw", lambda _: multi_msg)
    out = mailshow.render_message(
        "x@y", headers_only=False, max_chars=4000, attachment_text=True,
    )
    assert "no text extractable" in out  # the PNG
    assert "docx body" in out             # the DOCX
    assert "hello" in out                 # the PDF


def test_save_attachments_writes_files(tmp_path: Path, multi_msg):
    written = mailshow.save_attachments(multi_msg, tmp_path)
    names = sorted(p.name for p in written)
    assert names == ["doc.docx", "logo.png", "notes.pdf"]
    assert (tmp_path / "logo.png").read_bytes().startswith(b"\x89PNG")


def test_save_attachments_collision(tmp_path: Path):
    raw = _multipart_msg([
        ("dup.txt", "text/plain", b"first"),
        ("dup.txt", "text/plain", b"second"),
    ])
    written = mailshow.save_attachments(raw, tmp_path)
    names = sorted(p.name for p in written)
    assert names == ["dup.txt", "dup.txt.1"]
    contents = sorted((tmp_path / n).read_bytes() for n in names)
    assert contents == [b"first", b"second"]


def test_save_attachments_sanitises_path_traversal(tmp_path: Path):
    raw = _multipart_msg([("../escape.txt", "text/plain", b"x")])
    written = mailshow.save_attachments(raw, tmp_path)
    # `/` becomes `_`, leading dots are stripped → nothing escapes outdir
    assert written == [tmp_path / "_escape.txt"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_load_cursor_parses_zulu(tmp_path: Path):
    state = tmp_path / "mail-state.json"
    state.write_text('{"last_successful_run": "2026-05-17T17:00:00Z"}')
    dt = mailshow.load_cursor(state)
    assert dt.isoformat() == "2026-05-17T17:00:00+00:00"


def test_load_cursor_parses_offset(tmp_path: Path):
    state = tmp_path / "mail-state.json"
    state.write_text('{"last_successful_run": "2026-05-17T19:00:00+02:00"}')
    dt = mailshow.load_cursor(state)
    # Normalized to UTC.
    assert dt.isoformat() == "2026-05-17T17:00:00+00:00"


def test_cursor_query_without_extra():
    from datetime import datetime, timezone
    cursor = datetime(2026, 5, 17, 17, 0, tzinfo=timezone.utc)
    q = mailshow.cursor_query(cursor, None)
    assert q == f"tag:inbox and date:@{int(cursor.timestamp())}.."


def test_cursor_query_with_extra():
    from datetime import datetime, timezone
    cursor = datetime(2026, 5, 17, 17, 0, tzinfo=timezone.utc)
    q = mailshow.cursor_query(cursor, "from:astrid")
    ts = int(cursor.timestamp())
    assert q == f"(tag:inbox and date:@{ts}..) and (from:astrid)"
