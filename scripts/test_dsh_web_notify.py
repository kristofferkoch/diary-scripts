"""Tests for the web-token notifier's URL rewriting.

The two token-based UIs carry the token differently — dsh-web as a query
string (`?token=`), kimi-web as a URL fragment (`#token=`) — and the fragment
is the easy one to drop when the authority is swapped for the public base.
opencode-web has no token at all: it uses HTTP Basic with a static password
read from the unit's env file, so it is covered separately.
"""
from __future__ import annotations

from urllib import error as urlerror
from urllib import request as urlrequest

from scripts.dsh_web_notify import (
    BASIC_AUTH_USER,
    TOKEN_RE,
    basic_auth_message,
    env_value,
    public_url,
    wait_for_http,
)


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


def test_env_value_last_wins_and_ignores_comments(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# comment line\n"
        "OPENCODE_SERVER_PASSWORD=first\n"
        "OTHER=1\n"
        'OPENCODE_SERVER_PASSWORD="second"\n'
    )
    assert env_value(str(env), "OPENCODE_SERVER_PASSWORD") == "second"
    assert env_value(str(env), "MISSING") == ""


def test_basic_auth_message_carries_link_and_credentials():
    message = basic_auth_message("opencode web", "https://server.example:8447",
                                 BASIC_AUTH_USER, "s3cret")
    lines = message.splitlines()
    assert lines[0] == "opencode web (re)started — login from phone:"
    assert lines[1] == "https://server.example:8447/"
    assert "user: opencode" in lines[2]
    assert "password: s3cret" in lines[2]


def test_wait_for_http_treats_401_as_ready(monkeypatch):
    """401 = listening and enforcing auth, which is what we want to report."""
    def unauthorised(*_args, **_kwargs):
        raise urlerror.HTTPError("http://localhost:8804/", 401,
                                 "Unauthorized", {}, None)

    monkeypatch.setattr(urlrequest, "urlopen", unauthorised)
    assert wait_for_http("http://localhost:8804/", timeout=1) is True


def test_wait_for_http_times_out_when_not_listening(monkeypatch):
    def refused(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urlrequest, "urlopen", refused)
    assert wait_for_http("http://localhost:8804/", timeout=0.05,
                         poll=0.01) is False
