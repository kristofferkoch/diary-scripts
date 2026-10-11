"""Tests for embed_images.py. Run with `uv run pytest scripts/`."""
from __future__ import annotations

import base64
import contextlib
import io
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

import scripts.embed_images as ei
from scripts.embed_messages import signal_payload


# ---------- fakes ----------

class _FakeCursor:
    def __init__(self, db: _FakeConn):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params: tuple[Any, ...] = ()):
        self.db.executed.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return self.db.fetchall_rows

    def fetchone(self):
        return self.db.fetchone_row


class _FakeConn:
    """Captures executed SQL; canned rows for the pending select / count."""
    def __init__(self):
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchall_rows: list[tuple] = []
        self.fetchone_row: tuple | None = None

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return contextlib.nullcontext()


def _png_bytes(w: int, h: int, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _pending_row(**over) -> ei.PendingRow:
    row: dict[str, Any] = {
        "att_id": 7, "message_row_id": 42, "filename": "photo.jpg",
        "mime": "image/jpeg", "size": 8, "vlm_attempts": 0,
        "mid": "id-1", "source": "mail", "from_addr": "Alice",
        "subject": "Subject", "date": "2026-10-01",
    }
    row.update(over)
    return cast(ei.PendingRow, row)


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch,
                    rows: list[ei.PendingRow], blob: bytes,
                    desc: str = "En kvittering fra en butikk.") -> None:
    """Stub everything around the Pillow + prompt core: DB select, blob
    resolution, VLM HTTP, embedding HTTP."""
    monkeypatch.setattr(ei, "select_pending",
                        lambda conn, sources, limit: rows)
    monkeypatch.setattr(ei, "resolve_blob",
                        lambda conn, row, indexes: blob)
    monkeypatch.setattr(ei, "_post_chat", lambda payload: {
        "choices": [{"message": {"reasoning_content": "tenker…",
                                 "content": desc}}]})
    monkeypatch.setattr(ei, "embed_batch",
                        lambda texts: [[0.1, 0.2]] * len(texts))


# ---------- MIME filter ----------

class TestImageMimes:
    def test_constant_matches_helper(self):
        assert all(ei.is_image_mime(m) for m in ei.IMAGE_MIMES)

    def test_out_of_scope_types(self):
        for m in ("image/tiff", "image/svg+xml", "image/bmp",
                  "application/pdf", "video/mp4", ""):
            assert not ei.is_image_mime(m)


# ---------- preprocessing ----------

class TestPrepareImage:
    def test_tiny_returns_none(self):
        assert ei.prepare_image(_png_bytes(64, 64)) is None
        assert ei.prepare_image(_png_bytes(100, 127)) is None

    def test_at_floor_is_processed(self):
        assert ei.prepare_image(_png_bytes(128, 100)) is not None
        assert ei.prepare_image(_png_bytes(300, 300)) is not None

    def test_downscales_long_side(self):
        url = ei.prepare_image(_png_bytes(3000, 1000))
        assert url is not None
        assert url.startswith("data:image/jpeg;base64,")
        data = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(data)) as im:
            assert im.format == "JPEG"
            assert im.size == ei.scale_dims(3000, 1000) == (1568, 523)

    def test_gif_decodes_first_frame(self):
        buf = io.BytesIO()
        frames = [Image.new("P", (300, 300), 1), Image.new("P", (300, 300), 2)]
        frames[0].save(buf, format="GIF", save_all=True,
                       append_images=frames[1:])
        assert ei.prepare_image(buf.getvalue()) is not None

    def test_rgba_flattens(self):
        im = Image.new("RGBA", (200, 200), (10, 20, 30, 128))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        url = ei.prepare_image(buf.getvalue())
        assert url is not None
        data = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(data)) as out:
            assert out.mode == "RGB"


# ---------- mail blob matching ----------

def _raw_mail(parts: list[tuple[str, bytes]]) -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "pics"
    msg.set_content("see attached")
    msg.make_mixed()
    for name, data in parts:
        msg.add_attachment(data, maintype="image", subtype="png",
                           filename=name)
    return msg.as_bytes()


