"""Tests for embed_mail.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

import email
import io
import shutil
import subprocess
import zipfile
from email.policy import default as email_default
from pathlib import Path

import pytest

from embed_mail import (
    CHUNK_CHARS,
    _strip_xml,
    chunk_text,
    extract_attachment_text,
    extract_docx,
    extract_odt,
    extract_pdf,
    iter_attachments,
    parse_message,
    pick_date,
    strip_quotes_sig,
    tier_query,
)


# ---------- helpers ----------

def _msg(headers: dict[str, list[str]], body: str = "body") -> email.message.Message:
    parts: list[str] = []
    for k, vs in headers.items():
        for v in vs:
            parts.append(f"{k}: {v}")
    raw = ("\n".join(parts) + "\n\n" + body).encode()
    return email.message_from_bytes(raw, policy=email_default)


def _make_docx(text: str) -> bytes:
    """Tiniest .docx that pdftotext-style extractors will recognise.

    A docx is a zip containing word/document.xml. We don't need the full
    OOXML scaffolding for the extractor — it just unzips and strips tags.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<w:document xmlns:w="x">'
                   f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>'
                   '</w:document>')
    return buf.getvalue()


def _make_odt(text: str) -> bytes:
    """Tiniest .odt — zip with content.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<office:document-content xmlns:office="urn:office" '
                   'xmlns:text="urn:text">'
                   f'<office:body><office:text><text:p>{text}'
                   '</text:p></office:text></office:body>'
                   '</office:document-content>')
    return buf.getvalue()


def _make_pdf(text: str) -> bytes:
    """Minimal valid 1-page PDF whose page contains the given text.

    Built with byte-exact offsets so the xref table stays valid. Used to verify
    the pdftotext path end-to-end without depending on a checked-in binary.
    """
    # Stream is a content stream that draws `text` once at (10, 50) in Helvetica 12.
    safe = text.replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 12 Tf 10 50 Td ({safe}) Tj ET\n".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_off = len(out)
    out += f"xref\n0 {len(objs)+1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_off}\n%%EOF\n").encode()
    return bytes(out)


def _multipart_msg(attachments: list[tuple[str, str, bytes]],
                   plain_body: str = "body text") -> bytes:
    """Build raw mail with the given attachments (filename, mime, bytes)."""
    msg = email.message.EmailMessage(policy=email_default)
    msg["From"] = "a@b"
    msg["To"] = "c@d"
    msg["Subject"] = "test"
    msg["Date"] = "Sat, 1 Jan 2020 12:00:00 +0000"
    msg.set_content(plain_body)
    for fn, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fn)
    return bytes(msg)


# ---------- existing tests (kept verbatim) ----------

def test_pick_date_skips_implausible_first_header():
    m = _msg({"Date": [
        "Tue, 04 Jan 2270 08:35:50 +0000",
        "Sat, 1 Jan 2000 19:15:35 +0000",
    ]})
    assert pick_date(m) == "2000-01-01T19:15:35+00:00"


def test_pick_date_returns_none_when_all_implausible():
    m = _msg({"Date": ["Tue, 04 Jan 2270 08:35:50 +0000"]})
    assert pick_date(m) is None


def test_pick_date_handles_naive_datetime_as_utc():
    m = _msg({"Date": ["Sat, 1 Jan 2020 12:00:00 -0000"]})
    assert pick_date(m) == "2020-01-01T12:00:00+00:00"


def test_pick_date_no_header():
    m = _msg({"From": ["a@b"]})
    assert pick_date(m) is None


def test_chunk_text_long_input_chunks_with_overlap():
    text = ("paragraph one. " * 200) + "\n\n" + ("paragraph two. " * 200)
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    joined = "".join(chunks)
    assert len(joined) >= len(text.strip())


def test_chunk_text_snaps_to_blank_line_when_available():
    head = "a" * (CHUNK_CHARS - 100)
    text = head + "\n\nTAIL" + ("z" * 3000)
    chunks = chunk_text(text)
    assert chunks[0].endswith("a" * 10)
    assert chunks[1].startswith("TAIL") or "TAIL" in chunks[1][:300]


def test_strip_quotes_sig_strips_norwegian_skrev():
    text = (
        "Takk for sist!\n"
        "\n"
        "Den lør. 14. mai 2026 kl. 09:30 skrev Ola <ola@example.com>:\n"
        "> dette er det gamle svaret\n"
        "> som ikke skal med\n"
    )
    cleaned = strip_quotes_sig(text)
    assert "Takk for sist" in cleaned
    assert "gamle svaret" not in cleaned


def test_strip_quotes_sig_drops_dash_signature():
    text = "Hei!\n\nKan du sjekke dette?\n\n-- \nMvh Example\nTlf 123\n"
    cleaned = strip_quotes_sig(text)
    assert "sjekke dette" in cleaned
    assert "Mvh Example" not in cleaned


def test_parse_message_prefers_text_plain_over_html():
    raw = (
        b"From: a@b\r\n"
        b"To: c@d\r\n"
        b"Subject: hi\r\n"
        b"Date: Sat, 1 Jan 2020 12:00:00 +0000\r\n"
        b'Content-Type: multipart/alternative; boundary="X"\r\n'
        b"\r\n"
        b"--X\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"plain body wins\r\n"
        b"--X\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p>html loses</p>\r\n"
        b"--X--\r\n"
    )
    hdr, body = parse_message(raw)
    assert hdr["subject"] == "hi"
    assert hdr["date"] == "2020-01-01T12:00:00+00:00"
    assert "plain body wins" in body
    assert "html loses" not in body


def test_parse_message_falls_back_to_html_when_no_plain():
    raw = (
        b"From: a@b\r\n"
        b"Subject: only-html\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p>hello &amp; goodbye</p>\r\n"
    )
    _, body = parse_message(raw)
    assert "hello & goodbye" in body
    assert "<p>" not in body


def test_tier_query_tier1():
    assert tier_query(1) == "date:1y.."


def test_tier_query_tier2_includes_me():
    q = tier_query(2)
    assert q.startswith('thread:"{from:')
    assert q.endswith('}"')


# ---------- attachment extractors ----------

class TestExtractAttachmentText:
    def test_plain_text(self):
        assert extract_attachment_text("text/plain", b"hello world").strip() == "hello world"

    def test_text_csv(self):
        out = extract_attachment_text("text/csv", b"a,b,c\n1,2,3")
        assert "a,b,c" in out and "1,2,3" in out

    def test_text_calendar_passthrough(self):
        ics = (b"BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
               b"SUMMARY:Tannlege Robin\nEND:VEVENT\nEND:VCALENDAR\n")
        out = extract_attachment_text("text/calendar", ics)
        assert "Tannlege Robin" in out
        assert "VEVENT" in out

    def test_application_ics_passthrough(self):
        ics = b"BEGIN:VCALENDAR\nSUMMARY:Foreldremoete\nEND:VCALENDAR\n"
        out = extract_attachment_text("application/ics", ics)
        assert "Foreldremoete" in out

    def test_html_strips_tags(self):
        out = extract_attachment_text("text/html", b"<p>hei <b>verden</b></p>")
        assert "hei verden" in out
        assert "<p>" not in out

    def test_image_returns_empty(self):
        # Truncated PNG header — should NOT be decoded as text.
        assert extract_attachment_text("image/png", b"\x89PNG\r\n\x1a\n") == ""

    def test_octet_stream_returns_empty(self):
        assert extract_attachment_text("application/octet-stream", b"binary") == ""

    def test_unknown_mime_returns_empty(self):
        assert extract_attachment_text("application/x-weird", b"data") == ""

    def test_none_mime_returns_empty(self):
        assert extract_attachment_text("", b"data") == ""

    def test_case_insensitive_mime(self):
        assert "hi" in extract_attachment_text("TEXT/PLAIN", b"hi")


class TestExtractDocx:
    def test_roundtrip(self):
        text = extract_docx(_make_docx("Skolesvømming uke 23"))
        assert "Skolesvømming uke 23" in text

    def test_unicode_norwegian(self):
        text = extract_docx(_make_docx("ÆØÅ æøå godt"))
        assert "ÆØÅ" in text and "æøå" in text

    def test_corrupt_docx_returns_empty(self):
        assert extract_docx(b"not a zip") == ""

    def test_zip_without_document_xml_returns_empty(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "no document.xml here")
        assert extract_docx(buf.getvalue()) == ""


class TestExtractOdt:
    def test_roundtrip(self):
        text = extract_odt(_make_odt("Protokoll fra styremøte"))
        assert "Protokoll fra styremøte" in text

    def test_corrupt_odt_returns_empty(self):
        assert extract_odt(b"not a zip") == ""


class TestExtractPdf:
    @pytest.mark.skipif(shutil.which("pdftotext") is None,
                        reason="pdftotext not installed")
    def test_synthetic_pdf_roundtrip(self):
        pdf = _make_pdf("Hello PDF World")
        text = extract_pdf(pdf)
        assert "Hello PDF World" in text

    @pytest.mark.skipif(shutil.which("pdftotext") is None,
                        reason="pdftotext not installed")
    def test_corrupt_pdf_returns_empty(self):
        # pdftotext on garbage exits nonzero; extractor should swallow that.
        assert extract_pdf(b"not a pdf") == ""

    def test_no_pdftotext_returns_empty(self, monkeypatch):
        # Force the binary lookup to fail by replacing subprocess.run.
        def boom(*a, **kw):
            raise FileNotFoundError("pdftotext")
        monkeypatch.setattr("embed_mail.subprocess.run", boom)
        assert extract_pdf(b"%PDF-1.4\n") == ""


class TestStripXml:
    def test_basic(self):
        assert _strip_xml("<w:p><w:t>Hi</w:t></w:p>").strip() == "Hi"

    def test_entities_decoded(self):
        assert _strip_xml("&amp;&lt;&gt;").strip() == "&<>"

    def test_collapses_whitespace(self):
        assert _strip_xml("<a>x</a>     <b>y</b>").strip() == "x y"


# ---------- iter_attachments ----------

class TestIterAttachments:
    def test_yields_pdf_attachment(self):
        pdf_bytes = _make_pdf("attachment text")
        raw = _multipart_msg([("doc.pdf", "application/pdf", pdf_bytes)])
        atts = list(iter_attachments(raw))
        assert len(atts) == 1
        fn, mime, data, text = atts[0]
        assert fn == "doc.pdf"
        assert mime == "application/pdf"
        assert data == pdf_bytes
        # Text extraction only runs if pdftotext available; test data shape always.
        if shutil.which("pdftotext"):
            assert "attachment text" in text

    def test_yields_multiple_attachments(self):
        raw = _multipart_msg([
            ("a.ics", "text/calendar", b"BEGIN:VCALENDAR\nSUMMARY:A\nEND:VCALENDAR"),
            ("b.txt", "text/plain", b"plain bytes"),
        ])
        atts = list(iter_attachments(raw))
        assert len(atts) == 2
        fns = {a[0] for a in atts}
        assert fns == {"a.ics", "b.txt"}

    def test_does_not_yield_body_part(self):
        # The body (text/plain "body text") is NOT an attachment.
        raw = _multipart_msg([("doc.txt", "text/plain", b"attached")],
                             plain_body="body text")
        atts = list(iter_attachments(raw))
        assert len(atts) == 1
        assert atts[0][0] == "doc.txt"
        assert atts[0][3].strip() == "attached"

    def test_empty_when_no_attachments(self):
        raw = (b"From: a@b\r\nSubject: plain\r\n"
               b"Content-Type: text/plain\r\n\r\nbody only\r\n")
        assert list(iter_attachments(raw)) == []

    def test_image_attachment_yielded_with_empty_text(self):
        raw = _multipart_msg([("logo.png", "image/png", b"\x89PNG\r\n\x1a\n")])
        atts = list(iter_attachments(raw))
        assert len(atts) == 1
        fn, mime, data, text = atts[0]
        assert mime == "image/png"
        assert text == ""   # unsupported → empty, but row still yielded


# ---------- batched embedding loop ----------

class TestPrepareMessage:
    def test_body_only(self, monkeypatch):
        from embed_mail import _prepare_message
        raw = b"From: a@b\r\nSubject: s\r\nContent-Type: text/plain\r\n\r\n" + b"x" * 200
        monkeypatch.setattr("embed_mail.nm_raw", lambda mid: raw)
        monkeypatch.setattr("embed_mail.nm_thread_id", lambda mid: "tid-1")
        p = _prepare_message("id@x")
        assert p is not None
        assert p["mid"] == "id@x"
        assert p["tid"] == "tid-1"
        assert len(p["body_chunks"]) >= 1
        assert p["attachments"] == []

    def test_returns_none_for_empty_message(self, monkeypatch):
        from embed_mail import _prepare_message
        raw = b"From: a@b\r\nSubject: s\r\nContent-Type: text/plain\r\n\r\nshort"
        monkeypatch.setattr("embed_mail.nm_raw", lambda mid: raw)
        monkeypatch.setattr("embed_mail.nm_thread_id", lambda mid: None)
        # body is "short" (5 chars, < 40) and no attachments → None.
        assert _prepare_message("id@x") is None

    def test_short_body_plus_attachment_is_kept(self, monkeypatch):
        from embed_mail import _prepare_message
        raw = _multipart_msg(
            [("a.ics", "text/calendar",
              b"BEGIN:VCALENDAR\nSUMMARY:Foo\nEND:VCALENDAR")],
            plain_body="hi",
        )
        monkeypatch.setattr("embed_mail.nm_raw", lambda mid: raw)
        monkeypatch.setattr("embed_mail.nm_thread_id", lambda mid: None)
        p = _prepare_message("id@x")
        assert p is not None
        assert p["body_chunks"] == []     # body too short
        assert len(p["attachments"]) == 1
        assert p["attachments"][0]["mime"] == "text/calendar"
        assert p["attachments"][0]["text_chars"] > 0
        assert len(p["attachments"][0]["chunks"]) >= 1


class TestEmbedBatch:
    def test_calls_api_embed_with_list(self, monkeypatch):
        import embed_mail
        captured: dict = {}

        class FakeResp:
            def __init__(self, payload: bytes): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self.payload

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            import json
            return FakeResp(json.dumps(
                {"embeddings": [[0.1] * 1024, [0.2] * 1024, [0.3] * 1024]}
            ).encode())

        monkeypatch.setattr("embed_mail.urllib.request.urlopen", fake_urlopen)
        out = embed_mail.embed_batch(["a", "b", "c"])
        assert len(out) == 3
        assert all(len(v) == 1024 for v in out)
        assert captured["url"].endswith("/api/embed")
        import json
        body = json.loads(captured["body"].decode())
        assert body["input"] == ["a", "b", "c"]
        assert body["model"]    # whatever EMBED_MODEL resolved to at import

    def test_empty_input_skips_http(self, monkeypatch):
        import embed_mail
        def explode(*a, **kw):
            raise AssertionError("should not call HTTP for empty input")
        monkeypatch.setattr("embed_mail.urllib.request.urlopen", explode)
        assert embed_mail.embed_batch([]) == []

    def test_mismatched_count_raises(self, monkeypatch):
        import embed_mail

        class FakeResp:
            def __init__(self, payload: bytes): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self.payload

        def fake_urlopen(req, timeout=None):
            import json
            return FakeResp(json.dumps({"embeddings": [[0.1] * 1024]}).encode())

        monkeypatch.setattr("embed_mail.urllib.request.urlopen", fake_urlopen)
        with pytest.raises(RuntimeError, match="expected 2"):
            embed_mail.embed_batch(["a", "b"])


# ---------- end-to-end with real notmuch (skipped if mail not present) ----------

_HAS_NOTMUCH = (shutil.which("notmuch") is not None
                and Path("~/Mail/Proton/.notmuch").expanduser().exists())


def _first_message_with_attachment_of(mime: str) -> bytes | None:
    """Scan notmuch (limited) for the first message carrying an attachment of
    the given MIME type. Returns raw bytes or None."""
    if not _HAS_NOTMUCH:
        return None
    proc = subprocess.run(
        ["notmuch", "search", "--limit=2000",
         "--output=messages", "attachment"],
        capture_output=True, text=True)
    if proc.returncode:
        return None
    for line in proc.stdout.splitlines():
        if not line.startswith("id:"):
            continue
        mid = line[3:].strip()
        try:
            raw = subprocess.run(
                ["notmuch", "show", "--format=raw", f"id:{mid}"],
                capture_output=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        msg = email.message_from_bytes(raw, policy=email_default)
        if any(p.get_content_type() == mime for p in msg.walk()):
            return raw
    return None


@pytest.mark.skipif(not _HAS_NOTMUCH, reason="~/Mail/Proton/.notmuch not present")
class TestRealMailSamples:
    """End-to-end tests against the user's actual mail. Skip on machines
    without notmuch + ~/Mail. Asserts that iter_attachments returns reasonable
    output for each MIME type we expect to encounter in real mail."""

    @pytest.mark.skipif(shutil.which("pdftotext") is None,
                        reason="pdftotext not installed")
    def test_real_pdf_attachment(self):
        raw = _first_message_with_attachment_of("application/pdf")
        if raw is None:
            pytest.skip("no PDF attachment found in mail archive")
        pdfs = [a for a in iter_attachments(raw)
                if a[1] == "application/pdf"]
        assert pdfs, "iter_attachments should surface the PDF"
        fn, mime, data, text = pdfs[0]
        assert data.startswith(b"%PDF"), f"data not a PDF: {data[:8]!r}"
        # PDFs occasionally extract to nothing (e.g. scanned image PDFs);
        # only assert structure, not text presence.

    def test_real_ics_attachment(self):
        for mime in ("text/calendar", "application/ics"):
            raw = _first_message_with_attachment_of(mime)
            if raw is not None:
                break
        else:
            pytest.skip("no ICS attachment found in mail archive")
        atts = [a for a in iter_attachments(raw) if a[1] in
                ("text/calendar", "application/ics")]
        assert atts
        # All real ICS we've seen contains a VCALENDAR envelope.
        assert any("VCALENDAR" in a[3] or "BEGIN:VEVENT" in a[3] for a in atts)

    def test_real_docx_attachment(self):
        raw = _first_message_with_attachment_of(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if raw is None:
            pytest.skip("no DOCX attachment found in mail archive")
        atts = [a for a in iter_attachments(raw)
                if a[1].endswith("wordprocessingml.document")]
        assert atts
        fn, mime, data, text = atts[0]
        assert data[:2] == b"PK"   # zip magic
        # Real docs almost always have *some* extracted text
        assert len(text) > 0, f"empty extraction from {fn}"
