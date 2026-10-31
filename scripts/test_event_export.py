"""Tests for direct collector export and durable, bounded notmuch mail events."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import event_export
from scripts.event_export import MailJournal, emit_records, export_mail, mail_payload
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
    [
        None,
        {},
        {"kind": "event", "data": {"id": "x"}},
        {"kind": "event", "fetched_at": 1, "data": {"id": "x"}},
        {"kind": "event", "fetched_at": "now", "data": {"heading": "missing id"}},
        {"kind": "event", "fetched_at": "now", "data": [], "extra": True},
    ],
)
def test_malformed_collector_records_fail_without_writing(spool_path: Path, malformed):
    with pytest.raises(EventValidationError):
        emit_records(spool_path, "spond", [malformed])
    assert list(spool_path.iterdir()) == []


def test_malformed_record_does_not_get_silently_reordered_or_skipped(spool_path: Path):
    good = record()
    malformed = record()
    del malformed["data"]["id"]
    with pytest.raises(EventValidationError):
        emit_records(spool_path, "spond", [good, malformed, good])
    events = list(EventSpool(spool_path))
    assert len(events) == 1
    assert events[0]["payload"] == good["data"]


def raw_mail(body: bytes = b"Remember the form.") -> bytes:
    return (
        b"From: Sender <sender@example.test>\r\n"
        b"To: Owner <owner@example.test>\r\n"
        b"Subject: Plan\r\n"
        b"Date: Sat, 31 Oct 2026 10:00:00 +0000\r\n"
        b"Message-ID: <shared-id@example.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + body + b"\r\n"
    )


def test_mail_payload_distills_headers_body_and_attachments():
    raw = (
        b"From: s@example.test\r\nSubject: HTML\r\n"
        b"Message-ID: <id@example.test>\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/html\r\n\r\n<p>Hello &amp; goodbye</p>\r\n"
        b"--x\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=plan.pdf\r\n\r\n%PDF\r\n--x--\r\n"
    )
    payload = mail_payload(raw)
    assert payload["message_id"] == "<id@example.test>"
    assert payload["body"].strip() == "Hello & goodbye"
    assert payload["attachments"] == [{"filename": "plan.pdf", "content_type": "application/pdf"}]


def test_same_message_id_with_different_files_and_bodies_is_not_collapsed(
    spool_path: Path, state_dir: Path,
):
    raws = {"/mail/a": raw_mail(b"first body"), "/mail/b": raw_mail(b"different body")}
    counts = export_mail(
        spool_path, state_dir,
        search_files=lambda query: list(raws), raw_fetcher=raws.__getitem__,
    )
    assert counts["written"] == 2
    events = list(EventSpool(spool_path))
    assert len(events) == 2
    assert len({event["event_id"] for event in events}) == 2
    assert {event["payload"]["message_id"] for event in events} == {"<shared-id@example.test>"}
    assert {event["payload"]["body"].strip() for event in events} == {"first body", "different body"}


def test_mail_restart_skips_completed_raw_reread(
    spool_path: Path, state_dir: Path,
):
    fetches = []

    def fetch(mail_path: str) -> bytes:
        fetches.append(mail_path)
        return raw_mail()

    first = export_mail(
        spool_path, state_dir,
        search_files=lambda query: ["/mail/a"], raw_fetcher=fetch,
    )
    path = next(spool_path.glob("*.json"))
    original = path.read_bytes()
    second = export_mail(
        spool_path, state_dir,
        search_files=lambda query: ["/mail/a"], raw_fetcher=fetch,
    )
    assert first["written"] == 1 and second["duplicate"] == 1
    assert fetches == ["/mail/a"]
    assert path.read_bytes() == original


def test_pending_archived_mail_survives_crash_after_durable_spool_write(
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
    with pytest.raises(OSError, match="simulated crash"):
        export_mail(
            spool_path, state_dir,
            search_files=lambda query: ["/mail/archived"], raw_fetcher=lambda path: raw_mail(),
        )
    assert len(list(EventSpool(spool_path))) == 1
    monkeypatch.setattr(MailJournal, "save", original_save)
    counts = export_mail(
        spool_path, state_dir,
        search_files=lambda query: [], raw_fetcher=lambda path: raw_mail(),
    )
    assert counts["duplicate"] == 1
    journal = MailJournal(state_dir / "mail-event-journal.json")
    assert not journal.pending
    assert list(journal.completed) == ["/mail/archived"]


def test_one_bad_message_is_quarantined_without_blocking_or_repeating(
    spool_path: Path, state_dir: Path,
):
    fetches = []

    def fetch(mail_path: str) -> bytes:
        fetches.append(mail_path)
        if mail_path == "/mail/bad":
            raise EventValidationError("malformed raw message")
        return raw_mail()

    first = export_mail(
        spool_path, state_dir,
        search_files=lambda query: ["/mail/bad", "/mail/good"], raw_fetcher=fetch,
    )
    assert first["failed"] == 1 and first["written"] == 1
    assert len(list(EventSpool(spool_path))) == 1
    second = export_mail(
        spool_path, state_dir,
        search_files=lambda query: ["/mail/bad", "/mail/good"], raw_fetcher=fetch,
    )
    assert second["quarantined"] == 1 and second["duplicate"] == 1
    assert fetches == ["/mail/bad", "/mail/good"]
    assert list(MailJournal(state_dir / "mail-event-journal.json").failed) == ["/mail/bad"]


def test_bounded_raw_reader_terminates_instead_of_full_buffering(monkeypatch: pytest.MonkeyPatch):
    class Stream:
        def __init__(self):
            self.total = 0
            self.requests = []

        def read(self, size: int) -> bytes:
            assert size > 0
            self.requests.append(size)
            self.total += size
            return b"x" * size

    class Proc:
        def __init__(self):
            self.stdout = Stream()
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self, timeout: int):
            return 0

        def poll(self):
            return 0 if self.killed else None

    proc = Proc()
    monkeypatch.setattr(event_export, "MAX_MAIL_BYTES", 1024)
    monkeypatch.setattr(event_export.subprocess, "Popen", lambda *args, **kwargs: proc)
    with pytest.raises(EventValidationError, match="raw mail exceeds"):
        event_export.fetch_raw("/mail/large")
    assert proc.killed
    assert proc.stdout.total == 1025
    assert proc.stdout.requests == [1025]


def test_mail_body_truncation_is_deterministic(spool_path: Path, state_dir: Path):
    raw = raw_mail(b"x" * (256 * 1024 + 100))
    export_mail(
        spool_path, state_dir,
        search_files=lambda query: ["/mail/a"], raw_fetcher=lambda path: raw,
    )
    stored = next(iter(EventSpool(spool_path)))
    assert stored["payload"]["body"].endswith("[body truncated by trusted collector]")
    assert len(stored["payload"]["body"]) == 256 * 1024 + len("\n[body truncated by trusted collector]")


def test_malformed_journal_fails_closed(state_dir: Path):
    (state_dir / "mail-event-journal.json").write_text('{"version":1,"pending":{}}')
    with pytest.raises(EventValidationError, match="journal"):
        MailJournal(state_dir / "mail-event-journal.json")


def test_spool_json_is_canonical_and_has_no_collection_timestamp(spool_path: Path):
    emit_records(spool_path, "spond", [record(heading="Practice")])
    path = next(spool_path.glob("*.json"))
    data = path.read_bytes()
    event = json.loads(data)
    assert data == json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode() + b"\n"
    assert set(event) == {"schema_version", "event_id", "source", "kind", "native_id", "revision", "payload"}


def _legacy_paths(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "today_jsonl", lambda: tmp_path / "legacy.jsonl")


def test_spond_cli_emits_fresh_records_before_legacy_state(
    spool_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from scripts import spond_sync

    async def fresh(kinds, state):
        return 1, [record(heading="Practice")], {"event": 1}

    _legacy_paths(spond_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(spond_sync, "run_once", fresh)
    assert spond_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "spond"
    assert (tmp_path / "legacy.jsonl").exists()
    assert (tmp_path / "state.json").exists()


def test_vigilo_cli_emits_fresh_records_before_legacy_state(
    spool_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from scripts import vigilo_sync

    fresh_record = {
        "kind": "message", "fetched_at": "now",
        "data": {"threadUid": "thread-1", "body": "Reply"},
    }
    _legacy_paths(vigilo_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(
        vigilo_sync, "run_once", lambda kinds, state: (1, [fresh_record], {"message": 1}),
    )
    assert vigilo_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "vigilo"
    assert (tmp_path / "legacy.jsonl").exists()
    assert (tmp_path / "state.json").exists()


def test_skolemelding_cli_emits_fresh_records_before_legacy_state(
    spool_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from scripts import skolemelding_sync

    _legacy_paths(skolemelding_sync, tmp_path, monkeypatch)
    monkeypatch.setattr(
        skolemelding_sync, "run_once", lambda state: (1, [record(kind="message")], {"message": 1}),
    )
    assert skolemelding_sync.main(["--once", "--event-spool", str(spool_path)]) == 0
    assert next(iter(EventSpool(spool_path)))["source"] == "skolemelding"
    assert (tmp_path / "legacy.jsonl").exists()
    assert (tmp_path / "state.json").exists()
