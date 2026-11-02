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


def test_save_attachments_symlink_collision_does_not_overwrite_target(tmp_path: Path):
    raw = _multipart_msg([("dup.txt", "text/plain", b"new")])
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    outdir = tmp_path / "store"
    outdir.mkdir()
    (outdir / "dup.txt").symlink_to(outside)
    written = mailshow.save_attachments(raw, outdir)
    assert [p.name for p in written] == ["dup.txt.1"]
    assert outside.read_bytes() == b"original"


def test_save_attachments_rejects_symlinked_output_directory(tmp_path: Path):
    raw = _multipart_msg([("dup.txt", "text/plain", b"new")])
    outside = tmp_path / "outside"
    outside.mkdir()
    outdir = tmp_path / "store"
    outdir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        mailshow.save_attachments(raw, outdir)
    assert not (outside / "dup.txt").exists()


def test_window_query_without_bounds_is_just_inbox():
    assert mailshow.window_query(None, None, None) == "tag:inbox"


def test_window_query_is_half_open():
    from datetime import datetime, timezone
    since = datetime(2026, 5, 17, 17, 0, tzinfo=timezone.utc)
    until = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
    q = mailshow.window_query(since, until, None)
    # +1 to skip the message at the lower bound (notmuch date:@N.. is inclusive).
    assert q == (f"tag:inbox and date:@{int(since.timestamp()) + 1}"
                 f"..@{int(until.timestamp())}")


def test_window_query_ands_the_extra_query():
    from datetime import datetime, timezone
    since = datetime(2026, 5, 17, 17, 0, tzinfo=timezone.utc)
    q = mailshow.window_query(since, None, "from:astrid")
    assert q == f"(tag:inbox and date:@{int(since.timestamp()) + 1}..) and (from:astrid)"


def test_exclude_ids_and_ids_out(tmp_path, monkeypatch, capsys):
    import json as _json
    (tmp_path / "mail.ids").write_text("A\n")
    (tmp_path / "exclude.ids").write_text("id:B\n")

    monkeypatch.setattr(mailshow, "notmuch_search_ids", lambda q, limit: ["A", "B", "C"])
    monkeypatch.setattr(
        mailshow, "render_message",
        lambda mid, *a, **k: _json.dumps({"id": mid}))

    rc = mailshow.main([
        "--since", "2026-05-17T17:00:00Z",
        "--ids-out", str(tmp_path / "mail.ids"),
        "--exclude-ids", str(tmp_path / "exclude.ids"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"id": "A"' in out and '"id": "C"' in out
    assert '"id": "B"' not in out, "excluded overlap id must not be re-shown"
    assert (tmp_path / "mail.ids").read_text().split() == ["A", "C"]
