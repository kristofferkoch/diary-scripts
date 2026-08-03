"""Tests for mail_reader.extract helpers.

Integration-style: needs `mailvec`. Each DB-touching test runs inside a
transaction that's rolled back at teardown — the helpers don't commit, so
nothing leaks into `entities` / `summary_*` for subsequent runs.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader import extract


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")


def _dsn_reachable() -> bool:
    try:
        with psycopg.connect(PG_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _dsn_reachable(),
    reason=f"mailvec DB not reachable at PG_DSN={PG_DSN!r}",
)


# ---------- normalize_entity (pure, no DB) ----------

@pytest.mark.parametrize("kind,value,meta,want", [
    ("person",     "Astrid Solberg",         {},                                    "astrid solberg"),
    ("person",     "  ASTRID   Solberg  ",   {},                                    "astrid solberg"),
    ("org",        "Sparebanken Sør AS",    {},                                    "sparebanken sør"),
    ("org",        "Hafslund",              {},                                    "hafslund"),
    ("org",        "Acme Inc.",             {},                                    "acme"),
    ("org",        "Oslo Kommune",          {},                                    "oslo"),
    ("place",      "Eksempelveien 3B",        {},                                    "eksempelveien 3b"),
    ("money",      "3 450 kr",              {"amount": 3450, "currency": "nok"},   "3450.00NOK"),
    ("money",      "USD 199.99",            {"amount": 199.99, "currency": "usd"}, "199.99USD"),
    ("money",      "no amount",             {},                                    "no amount"),
    ("identifier", "1234 5678 9012",        {"type": "kid"},                       "kid:123456789012"),
    ("identifier", "NAV-2026/4711",         {"type": "case"},                      "case:nav-2026/4711"),
    ("identifier", "raw-id",                {},                                    "raw-id"),
    ("contact",    "+47 22 33 44 55",       {"method": "tel"},                     "tel:4722334455"),
    ("contact",    "Foo@Example.COM",       {"method": "mail"},                    "mail:foo@example.com"),
    ("url",        "https://Example.com/Foo/", {},                                 "https://example.com/foo"),
])
def test_normalize_entity(kind, value, meta, want):
    assert extract.normalize_entity(kind, value, meta) == want


def test_normalize_entity_empty_value():
    assert extract.normalize_entity("org", "   ", {}) == ""


def test_normalize_entity_money_bad_amount_falls_back():
    # amount is non-numeric → fall back to the lowercased value
    assert extract.normalize_entity("money", "junk", {"amount": "not-a-number"}) == "junk"


def test_normalize_entity_tel_no_digits():
    assert extract.normalize_entity("contact", "no digits", {"method": "tel"}) == ""


# ---------- DB-touching tests ----------

@pytest.fixture
def conn():
    """Rollback-on-teardown connection — the helpers don't commit, so
    nothing leaks into themes/entities/summary_* between tests."""
    with psycopg.connect(PG_DSN) as c:
        try:
            yield c
        finally:
            c.rollback()


@pytest.fixture
def summary_id(conn):
    """Insert a sentinel summary row, yield its id, rollback at end."""
    with conn.cursor() as cur:
        # any embedded message works — we just need a valid FK target
        cur.execute("""
            SELECT m.id
            FROM messages m
            JOIN chunks c ON c.message_id = m.id AND c.attachment_id IS NULL
            LIMIT 1
        """)
        row = cur.fetchone()
        assert row is not None, "no embedded messages in DB"
        msg_row_id = row[0]
        cur.execute("""
            INSERT INTO summaries
                (message_id, model, prompt_version, short, status, quality_tier)
            VALUES (%s, 'pytest-extract', 'pytest', '', 'pending', 99)
            RETURNING id
        """, (msg_row_id,))
        ins = cur.fetchone()
        assert ins is not None
        return ins[0]


# ---------- upsert_entities ----------

def test_upsert_entities_dedup_via_normalized(conn, summary_id):
    ids = extract.upsert_entities(conn, summary_id, [
        {"kind": "org", "value": "Pytest Bank AS",  "meta": {}},
        {"kind": "org", "value": "Pytest Bank",     "meta": {}},
    ])
    assert len(set(ids)) == 1, "both rows normalize to same canonical form"


def test_upsert_entities_skips_unknown_kind(conn, summary_id):
    ids = extract.upsert_entities(conn, summary_id, [
        {"kind": "BADKIND", "value": "foo", "meta": {}},
        {"kind": "org",     "value": "Pytest Acme", "meta": {}},
    ])
    assert len(ids) == 1


def test_upsert_entities_skips_empty_value(conn, summary_id):
    ids = extract.upsert_entities(conn, summary_id, [
        {"kind": "person", "value": "  ", "meta": {}},
        {"kind": "person", "value": "Pytest Astrid", "meta": {}},
    ])
    assert len(ids) == 1


def test_upsert_entities_links_to_summary(conn, summary_id):
    ids = extract.upsert_entities(conn, summary_id, [
        {"kind": "money",      "value": "999 kr",
         "meta": {"amount": 999, "currency": "NOK"}},
        {"kind": "identifier", "value": "9999",
         "meta": {"type": "kid"}},
    ])
    assert len(ids) == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id FROM summary_entities WHERE summary_id = %s",
            (summary_id,),
        )
        linked = sorted(r[0] for r in cur.fetchall())
    assert linked == sorted(ids)


def test_upsert_entities_idempotent_link(conn, summary_id):
    """Calling twice with the same entities should not duplicate M2M rows."""
    extract.upsert_entities(conn, summary_id, [
        {"kind": "place", "value": "Pytest Town", "meta": {}},
    ])
    extract.upsert_entities(conn, summary_id, [
        {"kind": "place", "value": "Pytest Town", "meta": {}},
    ])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM summary_entities WHERE summary_id = %s",
            (summary_id,),
        )
        result = cur.fetchone()
        assert result is not None
        assert result[0] == 1


def test_upsert_entities_empty_returns_empty(conn, summary_id):
    assert extract.upsert_entities(conn, summary_id, []) == []


# ---------- insert_temporal ----------

def test_insert_temporal_inserts_valid(conn, summary_id):
    n = extract.insert_temporal(conn, summary_id, [
        {"kind": "deadline", "occurs_at": "2026-06-15", "note": "forfall"},
        {"kind": "event",    "occurs_at": "2026-04-22", "note": None},
    ])
    assert n == 2


def test_insert_temporal_skips_non_iso_date(conn, summary_id):
    n = extract.insert_temporal(conn, summary_id, [
        {"kind": "deadline", "occurs_at": "neste tirsdag", "note": None},
        {"kind": "deadline", "occurs_at": "2026-06-15",    "note": None},
    ])
    assert n == 1, "mangled date is skipped, good one still lands"


def test_insert_temporal_skips_bad_kind(conn, summary_id):
    n = extract.insert_temporal(conn, summary_id, [
        {"kind": "whenever", "occurs_at": "2026-06-15", "note": None},
    ])
    assert n == 0


def test_insert_temporal_idempotent(conn, summary_id):
    row: extract.Temporal = {"kind": "deadline", "occurs_at": "2026-06-15", "note": "x"}
    first = extract.insert_temporal(conn, summary_id, [row])
    second = extract.insert_temporal(conn, summary_id, [row])
    assert first == 1
    assert second == 0


def test_insert_temporal_empty_returns_zero(conn, summary_id):
    assert extract.insert_temporal(conn, summary_id, []) == 0
