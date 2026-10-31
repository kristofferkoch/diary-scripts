"""Real temporary-notmuch integration for PIPE-01 mail export."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.event_export import export_mail, fetch_raw
from scripts.event_spool import EventSpool

pytestmark = pytest.mark.skipif(shutil.which("notmuch") is None, reason="notmuch is not installed")


def raw_mail(message_id: str, body: str = "body") -> bytes:
    return (
        b"From: Sender <sender@example.test>\r\n"
        b"To: Owner <owner@example.test>\r\n"
        b"Subject: Integration\r\n"
        b"Date: Sat, 31 Oct 2026 10:00:00 +0000\r\n"
        + f"Message-ID: <{message_id}>\r\n".encode()
        + b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + body.encode() + b"\r\n"
    )


class NotmuchSandbox:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.mail = root / "mail"
        self.config = root / "notmuch-config"
        self.home = root / "home"
        self.home.mkdir()
        self.config.write_text(f"[database]\npath={self.mail}\nmail_root={self.mail}\n")
        monkeypatch.setenv("NOTMUCH_CONFIG", str(self.config))
        monkeypatch.setenv("HOME", str(self.home))

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["notmuch", *args], check=True, capture_output=True, text=True, timeout=30,
        )

    def add(self, relative: str, raw: bytes) -> Path:
        path = self.mail / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def new(self) -> None:
        self.run("new")


@pytest.fixture
def notmuch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NotmuchSandbox:
    return NotmuchSandbox(tmp_path, monkeypatch)


@pytest.fixture
def export_paths(tmp_path: Path) -> tuple[Path, Path]:
    spool = tmp_path / "events"
    state = tmp_path / "state"
    spool.mkdir(mode=0o750)
    state.mkdir(mode=0o700)
    return spool, state


def test_real_notmuch_plain_spaced_paths_and_all_mail_copy(notmuch, export_paths):
    plain = raw_mail("plain@example.test", "plain body")
    spaced = raw_mail("spaced@example.test", "spaced body")
    notmuch.add("INBOX/cur/plain.eml", plain)
    notmuch.add("INBOX/cur/message with spaces.eml", spaced)
    notmuch.add("All Mail/cur/exact copy with spaces.eml", plain)
    notmuch.new()
    spool, state = export_paths

    counts = export_mail(spool, state)

    assert counts["written"] == 2, counts
    assert counts["failed"] == counts["retry"] == 0
    events = list(EventSpool(spool))
    assert {event["native_id"] for event in events} == {"plain@example.test", "spaced@example.test"}


def test_real_notmuch_pending_id_follows_filesystem_move_and_transient_retry(
    notmuch, export_paths,
):
    original = notmuch.add("INBOX/cur/moving.eml", raw_mail("moving@example.test", "moving body"))
    notmuch.new()
    spool, state = export_paths
    attempts = []

    def transient_once(path: str) -> bytes:
        attempts.append(path)
        if len(attempts) == 1:
            raise TimeoutError("temporary read failure")
        return fetch_raw(path)

    first = export_mail(spool, state, raw_fetcher=transient_once)
    assert first["retry"] == 1 and first["failed"] == 0

    moved = notmuch.mail / "Archive/cur/moved with spaces.eml"
    moved.parent.mkdir(parents=True, exist_ok=True)
    original.rename(moved)
    notmuch.new()
    notmuch.run("tag", "-inbox", "--", "id:moving@example.test")
    assert notmuch.run("search", "--output=messages", "tag:inbox").stdout == ""

    second = export_mail(spool, state, raw_fetcher=transient_once)

    assert second["written"] == 1, second
    assert second["failed"] == 0
    assert attempts[0] == str(original)
    assert Path(attempts[1]).parent == moved.parent
    assert Path(attempts[1]).name.startswith("moved with spaces.eml:2,")
    event = next(iter(EventSpool(spool)))
    assert event["native_id"] == "moving@example.test"
    assert event["payload"]["body"].strip() == "moving body"


def test_real_notmuch_same_id_different_bodies_and_exact_copy(notmuch, export_paths):
    first = raw_mail("shared@example.test", "first body")
    second = raw_mail("shared@example.test", "different body")
    notmuch.add("INBOX/cur/first.eml", first)
    notmuch.add("INBOX/cur/second with spaces.eml", second)
    notmuch.add("All Mail/cur/first exact copy.eml", first)
    notmuch.new()
    spool, state = export_paths

    counts = export_mail(spool, state)

    assert counts["written"] == 2, counts
    events = list(EventSpool(spool))
    assert len(events) == 2
    assert {event["native_id"] for event in events} == {"shared@example.test"}
    assert {event["payload"]["body"].strip() for event in events} == {"first body", "different body"}