class TestMailBlob:
    def test_occurrence_disambiguates_duplicate_filenames(self):
        raw = _raw_mail([("image001.png", b"AAAA1111"),
                         ("image001.png", b"BBBB2222")])
        assert ei.mail_blob(raw, "image001.png", "image/png", 8, 0) == b"AAAA1111"
        assert ei.mail_blob(raw, "image001.png", "image/png", 8, 1) == b"BBBB2222"
        assert ei.mail_blob(raw, "image001.png", "image/png", 8, 2) is None

    def test_size_breaks_same_name_tie(self):
        raw = _raw_mail([("logo.png", b"tiny"),
                         ("logo.png", b"much-larger-blob")])
        assert (ei.mail_blob(raw, "logo.png", "image/png", 16, 0)
                == b"much-larger-blob")

    def test_mime_mismatch_skips(self):
        raw = _raw_mail([("photo.png", b"12345678")])
        assert ei.mail_blob(raw, "photo.png", "image/jpeg", 8, 0) is None

    def test_occurrence_index_counts_earlier_duplicates(self):
        conn = _FakeConn()
        conn.fetchone_row = (1,)
        row = _pending_row()
        assert ei._occurrence_index(cast(Any, conn), row) == 1
        sql, params = conn.executed[0]
        assert "count(*)" in sql
        assert params == (row["message_row_id"], row["filename"],
                          row["mime"], row["size"], row["att_id"])


# ---------- signal / vigilo blob resolution ----------

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
        "text": "look at this",
        "quote": None,
        "attachments": [{"contentType": "image/jpeg", "filename": "photo.jpg",
                         "size": 8, "id": "blob-1"}],
    }
    rec.update(over)
    return rec


class TestRecordIndex:
    def test_signal_mids_recomputed_with_builder(self, monkeypatch):
        rec = _signal_rec()
        monkeypatch.setattr(ei, "iter_records",
                            lambda source, verbose: iter([rec]))
        index = ei._record_index("signal")
        p = signal_payload(rec)
        assert p is not None
        assert index[p["mid"]] is rec

    def test_newest_sighting_wins(self, monkeypatch):
        old = _signal_rec()
        new = dict(_signal_rec())   # same mid (ts + sender + text)
        monkeypatch.setattr(ei, "iter_records",
                            lambda source, verbose: iter([old, new]))
        index = ei._record_index("signal")
        p = signal_payload(old)
        assert p is not None
        assert index[p["mid"]] is new

    def test_unbuildable_records_skipped(self, monkeypatch):
        monkeypatch.setattr(ei, "iter_records",
                            lambda source, verbose: iter([{"ts": 1}]))
        assert ei._record_index("signal") == {}


class TestSignalBlob:
    def test_matches_filename_and_size(self, tmp_path, monkeypatch):
        blob = tmp_path / "att-1"
        blob.write_bytes(b"\x89PNG....")
        monkeypatch.setattr(ei, "SIGNAL_ATTACH_DIR", tmp_path)
        rec = {"attachments": [
            {"id": "att-0", "filename": "other.png",
             "contentType": "image/png", "size": 3},
            {"id": "att-1", "filename": "photo.png",
             "contentType": "image/png", "size": 8},
        ]}
        assert ei.signal_blob(rec, "photo.png", 8) == blob
        assert ei.signal_blob(rec, "photo.png", 9) is None
        assert ei.signal_blob(rec, "missing.png", 8) is None

    def test_unnamed_attachment(self, tmp_path, monkeypatch):
        blob = tmp_path / "att-2"
        blob.write_bytes(b"12345678")
        monkeypatch.setattr(ei, "SIGNAL_ATTACH_DIR", tmp_path)
        rec = {"attachments": [{"id": "att-2", "contentType": "image/jpeg",
                                "size": 8}]}
        assert ei.signal_blob(rec, "(unnamed)", 8) == blob

    def test_missing_blob_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ei, "SIGNAL_ATTACH_DIR", tmp_path)
        rec = {"attachments": [{"id": "gone", "filename": "photo.png",
                                "contentType": "image/png", "size": 8}]}
        assert ei.signal_blob(rec, "photo.png", 8) is None


