"""Tests for the web-token notifier's URL rewriting.

The two UIs carry the token differently — dsh-web as a query string
(`?token=`), kimi-web as a URL fragment (`#token=`) — and the fragment is the
easy one to drop when the authority is swapped for the public base.
"""
from __future__ import annotations

from scripts.dsh_web_notify import TOKEN_RE, public_url


def test_query_token_survives_rewrite():
    assert public_url("http://127.0.0.1:8802/?token=abc", "https://n:8444") == \
        "https://n:8444/?token=abc"


def test_fragment_token_survives_rewrite():
    assert public_url("http://127.0.0.1:8803/#token=abc", "https://n:8446") == \
        "https://n:8446/#token=abc"


def test_regex_matches_both_token_forms():
    assert TOKEN_RE.findall("Local: http://127.0.0.1:8802/?token=a b") == \
        ["http://127.0.0.1:8802/?token=a"]
    assert TOKEN_RE.findall("Local: http://127.0.0.1:8803/#token=b") == \
        ["http://127.0.0.1:8803/#token=b"]
