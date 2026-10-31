"""Tests for direct collector export and durable logical-ID notmuch mail events."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import event_export
from scripts.event_export import (
    MailContentError,
    MailJournal,
    canonical_inbox_files,
    emit_records,
    export_mail,
    mail_payload,
    quarantine_key,
)
from scripts.event_spool import EventSpool, EventValidationError


@pytest.fixture
def spool_path(tmp_path: Path) -> Path:
    path = tmp_path / "events"
    path.mkdir(mode=0o750)
    return path


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "collector-state"
    path.mkdir(mode=0o700)
    return path


def record(kind: str = "event", fetched_at: str = "2026-10-31T10:00:00Z", **data):
    return {"kind": kind, "fetched_at": fetched_at, "data": {"id": "event-1", **data}}


def test_fetched_at_does_not_change_identity_or_duplicate_events(spool_path: Path):
    first = emit_records(spool_path, "spond", [record(heading="Practice")])
    original_files = list(spool_path.glob("*.json"))
    second = emit_records(
        spool_path, "spond", [record(fetched_at="2026-11-01T10:00:00Z", heading="Practice")],
    )
    assert (first, second) == (
        {"written": 1, "duplicate": 0}, {"written": 0, "duplicate": 1},
    )
    assert list(spool_path.glob("*.json")) == original_files
    event = EventSpool(spool_path).read(original_files[0].stem)
    assert "fetched_at" not in event
    assert "fetched_at" not in event["payload"]


def test_actual_update_gets_a_second_event(spool_path: Path):
    emit_records(spool_path, "spond", [record(heading="Practice")])
    counts = emit_records(spool_path, "spond", [record(kind="event", heading="Cancelled")])
    assert counts == {"written": 1, "duplicate": 0}
    assert len(list(EventSpool(spool_path))) == 2


@pytest.mark.parametrize(
    ("source", "kind", "key"),
    [
        ("spond", "chat", "chatId"),
        ("vigilo", "message", "threadUid"),
        ("vigilo", "news", "postId"),
        ("vigilo", "consent", "consentId"),
        ("vigilo", "check_out_note", "noteId"),
        ("skolemelding", "message", "id"),
    ],
)
def test_every_collector_native_id_shape(spool_path: Path, source: str, kind: str, key: str):
    item = {"kind": kind, "fetched_at": "now", "data": {key: "native-1", "body": "text"}}
    assert emit_records(spool_path, source, [item]) == {"written": 1, "duplicate": 0}
    stored = next(iter(EventSpool(spool_path)))
    assert (stored["source"], stored["kind"], stored["native_id"]) == (source, kind, "native-1")


@pytest.mark.parametrize(
    "malformed",
    [None, {}, {"kind": "event", "data": {"id": "x"}},
     {"kind": "event", "fetched_at": 1, "data": {"id": "x"}},
     {"kind": "event", "fetched_at": "now", "data": {"heading": "missing id"}},
     {"kind": "event", "fetched_at": "now", "data": [], "extra": True}],
)
def test_malformed_collector_records_fail_without_writing(spool_path: Path, malformed):
    with pytest.raises(EventValidationError):
        emit_records(spool_path, "spond", [malformed])
    assert list(spool_path.iterdir()) == []


def raw_mail(body: bytes = b"Remember the form.", message_id: str = "shared@example.test") -> bytes:
    return (
        b"From: Sender <sender@example.test>\r\n"
        b"To: Owner <owner@example.test>\r\n"
        b"Subject: Plan\r\n"
        b"Date: Sat, 31 Oct 2026 10:00:00 +0000\r\n"
        + f"Message-ID: <{message_id}>\r\n".encode()
        + b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + body + b"\r\n"
    )


def export_with(
    spool_path: Path, state_dir: Path, messages: dict[str, list[str]], raws: dict[str, bytes],
    discovered: list[str] | None = None, raw_fetcher=None,
):
    return export_mail(
        spool_path, state_dir,
        search_messages=lambda query: list(messages) if discovered is None else discovered,
        message_files=lambda message_id: messages.get(message_id, []),
        raw_fetcher=raw_fetcher or raws.__getitem__,
    )


def test_same_logical_id_with_different_bodies_is_not_collapsed(spool_path: Path, state_dir: Path):
    messages = {"shared@example.test": ["/mail/INBOX/a", "/mail/INBOX/b"]}
    raws = {
        "/mail/INBOX/a": raw_mail(b"first body"),
        "/mail/INBOX/b": raw_mail(b"different body"),
    }
    counts = export_with(spool_path, state_dir, messages, raws)
    assert counts["written"] == 2
    events = list(EventSpool(spool_path))
    assert len({event["native_id"] for event in events}) == 1
    assert len({event["event_id"] for event in events}) == 2
    assert {event["payload"]["body"].strip() for event in events} == {"first body", "different body"}


def test_inbox_and_all_mail_exact_copy_produces_one_event(spool_path: Path, state_dir: Path):
    raw = raw_mail()
    messages = {"shared@example.test": ["/mail/All Mail/copy", "/mail/INBOX/original"]}
    raws = {"/mail/All Mail/copy": raw, "/mail/INBOX/original": raw}
    counts = export_with(spool_path, state_dir, messages, raws)
    assert counts["written"] == 1
    assert len(list(EventSpool(spool_path))) == 1
    assert canonical_inbox_files(messages["shared@example.test"]) == ["/mail/INBOX/original"]


def test_completed_mail_remains_duplicate_after_restart(spool_path: Path, state_dir: Path):
    messages = {"shared@example.test": ["/mail/INBOX/a"]}
    raws = {"/mail/INBOX/a": raw_mail()}
    first = export_with(spool_path, state_dir, messages, raws)
    second = export_with(spool_path, state_dir, messages, raws)
    assert first["written"] == 1 and second["duplicate"] == 1
    assert len(list(EventSpool(spool_path))) == 1


def test_pending_logical_id_follows_archive_move_after_crash(
    spool_path: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch,
):
    original_save = MailJournal.save
    injected = {"done": False}

    def crash_after_write(self: MailJournal) -> None:
        if self.completed and not injected["done"]:
            injected["done"] = True
            raise OSError("simulated crash before journal commit")
        original_save(self)

    monkeypatch.setattr(MailJournal, "save", crash_after_write)
    messages = {"shared@example.test": ["/mail/INBOX/a"]}
    raws = {"/mail/INBOX/a": raw_mail(), "/mail/Archive/moved": raw_mail()}
    with pytest.raises(OSError, match="simulated crash"):
        export_with(spool_path, state_dir, messages, raws)
    monkeypatch.setattr(MailJournal, "save", original_save)
    messages["shared@example.test"] = ["/mail/Archive/moved"]
    counts = export_with(spool_path, state_dir, messages, raws, discovered=[])
    assert counts["duplicate"] == 1
    journal = MailJournal(state_dir / "mail-event-journal.json")
    assert not journal.pending
    assert list(journal.completed) == ["shared@example.test"]


def test_transient_read_failure_stays_pending_then_succeeds(spool_path: Path, state_dir: Path):
    messages = {"shared@example.test": ["/mail/INBOX/a"]}
    attempts = []

    def flaky(path: str) -> bytes:
        attempts.append(path)
        if len(attempts) == 1:
            raise TimeoutError("temporary database timeout")
        return raw_mail()

    first = export_with(spool_path, state_dir, messages, {}, raw_fetcher=flaky)
    assert first["retry"] == 1 and first["failed"] == 0
    assert list(MailJournal(state_dir / "mail-event-journal.json").pending) == ["shared@example.test"]
    second = export_with(spool_path, state_dir, messages, {}, discovered=[], raw_fetcher=flaky)
    assert second["written"] == 1
    assert attempts == ["/mail/INBOX/a", "/mail/INBOX/a"]
    assert not MailJournal(state_dir / "mail-event-journal.json").pending


def test_deterministic_bad_message_is_quarantined_without_blocking_or_repeating(
    spool_path: Path, state_dir: Path,
):
    messages = {
        "bad@example.test": ["/mail/INBOX/bad"],
        "good@example.test": ["/mail/INBOX/good"],
    }
    fetches = []

    def fetch(path: str) -> bytes:
        fetches.append(path)
        if path.endswith("bad"):
            raise MailContentError("malformed raw message")
        return raw_mail(message_id="good@example.test")

    first = export_with(spool_path, state_dir, messages, {}, raw_fetcher=fetch)
    assert first["failed"] == 1 and first["written"] == 1
    second = export_with(spool_path, state_dir, messages, {}, raw_fetcher=fetch)
    assert second["quarantined"] == 1 and second["duplicate"] == 1
    assert fetches.count("/mail/INBOX/bad") == 1
    assert fetches.count("/mail/INBOX/good") == 2
    assert list(MailJournal(state_dir / "mail-event-journal.json").failed) == [
        quarantine_key("bad@example.test", "/mail/INBOX/bad")
    ]


def test_direct_raw_reader_handles_spaces_and_enforces_limit(tmp_path: Path, monkeypatch):
    path = tmp_path / "mail with spaces.eml"
    path.write_bytes(raw_mail())
    assert event_export.fetch_raw(str(path)) == raw_mail()
    monkeypatch.setattr(event_export, "MAX_MAIL_BYTES", 1024)
    path.write_bytes(b"x" * 1025)
    with pytest.raises(MailContentError, match="exceeds"):
        event_export.fetch_raw(str(path))


def test_direct_raw_reader_rejects_symlink(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(raw_mail())
    path = tmp_path / "linked"
    path.symlink_to(target)
    with pytest.raises(OSError):
        event_export.fetch_raw(str(path))


def test_mail_body_truncation_is_deterministic(spool_path: Path, state_dir: Path):
    raw = raw_mail(b"x" * (256 * 1024 + 100))
    export_with(
        spool_path, state_dir,
        {"shared@example.test": ["/mail/INBOX/a"]}, {"/mail/INBOX/a": raw},
    )
    stored = next(iter(EventSpool(spool_path)))
    assert stored["payload"]["body"].endswith("[body truncated by trusted collector]")


def test_malformed_or_legacy_journal_fails_closed(state_dir: Path):
    (state_dir / "mail-event-journal.json").write_text('{"version":1,"pending":{}}')
    with pytest.raises(EventValidationError, match="journal"):
        MailJournal(state_dir / "mail-event-journal.json")


def test_mail_payload_distills_headers_body_and_attachments():
    raw = (
        b"From: s@example.test\r\nSubject: HTML\r\nMessage-ID: <id@example.test>\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/html\r\n\r\n<p>Hello &amp; goodbye</p>\r\n"
        b"--x\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=plan.pdf\r\n\r\n%PDF\r\n--x--\r\n"
    )
    payload = mail_payload(raw)
    assert payload["message_id"] == "<id@example.test>"
    assert payload["body"].strip() == "Hello & goodbye"
    assert payload["attachments"] == [{"filename": "plan.pdf", "content_type": "application/pdf"}]


def _legacy_paths(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "today_jsonl", lambda: tmp_path / "legacy.jsonl")


def test_spond_cli_emits_fresh_records_before_legacy_state(spool_path, tmp_path, monkeypatch):
    from scripts import spond_sync

    async def fresh(kinds, state):
        return 1, [record(heading="Practice")], {"event": 1}

    _legacy_paths(spond_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(spond_sync, "run_once", fresh)
    assert spond_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "spond"
    assert (tmp_path / "legacy.jsonl").exists()


def test_vigilo_cli_emits_fresh_records_before_legacy_state(spool_path, tmp_path, monkeypatch):
    from scripts import vigilo_sync
    fresh_record = {"kind": "message", "fetched_at": "now", "data": {"threadUid": "t", "body": "b"}}
    _legacy_paths(vigilo_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(vigilo_sync, "run_once", lambda kinds, state: (1, [fresh_record], {"message": 1}))
    assert vigilo_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "vigilo"
    assert (tmp_path / "legacy.jsonl").exists()


def test_skolemelding_cli_emits_fresh_records_before_legacy_state(spool_path, tmp_path, monkeypatch):
    from scripts import skolemelding_sync
    _legacy_paths(skolemelding_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(skolemelding_sync, "run_once", lambda state: (1, [record(kind="message")], {"message": 1}))
    assert skolemelding_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "skolemelding"
    assert (tmp_path / "legacy.jsonl").exists()