class TestVigiloBlob:
    def test_message_attachment_resolves_recorded_path(self, tmp_path):
        f = tmp_path / "bilde.jpg"
        f.write_bytes(b"jpeg-bytes")
        rec = {"kind": "message", "data": {"threadUid": "t1", "messages": [
            {"sender": "Alice", "body": "se her", "attachments": [
                {"name": "bilde.jpg", "mimeType": "image/jpeg",
                 "path": str(f)}]}]}}
        assert ei.vigilo_blob(rec, "bilde.jpg", "image/jpeg") == f
        assert ei.vigilo_blob(rec, "annet.jpg", "image/jpeg") is None

    def test_missing_file_is_none(self, tmp_path):
        rec = {"kind": "message", "data": {"messages": [
            {"attachments": [{"name": "gone.jpg", "mimeType": "image/jpeg",
                              "path": str(tmp_path / "gone.jpg")}]}]}}
        assert ei.vigilo_blob(rec, "gone.jpg", "image/jpeg") is None

    def test_news_attachment_has_no_local_copy(self):
        rec = {"kind": "news", "data": {"postId": "n1", "attachments": [
            {"id": "a1", "name": "plan.jpg", "mimeType": "image/jpeg"}]}}
        assert ei.vigilo_blob(rec, "plan.jpg", "image/jpeg") is None


# ---------- prompt + VLM call ----------

class TestPrompt:
    def test_context_line(self):
        p = ei.build_prompt("kvittering.png", "Alice", "Refusjon",
                            "2026-10-01")
        assert "kvittering.png" in p
        assert "Alice" in p
        assert "Refusjon" in p
        assert "2026-10-01" in p
        assert "ordrett" in p          # verbatim-transcription instruction

    def test_missing_metadata_placeholders(self):
        p = ei.build_prompt("(unnamed)", "", "", None)
        assert "?" in p and "(uten emne)" in p and "ukjent" in p


