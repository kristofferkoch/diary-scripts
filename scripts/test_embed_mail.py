"""Tests for embed_mail.py. Run with `uv run pytest`."""
from __future__ import annotations

import email
from email.policy import default as email_default

from embed_mail import (
    CHUNK_CHARS,
    chunk_text,
    parse_message,
    pick_date,
    strip_quotes_sig,
    tier_query,
)


def _msg(headers: dict[str, list[str]], body: str = "body") -> email.message.Message:
    parts = []
    for k, vs in headers.items():
        for v in vs:
            parts.append(f"{k}: {v}")
    raw = ("\n".join(parts) + "\n\n" + body).encode()
    return email.message_from_bytes(raw, policy=email_default)


def test_pick_date_skips_implausible_first_header():
    # The bug we hit IRL: spammy mail with two Date: headers, the first one
    # is garbage (year 2270). Must skip it and use the plausible one.
    m = _msg({"Date": [
        "Tue, 04 Jan 2270 08:35:50 +0000",
        "Sat, 1 Jan 2000 19:15:35 +0000",
    ]})
    assert pick_date(m) == "2000-01-01T19:15:35+00:00"


def test_pick_date_returns_none_when_all_implausible():
    m = _msg({"Date": ["Tue, 04 Jan 2270 08:35:50 +0000"]})
    assert pick_date(m) is None


def test_pick_date_handles_naive_datetime_as_utc():
    # Some mail clients emit "-0000" / no offset; parsedate returns naive.
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
    # Reassembling all chunks must cover every char of the input (modulo
    # overlap repetition + the strip()-trimmed edges).
    joined = "".join(chunks)
    assert len(joined) >= len(text.strip())


def test_chunk_text_snaps_to_blank_line_when_available():
    # Force a blank line inside the snap window so the chunker prefers it.
    head = "a" * (CHUNK_CHARS - 100)
    text = head + "\n\nTAIL" + ("z" * 3000)
    chunks = chunk_text(text)
    # First chunk should end at the blank line, not mid-stream.
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
