#!/usr/bin/env python3
"""shopping.py — sjekk-side CLI for the standing shopping list.

The /shopping/ page on server is a categorised checklist the user
works through in the store (see migrations/012_shopping_list.sql). Checks
persist and are reversible on the web; the sjekk routine garbage-collects
bought items:

    uv run scripts/shopping.py                 # list items grouped by category
    uv run scripts/shopping.py add "Bananer"   # add to 'Annet' (default)
    uv run scripts/shopping.py add "Kjøttdeig" --cat kjøl   # category by prefix
    uv run scripts/shopping.py check 42        # tick item 42 (bought)
    uv run scripts/shopping.py uncheck 42      # untick item 42
    uv run scripts/shopping.py mv 42 frys      # move item 42 to a category
    uv run scripts/shopping.py rename 42 "..."  # rename item 42
    uv run scripts/shopping.py rm 42           # hard-delete item 42
    uv run scripts/shopping.py uncheck-all     # clear all checks (fresh trip)
    uv run scripts/shopping.py sweep           # delete checked items (the sjekk step)

`--cat` accepts a case-insensitive prefix of a canonical category, so
`frys`, `kjøl`, `nett` all resolve. Run `sweep` as part of every sjekk to
remove what the user ticked off since the last pass.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


from mail_reader import db, shopping  # noqa: E402


def _resolve_category(s: str) -> str:
    """Map a user-typed category to a canonical one by case-insensitive
    exact-or-prefix match. Raises SystemExit on no/ambiguous match."""
    s = s.strip().lower()
    exact = [c for c in shopping.CATEGORIES if c.lower() == s]
    if exact:
        return exact[0]
    hits = [c for c in shopping.CATEGORIES if c.lower().startswith(s)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(
            f"ukjent kategori {s!r}. Velg blant: {', '.join(shopping.CATEGORIES)}"
        )
    raise SystemExit(f"flertydig kategori {s!r}: {', '.join(hits)}")


def cmd_list(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        items = shopping.list_items(conn)
    groups = shopping.grouped(items)
    if not groups:
        print("(lista er tom)")
        return 0
    for category, cat_items in groups:
        print(f"\n{category}")
        for it in cat_items:
            box = "[x]" if it["checked"] else "[ ]"
            print(f"  {box} #{it['id']}  {it['name']}")
    n_checked = sum(1 for it in items if it["checked"])
    print(f"\n{len(items)} vare(r){f', {n_checked} avkrysset' if n_checked else ''}.")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    category = _resolve_category(args.cat) if args.cat else shopping.DEFAULT_CATEGORY
    with db.connect() as conn:
        try:
            item = shopping.add(conn, args.text, category)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    print(f"lagt til #{item['id']} ({item['category']})")
    return 0


def _set_checked(item_id: int, checked: bool) -> int:
    with db.connect() as conn:
        try:
            shopping.set_checked(conn, item_id, checked)
        except KeyError:
            print(f"ingen vare med id {item_id}", file=sys.stderr)
            return 1
    print(f"#{item_id} {'avkrysset' if checked else 'fjernet kryss'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    return _set_checked(args.id, True)


def cmd_uncheck(args: argparse.Namespace) -> int:
    return _set_checked(args.id, False)


def cmd_mv(args: argparse.Namespace) -> int:
    category = _resolve_category(args.cat)
    with db.connect() as conn:
        try:
            shopping.update(conn, args.id, category=category)
        except KeyError:
            print(f"ingen vare med id {args.id}", file=sys.stderr)
            return 1
    print(f"#{args.id} flyttet til {category}")
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            shopping.update(conn, args.id, name=args.text)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        except KeyError:
            print(f"ingen vare med id {args.id}", file=sys.stderr)
            return 1
    print(f"#{args.id} omdøpt")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        try:
            shopping.delete(conn, args.id)
        except KeyError:
            print(f"ingen vare med id {args.id}", file=sys.stderr)
            return 1
    print(f"#{args.id} slettet")
    return 0


def cmd_uncheck_all(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        n = shopping.uncheck_all(conn)
    print(f"{n} kryss nullstilt")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    with db.connect() as conn:
        removed = shopping.sweep_checked(conn)
    if not removed:
        print("ingen avkryssede varer å fjerne")
        return 0
    for it in removed:
        print(f"fjernet #{it['id']}  {it['name']} ({it['category']})")
    print(f"\n{len(removed)} vare(r) fjernet.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="shopping list CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list items grouped by category").set_defaults(
        func=cmd_list
    )

    p_add = sub.add_parser("add", help="add an item")
    p_add.add_argument("text", help="item name")
    p_add.add_argument("--cat", help="category (prefix ok); default 'Annet'")
    p_add.set_defaults(func=cmd_add)

    p_check = sub.add_parser("check", help="tick an item (bought)")
    p_check.add_argument("id", type=int)
    p_check.set_defaults(func=cmd_check)

    p_uncheck = sub.add_parser("uncheck", help="untick an item")
    p_uncheck.add_argument("id", type=int)
    p_uncheck.set_defaults(func=cmd_uncheck)

    p_mv = sub.add_parser("mv", help="move an item to a category")
    p_mv.add_argument("id", type=int)
    p_mv.add_argument("cat", help="target category (prefix ok)")
    p_mv.set_defaults(func=cmd_mv)

    p_rename = sub.add_parser("rename", help="rename an item")
    p_rename.add_argument("id", type=int)
    p_rename.add_argument("text", help="new name")
    p_rename.set_defaults(func=cmd_rename)

    p_rm = sub.add_parser("rm", help="delete an item")
    p_rm.add_argument("id", type=int)
    p_rm.set_defaults(func=cmd_rm)

    sub.add_parser(
        "uncheck-all", help="clear all checks (fresh trip)"
    ).set_defaults(func=cmd_uncheck_all)

    sub.add_parser(
        "sweep", help="delete checked items — the sjekk garbage-collect step"
    ).set_defaults(func=cmd_sweep)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        return cmd_list(args)  # bare invocation = list
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