class TestDescribeImage:
    def test_payload_shape_and_reasoning_ignored(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_post(payload):
            captured.update(payload)
            return {"choices": [{"message": {
                "reasoning_content": "tenker høyt…",
                "content": "Et bilde av en kvittering."}}]}

        monkeypatch.setattr(ei, "_post_chat", fake_post)
        out = ei.describe_image("data:image/jpeg;base64,AAAA", "prompt")
        assert out == "Et bilde av en kvittering."
        assert captured["model"] == ei.VLM_MODEL
        assert captured["max_tokens"] == 1024
        assert "enable_thinking" not in captured
        content = captured["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "prompt"}
        assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"

    def test_empty_description_raises(self, monkeypatch):
        monkeypatch.setattr(ei, "_post_chat", lambda payload: {
            "choices": [{"message": {"content": "  "}}]})
        with pytest.raises(RuntimeError, match="empty description"):
            ei.describe_image("data:image/jpeg;base64,AAAA", "prompt")


# ---------- pending selection ----------

class TestSelectPending:
    def test_sql_filters_and_mapping(self):
        conn = _FakeConn()
        conn.fetchall_rows = [
            (7, 42, "photo.jpg", "image/jpeg", 1234, 0, "id-1", "mail",
             "Alice", "Subject", "2026-10-01T10:00:00"),
        ]
        rows = ei.select_pending(cast(Any, conn), ("mail", "signal"), 5)
        sql, params = conn.executed[0]
        assert "a.text_chars = 0" in sql
        assert "a.vlm_model IS NULL" in sql
        assert "a.vlm_attempts < %s" in sql
        assert "m.date DESC NULLS LAST" in sql
        assert params == (list(ei.IMAGE_MIMES), ei.MAX_ATTEMPTS,
                          ["mail", "signal"], 5)
        row = rows[0]
        assert row["att_id"] == 7
        assert row["message_row_id"] == 42
        assert row["mid"] == "id-1"
        assert row["source"] == "mail"
        assert row["date"] == "2026-10-01T10:00:00"


# ---------- error accounting ----------

class TestMarkError:
    def test_threshold(self):
        assert ei.mark_error(0) == (1, None)
        assert ei.mark_error(1) == (2, None)
        assert ei.mark_error(2) == (3, "error")

    def test_process_third_strike_marks_error(self, monkeypatch):
        row = _pending_row()
        monkeypatch.setattr(ei, "select_pending",
                            lambda conn, sources, limit: [row])
        monkeypatch.setattr(ei, "resolve_blob",
                            lambda conn, row, indexes: None)   # blob gone
        conn = _FakeConn()
        for expected_attempts, expected_model in ((1, None), (2, None),
                                                  (3, "error")):
            conn.executed.clear()
            done, tiny, failed = ei.process(cast(Any, conn), ("mail",), 25,
                                            False)
            assert (done, tiny, failed) == (0, 0, 1)
            upd = [p for s, p in conn.executed
                   if s.startswith("UPDATE attachments SET vlm_attempts")]
            assert upd == [(expected_attempts, expected_model,
                            row["att_id"])]


# ---------- process flow ----------

class TestProcess:
    def test_happy_path_stores_prefixed_chunks(self, monkeypatch):
        conn = _FakeConn()
        rows = [_pending_row()]
        _patch_pipeline(monkeypatch, rows, blob=_png_bytes(300, 300))
        done, tiny, failed = ei.process(cast(Any, conn), ("mail",), 25, False)
        assert (done, tiny, failed) == (1, 0, 0)
        chunk_inserts = [p for s, p in conn.executed
                         if s.startswith("INSERT INTO chunks")]
        assert len(chunk_inserts) == 1
        assert chunk_inserts[0][0] == rows[0]["message_row_id"]
        assert chunk_inserts[0][1] == rows[0]["att_id"]   # attachment_id set
        assert chunk_inserts[0][3].startswith("Vedlegg photo.jpg (bilde): ")
        updates = [p for s, p in conn.executed
                   if s.startswith("UPDATE attachments SET text_chars")]
        assert len(updates) == 1
        assert updates[0] == (len("En kvittering fra en butikk."),
                              ei.VLM_MODEL, rows[0]["att_id"])

    def test_tiny_marked_without_attempt(self, monkeypatch):
        conn = _FakeConn()
        rows = [_pending_row()]
        _patch_pipeline(monkeypatch, rows, blob=_png_bytes(32, 32))
        done, tiny, failed = ei.process(cast(Any, conn), ("mail",), 25, False)
        assert (done, tiny, failed) == (0, 1, 0)
        tiny_updates = [p for s, p in conn.executed if "skipped:tiny" in s]
        assert tiny_updates == [(rows[0]["att_id"],)]
        assert not [s for s, _ in conn.executed
                    if s.startswith("UPDATE attachments SET vlm_attempts")]

    def test_signal_row_builds_record_index_once(self, monkeypatch):
        conn = _FakeConn()
        rows = [_pending_row(source="signal", mid="signal:1:abc",
                             filename="photo.png", mime="image/png"),
                _pending_row(att_id=8, source="signal", mid="signal:2:def",
                             filename="photo.png", mime="image/png")]
        _patch_pipeline(monkeypatch, rows, blob=_png_bytes(300, 300))
        scans = []
        monkeypatch.setattr(ei, "iter_records",
                            lambda source, verbose: scans.append(source)
                            or iter([]))
        done, tiny, failed = ei.process(cast(Any, conn), ("signal",), 25,
                                        False)
        assert (done, tiny, failed) == (2, 0, 0)
        assert scans == ["signal"]     # cached across rows, not per row

    def test_batch_embed_failure_marks_attempts(self, monkeypatch):
        conn = _FakeConn()
        rows = [_pending_row(), _pending_row(att_id=8, mid="id-2")]
        _patch_pipeline(monkeypatch, rows, blob=_png_bytes(300, 300))

        def boom(texts):
            raise RuntimeError("embed endpoint down")

        monkeypatch.setattr(ei, "embed_batch", boom)
        done, tiny, failed = ei.process(cast(Any, conn), ("mail",), 25, False)
        assert (done, tiny, failed) == (0, 0, 2)
        attempts = [p for s, p in conn.executed
                    if s.startswith("UPDATE attachments SET vlm_attempts")]
        assert attempts == [(1, None, 7), (1, None, 8)]
