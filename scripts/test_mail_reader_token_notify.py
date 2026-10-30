"""The mail token is delivered only in a URL fragment, never request logs."""
from __future__ import annotations

from scripts import mail_reader_token_notify as notify


def test_login_link_uses_fragment_and_is_not_printed(monkeypatch, capsys):
    token = "a" * 43
    seen: list[str] = []
    monkeypatch.setattr(notify, "token_for_notification", lambda rotate: token)
    monkeypatch.setattr(notify, "mail_web_url", lambda: "https://server.example/mail")
    monkeypatch.setattr(notify, "signal_note", seen.append)

    assert notify.main([]) == 0
    assert seen == [f"mail reader login link:\nhttps://server.example/mail/login#token={token}"]
    assert "?token=" not in seen[0]
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
