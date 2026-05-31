"""Tests for mail_reader.shopping — the standing shopping-list CRUD.

Hits the real mailvec DB (skips if unreachable, same as the other
DB-backed tests). Every item created here carries a unique marker in its
name; the `cleanup` fixture hard-deletes all marker rows on teardown so
the live list isn't polluted regardless of which assertions fired.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from mail_reader import shopping


PG_DSN = os.environ.get("PG_DSN", "dbname=mailvec")
MARKER = "[[test_shopping-marker]]"


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


@pytest.fixture
def conn():
    with psycopg.connect(PG_DSN) as c:
        yield c


@pytest.fixture(autouse=True)
def cleanup(conn):
    yield
    with conn.cursor() as cur:
        cur.execute("DELETE FROM shopping_items WHERE name LIKE %s", (f"%{MARKER}%",))
    conn.commit()


def _mk(conn, name: str, category: str = "Annet") -> dict:
    return shopping.add(conn, f"{name} {MARKER}", category)


# --- add / validation ------------------------------------------------------


def test_add_returns_unchecked_row(conn):
    item = _mk(conn, "Bananer", "Frukt & grønt")
    assert item["checked"] is False
    assert item["name"].startswith("Bananer")
    assert item["category"] == "Frukt & grønt"
    assert item["id"] > 0


def test_add_strips_and_rejects_empty(conn):
    item = shopping.add(conn, f"  trimmet {MARKER}  ")
    assert item["name"] == f"trimmet {MARKER}"
    assert item["category"] == "Annet"  # default
    with pytest.raises(ValueError):
        shopping.add(conn, "   ")


def test_add_rejects_offlist_category(conn):
    with pytest.raises(ValueError):
        shopping.add(conn, f"x {MARKER}", "Garasje")


# --- grouping / ordering ---------------------------------------------------


def test_grouped_orders_categories_netthandel_last(conn):
    _mk(conn, "nett-ting", "Netthandel")
    _mk(conn, "eple", "Frukt & grønt")
    _mk(conn, "vaskemiddel", "Husholdning")
    items = [i for i in shopping.list_items(conn) if MARKER in i["name"]]
    groups = shopping.grouped(items)
    order = [cat for cat, _ in groups]
    # Frukt & grønt precedes Husholdning precedes Netthandel (always last).
    assert order.index("Frukt & grønt") < order.index("Husholdning")
    assert order[-1] == "Netthandel"


def test_grouped_keeps_insertion_order_within_category(conn):
    first = _mk(conn, "først", "Frys")
    second = _mk(conn, "andre", "Frys")
    items = [i for i in shopping.list_items(conn) if MARKER in i["name"]]
    frys = dict(shopping.grouped(items))["Frys"]
    ids = [i["id"] for i in frys]
    assert ids.index(first["id"]) < ids.index(second["id"])


# --- checking --------------------------------------------------------------


def test_toggle_flips_and_persists(conn):
    item = _mk(conn, "melk", "Kjøl & meieri")
    toggled = shopping.toggle(conn, item["id"])
    assert toggled["checked"] is True
    back = shopping.toggle(conn, item["id"])
    assert back["checked"] is False


def test_set_checked_explicit(conn):
    item = _mk(conn, "ost")
    assert shopping.set_checked(conn, item["id"], True)["checked"] is True
    assert shopping.set_checked(conn, item["id"], False)["checked"] is False


def test_toggle_missing_raises(conn):
    with pytest.raises(KeyError):
        shopping.toggle(conn, 2_000_000_001)


# --- update ----------------------------------------------------------------


def test_update_name_and_category(conn):
    item = _mk(conn, "før", "Annet")
    edited = shopping.update(
        conn, item["id"], name=f"etter {MARKER}", category="Frys"
    )
    assert edited["name"] == f"etter {MARKER}"
    assert edited["category"] == "Frys"
    assert edited["updated_at"] >= edited["created_at"]


def test_update_rejects_offlist_category_and_empty_name(conn):
    item = _mk(conn, "noe")
    with pytest.raises(ValueError):
        shopping.update(conn, item["id"], category="Loft")
    with pytest.raises(ValueError):
        shopping.update(conn, item["id"], name="   ")


def test_update_no_fields_raises(conn):
    item = _mk(conn, "noe")
    with pytest.raises(ValueError):
        shopping.update(conn, item["id"])


def test_update_missing_raises(conn):
    with pytest.raises(KeyError):
        shopping.update(conn, 2_000_000_002, name="x")


# --- sweep / uncheck-all ---------------------------------------------------


def test_sweep_checked_deletes_only_checked_and_returns_them(conn):
    keep = _mk(conn, "behold")
    gone = _mk(conn, "kjøpt")
    shopping.set_checked(conn, gone["id"], True)
    removed = shopping.sweep_checked(conn)
    removed_ids = [i["id"] for i in removed]
    assert gone["id"] in removed_ids
    assert keep["id"] not in removed_ids
    assert shopping.get(conn, gone["id"]) is None
    assert shopping.get(conn, keep["id"]) is not None


def test_uncheck_all_clears_checks_without_deleting(conn):
    a = _mk(conn, "a")
    b = _mk(conn, "b")
    shopping.set_checked(conn, a["id"], True)
    shopping.set_checked(conn, b["id"], True)
    n = shopping.uncheck_all(conn)
    assert n >= 2
    assert shopping.get(conn, a["id"])["checked"] is False
    assert shopping.get(conn, b["id"])["checked"] is False


# --- delete ----------------------------------------------------------------


def test_delete_removes_and_missing_raises(conn):
    item = _mk(conn, "slett meg")
    shopping.delete(conn, item["id"])
    assert shopping.get(conn, item["id"]) is None
    with pytest.raises(KeyError):
        shopping.delete(conn, item["id"])
