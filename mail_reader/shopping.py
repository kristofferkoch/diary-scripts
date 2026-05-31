"""Shopping list — a standing, categorised checklist.

Backs the /shopping/ page (see server.py) and the sjekk-side CLI
(scripts/shopping.py). Each item is one row in `shopping_items`: a name,
a coarse category, and a `checked` flag. The page groups items by
category in the canonical `CATEGORIES` order (Netthandel last) and lets
the user tick them off in the store. Checks persist and are reversible
on the web; the sjekk routine removes bought items via `sweep_checked`.

Thin CRUD over Postgres — no ORM, parameterised SQL only. Offensive by
design: an empty name or an off-list category raises rather than being
silently coerced, and a missing row on update/toggle/delete raises so the
UI/CLI can surface a 404.
"""
from __future__ import annotations

from typing import Any, LiteralString, TypedDict

import psycopg
from psycopg.rows import dict_row

# Canonical, ordered category list. Netthandel is always last so it sinks
# to the bottom of the page (quick to reach the in-store categories first).
# Kept here, not as a DB CHECK constraint, so it can be reordered/extended
# without a migration — see migrations/012_shopping_list.sql.
CATEGORIES: list[str] = [
    "Frukt & grønt",
    "Kjøl & meieri",
    "Tørrvare & pålegg",
    "Frys",
    "Husholdning",
    "Annet",
    "Netthandel",
]
NETTHANDEL = "Netthandel"
DEFAULT_CATEGORY = "Annet"

_COLS = "id, name, category, checked, created_at, updated_at"


class Item(TypedDict):
    id: int
    name: str
    category: str
    checked: bool
    created_at: object  # datetime; kept opaque so this module needn't import it
    updated_at: object


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("item name is empty")
    return name


def _check_category(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")
    return category


def list_items(conn: psycopg.Connection) -> list[Item]:
    """Every item, ordered by insertion time. Grouping into the canonical
    category order is `grouped()`'s job; within a category the created_at
    order keeps checked items in place."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {_COLS} FROM shopping_items ORDER BY created_at, id"
        )
        return cur.fetchall()  # type: ignore[return-value]


def grouped(items: list[Item]) -> list[tuple[str, list[Item]]]:
    """Bucket `items` into (category, items) pairs in canonical order.

    Known categories appear in `CATEGORIES` order; any stray off-list
    category (manual SQL only) is appended alphabetically but still before
    Netthandel, which always stays last. Empty categories are omitted.
    """
    by_cat: dict[str, list[Item]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    ordered: list[tuple[str, list[Item]]] = []
    for cat in CATEGORIES:
        if cat != NETTHANDEL and by_cat.get(cat):
            ordered.append((cat, by_cat[cat]))
    for cat in sorted(by_cat):
        if cat not in CATEGORIES and by_cat[cat]:
            ordered.append((cat, by_cat[cat]))
    if by_cat.get(NETTHANDEL):
        ordered.append((NETTHANDEL, by_cat[NETTHANDEL]))
    return ordered


def get(conn: psycopg.Connection, item_id: int) -> Item | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT {_COLS} FROM shopping_items WHERE id = %s", (item_id,))
        return cur.fetchone()  # type: ignore[return-value]


def add(
    conn: psycopg.Connection, name: str, category: str = DEFAULT_CATEGORY
) -> Item:
    """Insert an item. Raises ValueError on empty name or off-list category."""
    name = _clean_name(name)
    category = _check_category(category)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"INSERT INTO shopping_items (name, category) VALUES (%s, %s) "
            f"RETURNING {_COLS}",
            (name, category),
        )
        row = cur.fetchone()
    conn.commit()
    return row  # type: ignore[return-value]


def update(
    conn: psycopg.Connection,
    item_id: int,
    *,
    name: str | None = None,
    category: str | None = None,
) -> Item:
    """Edit an item's name and/or category. Raises ValueError on bad input,
    KeyError if no such item, and rejects a no-op (both args None)."""
    # LiteralString-typed so the joined SET clause stays a LiteralString and
    # psycopg's injection-guarded execute() overload accepts it. Every fragment
    # below is a hardcoded literal; only %s placeholders carry user data.
    sets: list[LiteralString] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        params.append(_clean_name(name))
    if category is not None:
        sets.append("category = %s")
        params.append(_check_category(category))
    if not sets:
        raise ValueError("update with no fields")
    sets.append("updated_at = now()")
    params.append(item_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE shopping_items SET {', '.join(sets)} WHERE id = %s "
            f"RETURNING {_COLS}",
            tuple(params),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(item_id)
    conn.commit()
    return row  # type: ignore[return-value]


def set_checked(conn: psycopg.Connection, item_id: int, checked: bool) -> Item:
    """Set an item's checked flag explicitly. Raises KeyError if missing."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE shopping_items SET checked = %s, updated_at = now() "
            f"WHERE id = %s RETURNING {_COLS}",
            (checked, item_id),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(item_id)
    conn.commit()
    return row  # type: ignore[return-value]


def toggle(conn: psycopg.Connection, item_id: int) -> Item:
    """Flip an item's checked flag — the in-store tap. Raises KeyError if
    missing."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"UPDATE shopping_items SET checked = NOT checked, updated_at = now() "
            f"WHERE id = %s RETURNING {_COLS}",
            (item_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(item_id)
    conn.commit()
    return row  # type: ignore[return-value]


def uncheck_all(conn: psycopg.Connection) -> int:
    """Clear every check (start a fresh trip). Returns rows affected."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE shopping_items SET checked = false, updated_at = now() "
            "WHERE checked"
        )
        n = cur.rowcount
    conn.commit()
    return n


def sweep_checked(conn: psycopg.Connection) -> list[Item]:
    """Delete all checked items and return them — the sjekk's garbage-collect
    step for things the user bought. Returns [] when nothing was checked."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"DELETE FROM shopping_items WHERE checked RETURNING {_COLS}"
        )
        rows = cur.fetchall()
    conn.commit()
    return rows  # type: ignore[return-value]


def delete(conn: psycopg.Connection, item_id: int) -> None:
    """Hard-delete one item. Raises KeyError if it didn't exist."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM shopping_items WHERE id = %s", (item_id,))
        if cur.rowcount == 0:
            raise KeyError(item_id)
    conn.commit()
