"""Persistence integration tests for the embed writers."""
from __future__ import annotations

import psycopg

from scripts import embed_images as ei
from scripts import embed_messages as em


def _vector(width: int = 4096) -> list[float]:
    return [0.1, *([0.0] * (width - 1))]


def _fake_embed_pending(batch):
    return {
        mi: {("body", ci): _vector() for ci in range(len(payload["body_chunks"]))}
        for mi, payload in enumerate(batch)
    }


def _event(bucket: str) -> dict:
    return {
        "kind": "event",
        "fetched_at": "2026-11-01T10:00:00Z",
        "data": {
            "id": "event-1",
            "heading": "Practice",
            "responses": {bucket: ["member-1"]},
        },
    }


def _payload(record: dict) -> em.SourcePayload:
    payload = em.spond_payload(record)
    assert payload is not None
    return payload


def test_embed_messages_insert_replace_skip_and_cascade(
        writer_conn, test_pg_dsn, monkeypatch):
    monkeypatch.setattr(em, "_embed_pending", _fake_embed_pending)
    records = [_event("unansweredIds")]
    monkeypatch.setattr(em, "iter_records", lambda source, verbose: iter(records))

    assert em.process(writer_conn, "spond", None, False) == (1, 0, 0)
    with writer_conn.cursor() as cur:
        cur.execute(
            "SELECT id, content_hash FROM messages WHERE message_id = 'spond:event:event-1'"
        )
        first_id, first_hash = cur.fetchone()
    with psycopg.connect(test_pg_dsn) as check:
        with check.cursor() as cur:
            cur.execute("SELECT count(*) FROM messages WHERE id = %s", (first_id,))
            assert cur.fetchone()[0] == 1

    records[:] = [_event("acceptedIds")]
    assert em.process(writer_conn, "spond", None, False) == (1, 0, 0)
    with writer_conn.cursor() as cur:
        cur.execute(
            "SELECT id, content_hash FROM messages WHERE message_id = 'spond:event:event-1'"
        )
        second_id, second_hash = cur.fetchone()
        cur.execute("SELECT text FROM chunks WHERE message_id = %s", (second_id,))
        chunks = [row[0] for row in cur.fetchall()]

    assert second_id != first_id
    assert second_hash != first_hash
    assert len(chunks) == 1
    assert "accepted 1" in chunks[0]
    assert "unanswered 1" not in chunks[0]

    assert em.process(writer_conn, "spond", None, False) == (0, 1, 0)
    with writer_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE message_id = 'spond:event:event-1'")
        assert cur.fetchone()[0] == 1


def test_embed_messages_failed_insert_rolls_back_all_chunks(writer_conn, monkeypatch):
    payload = _payload(_event("unansweredIds"))
    payload["body_chunks"] = ["first chunk", "second chunk"]

    def bad_second_vector(batch):
        return {0: {("body", 0): _vector(), ("body", 1): [0.1]}}

    monkeypatch.setattr(em, "_embed_pending", bad_second_vector)
    assert em._flush_batch(writer_conn, [payload], False) == (0, 1)

    with writer_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM messages WHERE message_id = %s", (payload["mid"],))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM chunks")
        assert cur.fetchone()[0] == 0


def _pending_image(writer_conn) -> ei.PendingRow:
    with writer_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (message_id, source, tier) "
            "VALUES ('image@example.invalid', 'mail', 1) RETURNING id"
        )
        message_row_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO attachments "
            "(message_id, filename, mime_type, size_bytes, text_chars) "
            "VALUES (%s, 'photo.png', 'image/png', 8, 0) RETURNING id",
            (message_row_id,),
        )
        att_id = cur.fetchone()[0]
    return {
        "att_id": att_id,
        "message_row_id": message_row_id,
        "filename": "photo.png",
        "mime": "image/png",
        "size": 8,
        "vlm_attempts": 0,
        "mid": "image@example.invalid",
        "source": "mail",
        "from_addr": "Sender",
        "subject": "Image",
        "date": None,
    }


def test_embed_images_store_persists_vector_and_description(writer_conn):
    row = _pending_image(writer_conn)
    prepared: ei.Prepared = {
        "row": row,
        "description": "A receipt",
        "chunks": ["Vedlegg photo.png (bilde): A receipt"],
    }

    assert ei._store(writer_conn, [prepared], [_vector()], False) == (1, 0)
    with writer_conn.cursor() as cur:
        cur.execute(
            "SELECT text, vector_dims(embedding) FROM chunks WHERE attachment_id = %s",
            (row["att_id"],),
        )
        assert cur.fetchone() == (prepared["chunks"][0], 4096)
        cur.execute(
            "SELECT text_chars, vlm_model FROM attachments WHERE id = %s",
            (row["att_id"],),
        )
        assert cur.fetchone() == (len("A receipt"), ei.VLM_MODEL)


def test_embed_images_failed_store_rolls_back_and_counts_attempt(writer_conn):
    row = _pending_image(writer_conn)
    prepared: ei.Prepared = {
        "row": row,
        "description": "A receipt",
        "chunks": ["first chunk", "second chunk"],
    }

    assert ei._store(writer_conn, [prepared], [_vector(), [0.1]], False) == (0, 1)
    with writer_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE attachment_id = %s", (row["att_id"],))
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT vlm_attempts, vlm_model FROM attachments WHERE id = %s",
            (row["att_id"],),
        )
        assert cur.fetchone() == (1, None)
