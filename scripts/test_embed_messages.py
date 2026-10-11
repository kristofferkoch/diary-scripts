"""Tests for embed_messages.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

import contextlib
from typing import Any, cast

import psycopg

from scripts.embed_mail import AttachmentPayload
from scripts.embed_messages import (
    SourcePayload,
    _content_hash,
    decide,
    signal_payload,
    spond_payload,
    vigilo_payload,
)


def _must(p: SourcePayload | None) -> SourcePayload:
    """Narrow a builder result for the type checker (tests only build
    payloads from records they know are recordable)."""
    assert p is not None
    return p


# ---------- signal ----------

def _signal_rec(**over):
    rec = {
        "kind": "message",
        "direction": "in",
        "ts": 1780835700497,
        "iso": "2026-06-07T12:35:00Z",
        "fetched_at": "2026-06-07T13:00:00Z",
        "from": {"name": "Alice", "number": "+4700000000", "uuid": "u1"},
        "to": None,
        "group": None,
        "text": "see you at practice",
        "quote": None,
        "attachments": [],
    }
    rec.update(over)
    return rec


class TestSignalPayload:
    def test_mid_shape_and_stability(self):
        p = _must(signal_payload(_signal_rec()))
        assert p["mid"].startswith("signal:1780835700497:")
        suffix = p["mid"].rsplit(":", 1)[1]
        assert len(suffix) == 12
        assert _must(signal_payload(_signal_rec()))["mid"] == p["mid"]
        # A different text at the same timestamp must not collide.
        assert _must(signal_payload(_signal_rec(text="other")))["mid"] != p["mid"]

    def test_incoming_peer_mapping(self):
        p = _must(signal_payload(_signal_rec()))
        assert p["hdr"]["from"] == "Alice"
        assert p["hdr"]["subject"] == "Alice"   # 1:1 conversation label
        assert p["tid"] == "signal:peer:+4700000000"
        assert p["hdr"]["date"] == "2026-06-07T12:35:00Z"

    def test_group_mapping(self):
        p = _must(signal_payload(_signal_rec(
            group={"id": "g1", "name": "Example Club"})))
        assert p["hdr"]["subject"] == "Example Club"
        assert p["hdr"]["to"] == "Example Club"
        assert p["tid"] == "signal:group:g1"

    def test_sent_uses_destination(self):
        p = _must(signal_payload(_signal_rec(
            kind="sent", direction="out",
            to={"name": "Bob", "number": "+4700000001"})))
        assert p["hdr"]["subject"] == "Bob"
        assert p["tid"] == "signal:peer:+4700000001"

    def test_quote_folds_in(self):
        p = _must(signal_payload(_signal_rec(
            quote={"author": "+4700000000", "text": "earlier message"})))
        assert p["body_chunks"][0].endswith("> +4700000000: earlier message")
        # And the quote participates in the change-detection hash.
        assert p["content_hash"] != _must(signal_payload(_signal_rec()))["content_hash"]

    def test_empty_record_skipped(self):
        assert signal_payload(_signal_rec(text="", attachments=[])) is None
        assert signal_payload(_signal_rec(text=None, attachments=[])) is None

    def test_attachment_only_record_kept(self):
        p = _must(signal_payload(_signal_rec(text="", attachments=[
            {"contentType": "image/jpeg", "filename": "photo.jpg",
             "size": 1234, "id": "no-such-blob"}])))
        assert p["body_chunks"] == []
        att = p["attachments"][0]
        assert att["filename"] == "photo.jpg"
        assert att["text_chars"] == 0    # images stay metadata-only

    def test_extractable_attachment_reads_blob(self, tmp_path, monkeypatch):
        (tmp_path / "blob-1").write_text("minutes from the board meeting")
        monkeypatch.setattr("scripts.embed_messages.SIGNAL_ATTACH_DIR", tmp_path)
        p = _must(signal_payload(_signal_rec(text="attached", attachments=[
            {"contentType": "text/plain", "filename": "minutes.txt",
             "size": 29, "id": "blob-1"}])))
        att = p["attachments"][0]
        assert att["text_chars"] > 0
        assert any("board meeting" in c for c in att["chunks"])

    def test_extractable_mime_but_missing_blob_is_metadata_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("scripts.embed_messages.SIGNAL_ATTACH_DIR", tmp_path)
        p = _must(signal_payload(_signal_rec(attachments=[
            {"contentType": "application/pdf", "filename": "doc.pdf",
             "size": 99, "id": "gone"}])))
        assert p["attachments"][0]["text_chars"] == 0
        assert p["attachments"][0]["chunks"] == []

    def test_unextractable_blob_never_read(self, tmp_path, monkeypatch):
        # A large video blob must not be read just to extract nothing.
        blob = tmp_path / "vid-1"
        blob.write_bytes(b"\x00" * 64)
        monkeypatch.setattr("scripts.embed_messages.SIGNAL_ATTACH_DIR", tmp_path)
        p = _must(signal_payload(_signal_rec(attachments=[
            {"contentType": "video/mp4", "filename": "clip.mp4",
             "size": 64, "id": "vid-1"}])))
        assert p["attachments"][0]["text_chars"] == 0


# ---------- spond ----------

class TestSpondPayload:
    def test_chat(self):
        p = _must(spond_payload({
            "kind": "chat", "fetched_at": "2026-05-26T11:30:00Z",
            "data": {"id": "c1", "name": "Team chat",
                     "newestTimestamp": "2026-05-26T11:00:00Z",
                     "message": {"type": "TEXT", "text": "see you there"}}}))
        assert p["mid"] == "spond:chat:c1"
        assert p["hdr"]["subject"] == "Team chat"
        assert p["hdr"]["date"] == "2026-05-26T11:00:00Z"
        assert p["body_chunks"] == ["Team chat\nsee you there"]

    def test_chat_rename_uses_new_name(self):
        p = _must(spond_payload({
            "kind": "chat", "fetched_at": "f",
            "data": {"id": "c1", "name": "Old name",
                     "message": {"type": "RENAME", "newName": "New name"}}}))
        assert "New name" in p["body_chunks"][0]

    def test_chat_without_message_text_skipped(self):
        assert spond_payload({
            "kind": "chat",
            "data": {"id": "c1", "name": "x", "message": {}}}) is None

    def test_event_text_assembly(self):
        p = _must(spond_payload({
            "kind": "event", "fetched_at": "2026-05-26T11:30:00Z",
            "data": {"id": "e1", "heading": "Cup match",
                     "description": "Bring boots and bottle",
                     "location": {"feature": "Example Stadium"},
                     "startTimestamp": "2026-05-30T08:30:00Z",
                     "responses": {"acceptedIds": ["A", "B"],
                                   "declinedIds": ["C"],
                                   "unansweredIds": ["D", "E", "F"]}}}))
        assert p["mid"] == "spond:event:e1"
        assert p["hdr"]["subject"] == "Cup match"
        assert p["hdr"]["date"] == "2026-05-30T08:30:00Z"
        text = p["body_chunks"][0]
        assert "Bring boots and bottle" in text
        assert "Example Stadium" in text
        assert "accepted 2, declined 1, unanswered 3" in text

    def test_event_cancelled_marked(self):
        p = _must(spond_payload({
            "kind": "event",
            "data": {"id": "e1", "heading": "Training", "cancelled": True}}))
        assert "Cancelled" in p["body_chunks"][0]

    def test_event_rsvp_change_flips_hash(self):
        base = _must(spond_payload({"kind": "event",
                                    "data": {"id": "e1", "heading": "Training",
                                             "responses": {"unansweredIds": ["A"]}}}))
        changed = _must(spond_payload({"kind": "event",
                                       "data": {"id": "e1", "heading": "Training",
                                                "responses": {"acceptedIds": ["A"]}}}))
        assert base["mid"] == changed["mid"]
        assert base["content_hash"] != changed["content_hash"]

    def test_post(self):
        p = _must(spond_payload({
            "kind": "post", "fetched_at": "f",
            "data": {"id": "p1", "title": "Info night",
                     "body": "Thursday at 18:00",
                     "timestamp": "2026-05-20T09:00:00Z"}}))
        assert p["mid"] == "spond:post:p1"
        assert p["hdr"]["date"] == "2026-05-20T09:00:00Z"
        assert "Thursday at 18:00" in p["body_chunks"][0]

    def test_missing_id_skipped(self):
        for kind in ("chat", "event", "post"):
            assert spond_payload({"kind": kind, "data": {}}) is None

    def test_unknown_kind_skipped(self):
        assert spond_payload({"kind": "weird", "data": {"id": "x"}}) is None


# ---------- vigilo ----------

def _vigilo_thread(**over):
    data = {
        "threadUid": "t1",
        "unit": "Example AKS",
        "title": "Pickup times",
        "childId": "child-1",
        "messages": [
            {"sender": "Alice", "timeCreated": "2026-10-01T07:00:00Z",
             "body": "New hours from Monday", "attachments": []},
            {"sender": "Bob", "timeCreated": "2026-10-02T09:00:00Z",
             "body": "Thanks, noted", "attachments": []},
        ],
    }
    data.update(over)
    return {"kind": "message", "fetched_at": "2026-10-02T10:00:00Z",
            "data": data}


class TestVigiloPayload:
    def test_thread_text_and_mapping(self):
        p = _must(vigilo_payload(_vigilo_thread()))
        assert p["mid"] == "vigilo:message:t1"
        assert p["hdr"]["subject"] == "Pickup times"
        assert p["hdr"]["from"] == "Bob"           # last sender
        assert p["hdr"]["to"] == "Example AKS"
        assert p["hdr"]["date"] == "2026-10-02T09:00:00Z"   # newest message
        text = p["body_chunks"][0]
        assert "--- Alice (2026-10-01T07:00:00Z) ---" in text
        assert "New hours from Monday" in text
        assert "Thanks, noted" in text

    def test_message_update_collapses_to_same_row(self):
        original = _must(vigilo_payload(_vigilo_thread()))
        update_rec = _vigilo_thread(
            messages=[*_vigilo_thread()["data"]["messages"],
                      {"sender": "Alice", "timeCreated": "2026-10-03T08:00:00Z",
                       "body": "One more thing", "attachments": []}])
        update_rec["kind"] = "message_update"
        updated = _must(vigilo_payload(update_rec))
        # An update re-emits the whole thread: it upserts over the original
        # row (same message_id) instead of duplicating it.
        assert updated["mid"] == original["mid"]
        assert updated["content_hash"] != original["content_hash"]

    def test_thread_attachment_extracted_from_recorded_path(self, tmp_path):
        blob = tmp_path / "attachments" / "t1" / "Uke_42.txt"
        blob.parent.mkdir(parents=True)
        blob.write_text("weekly plan for week 42")
        rec = _vigilo_thread(messages=[
            {"sender": "Alice", "timeCreated": "2026-10-01T07:00:00Z",
             "body": "Plan attached",
             "attachments": [{"name": "Uke 42.txt", "mimeType": "text/plain",
                              "path": str(blob)}]}])
        p = _must(vigilo_payload(rec))
        att = p["attachments"][0]
        assert att["filename"] == "Uke 42.txt"
        assert att["text_chars"] > 0
        assert any("week 42" in c for c in att["chunks"])

    def test_thread_attachment_missing_file_is_metadata_only(self, tmp_path):
        rec = _vigilo_thread(messages=[
            {"sender": "Alice", "timeCreated": "2026-10-01T07:00:00Z",
             "body": "Plan attached",
             "attachments": [{"name": "gone.txt", "mimeType": "text/plain",
                              "path": str(tmp_path / "gone.txt")}]}])
        p = _must(vigilo_payload(rec))
        assert p["attachments"][0]["text_chars"] == 0

    def test_news(self):
        p = _must(vigilo_payload({
            "kind": "news", "fetched_at": "f",
            "data": {"postId": "n1", "title": "Ukeplan",
                     "created": "2026-10-01T06:00:00Z",
                     "body": "Swimming on Tuesday",
                     "attachments": [{"id": "a1", "name": "plan.pdf",
                                      "mimeType": "application/pdf"}]}}))
        assert p["mid"] == "vigilo:news:n1"
        assert p["hdr"]["date"] == "2026-10-01T06:00:00Z"
        assert "Swimming on Tuesday" in p["body_chunks"][0]
        # News attachments have no local file — metadata-only.
        assert p["attachments"][0]["text_chars"] == 0

    def test_consent(self):
        p = _must(vigilo_payload({
            "kind": "consent", "fetched_at": "2026-10-01T06:00:00Z",
            "data": {"consentId": "cf1", "unit": "Example AKS",
                     "title": "Photo consent", "status": "pending",
                     "deadline": "2026-10-15", "body": "Please answer"}}))
        assert p["mid"] == "vigilo:consent:cf1"
        text = p["body_chunks"][0]
        assert "Please answer" in text
        assert "Status: pending, deadline: 2026-10-15" in text

    def test_missing_ids_skipped(self):
        assert vigilo_payload({"kind": "message", "data": {}}) is None
        assert vigilo_payload({"kind": "news", "data": {}}) is None
        assert vigilo_payload({"kind": "consent", "data": {}}) is None


# ---------- content hash + upsert decision ----------

class TestContentHash:
    def test_attachment_text_participates(self):
        base: list[AttachmentPayload] = [
            {"filename": "a.txt", "mime": "text/plain", "size": 1,
             "text_chars": 5, "chunks": ["hello"]}]
        changed: list[AttachmentPayload] = [
            {"filename": "a.txt", "mime": "text/plain", "size": 1,
             "text_chars": 7, "chunks": ["hello!!"]}]
        assert _content_hash("body", base) != _content_hash("body", changed)

    def test_stable(self):
        atts: list[AttachmentPayload] = [
            {"filename": "a.txt", "mime": "text/plain", "size": 1,
             "text_chars": 5, "chunks": ["hello"]}]
        assert _content_hash("body", atts) == _content_hash("body", atts)


class TestDecide:
    def test_insert_when_absent(self):
        assert decide(None, "abc") == "insert"

    def test_skip_on_same_hash(self):
        assert decide("abc", "abc") == "skip"

    def test_replace_on_different_hash(self):
        assert decide("old", "abc") == "replace"


# ---------- process-level upsert (multi-version records in one scan) ----------

class _FakeDb:
    """In-memory stand-in for the messages table, enforcing the
    message_id UNIQUE constraint like Postgres does."""
    def __init__(self, hashes: dict[str, str] | None = None):
        self.hashes = dict(hashes or {})   # message_id -> content_hash
        self.next_id = 0
        self.deletes: list[str] = []
        self.inserts: list[str] = []


class _FakeCursor:
    def __init__(self, db: _FakeDb):
        self.db = db
        self._row: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params: tuple[Any, ...] = ()):
        s = " ".join(str(sql).split())
        if s.startswith("SELECT content_hash"):
            h = self.db.hashes.get(params[0])
            self._row = (h,) if h is not None else None
        elif s.startswith("DELETE FROM messages"):
            self.db.deletes.append(params[0])
            self.db.hashes.pop(params[0], None)
        elif s.startswith("INSERT INTO messages"):
            mid, content_hash = params[0], params[-1]
            if mid in self.db.hashes:
                raise psycopg.errors.UniqueViolation(
                    f'duplicate key value violates unique constraint ({mid})')
            self.db.next_id += 1
            self.db.hashes[mid] = content_hash
            self.db.inserts.append(mid)
            self._row = (self.db.next_id,)
        elif s.startswith("INSERT INTO attachments"):
            self._row = (1,)
        # chunk inserts: no-op

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, db: _FakeDb):
        self.db = db

    def cursor(self):
        return _FakeCursor(self.db)

    def transaction(self):
        return contextlib.nullcontext()


def _spond_event(responses: dict) -> dict:
    return {"kind": "event", "fetched_at": "2026-05-26T11:30:00Z",
            "data": {"id": "e1", "heading": "Training",
                     "startTimestamp": "2026-05-30T08:30:00Z",
                     "responses": responses}}


def _fake_embed_pending(batch):
    return {mi: {("body", ci): [0.1, 0.2]
                 for ci in range(len(p["body_chunks"]))}
            for mi, p in enumerate(batch)}


class TestProcessMultiVersion:
    """Regression: when the JSONL history holds several versions of one
    message_id, the decide must run against the NEWEST version only —
    otherwise a stale sighting DELETEs + re-inserts the current row on
    every single run (observed live: done=94 replaced=94 for spond on a
    fully backfilled DB)."""

    def _run(self, monkeypatch, db: _FakeDb, records: list[dict]):
        from scripts.embed_messages import process
        monkeypatch.setattr("scripts.embed_messages.iter_records",
                            lambda source, verbose: iter(records))
        monkeypatch.setattr("scripts.embed_messages._embed_pending",
                            _fake_embed_pending)
        return process(cast(Any, _FakeConn(db)), "spond", None, False)

    def test_newest_version_matching_db_is_a_pure_skip(self, monkeypatch):
        # The live scenario: DB is fully backfilled (holds v2), the rescan
        # sees v1 (older file) then v2. Zero writes, one skip.
        v1 = _spond_event({"unansweredIds": ["A"]})   # older file
        v2 = _spond_event({"acceptedIds": ["A"]})     # newer file
        v2_hash = _must(spond_payload(v2))["content_hash"]
        db = _FakeDb({"spond:event:e1": v2_hash})     # DB already has v2
        done, skipped, failed = self._run(monkeypatch, db, [v1, v2])
        assert (done, failed) == (0, 0)
        assert skipped == 1
        assert db.deletes == [] and db.inserts == []
        assert db.hashes["spond:event:e1"] == v2_hash

    def test_older_db_row_replaced_once_by_newest(self, monkeypatch):
        v1 = _spond_event({"unansweredIds": ["A"]})
        v2 = _spond_event({"acceptedIds": ["A"]})
        v1_hash = _must(spond_payload(v1))["content_hash"]
        db = _FakeDb({"spond:event:e1": v1_hash})     # DB has the old version
        done, skipped, failed = self._run(monkeypatch, db, [v1, v2])
        assert failed == 0
        assert done == 1
        assert db.deletes == ["spond:event:e1"]       # replaced exactly once
        assert db.inserts == ["spond:event:e1"]
        assert db.hashes["spond:event:e1"] == _must(spond_payload(v2))["content_hash"]

    def test_identical_reemission_embeds_once(self, monkeypatch):
        v1 = _spond_event({"unansweredIds": ["A"]})
        db = _FakeDb()
        done, skipped, failed = self._run(monkeypatch, db, [v1, v1])
        assert failed == 0
        assert done == 1
        assert db.inserts == ["spond:event:e1"]
        assert db.hashes["spond:event:e1"] == _must(spond_payload(v1))["content_hash"]

    def test_flush_straddle_still_newest_wins(self, monkeypatch):
        # Tiny batches force flushes between records; pre-grouping by mid
        # means the stale version never reaches the queue at all.
        v1 = _spond_event({"unansweredIds": ["A"]})
        v2 = _spond_event({"acceptedIds": ["A"]})
        other = {"kind": "post", "fetched_at": "f",
                 "data": {"id": "p1", "title": "Info night",
                          "body": "Thursday at 18:00"}}
        db = _FakeDb()
        monkeypatch.setattr("scripts.embed_messages.BATCH_CHUNKS", 1)
        done, skipped, failed = self._run(monkeypatch, db, [v1, other, v2])
        assert failed == 0
        assert done == 2      # event + post, each exactly once
        assert sorted(db.inserts) == ["spond:event:e1", "spond:post:p1"]
        assert db.deletes == []
        assert db.hashes["spond:event:e1"] == _must(spond_payload(v2))["content_hash"]
