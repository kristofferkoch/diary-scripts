"""Typed wrapper around notmuch thread identifiers.

The thread ID lives in three forms across the codebase:

  notmuch JSON output  →  bare ('00000000000348fc')
  messages.thread_id   →  prefixed ('thread:00000000000348fc')
  URL path /t/{...}    →  bare (so the route handler can re-prefix
                          before passing to `notmuch search`)

Mixing forms causes silent bugs — the most recent was the agenda strip
emitting prefixed IDs into URLs, producing `thread:thread:XXX` queries
that returned no results ('thread not found'). This module enforces a
single canonical (bare) representation and exposes explicit accessors
for the DB / notmuch-query form.

`ThreadId` is intentionally NOT a `str` subclass: implicit equality
between a `ThreadId` and a raw string raises `TypeError` so the
prefix-mixing bug class can't recur silently. Equality against `None`
is allowed (idiomatic optional-check). Hash uses a distinct hash space
from `str` so `tid in {"X"}` returns False without falling into the
TypeError branch.
"""
from __future__ import annotations


class ThreadId:
    """A notmuch thread identifier, stored in canonical (bare) form.

    Construct from either form — bare or `thread:`-prefixed — at the
    boundary where the value enters the program (notmuch output, DB
    row, URL path param). Internal code then passes `ThreadId` around
    and uses `.notmuch_query` / `.db_form` when emitting SQL.
    """

    __slots__ = ("bare",)

    def __init__(self, value: str | ThreadId) -> None:
        if isinstance(value, ThreadId):
            self.bare = value.bare
        else:
            self.bare = value.removeprefix("thread:")

    @property
    def notmuch_query(self) -> str:
        """`thread:XXXX` — the form `notmuch search` and the
        `messages.thread_id` DB column both use. Same string as
        `db_form`; two names so call sites read naturally."""
        return f"thread:{self.bare}"

    @property
    def db_form(self) -> str:
        """Alias of `notmuch_query` for SQL parameter binding."""
        return f"thread:{self.bare}"

    def __eq__(self, other: object) -> bool:
        # Allow `tid == None` so the idiomatic `if tid == None: ...`
        # check works (and to mirror the default `obj == None` -> False).
        # `is None` is the recommended pattern, but we don't want to
        # break code that happens to use `==`.
        if other is None:
            return False
        if not isinstance(other, ThreadId):
            raise TypeError(
                f"cannot compare ThreadId to {type(other).__name__}; "
                "wrap with ThreadId() explicitly if you mean to"
            )
        return self.bare == other.bare

    def __ne__(self, other: object) -> bool:
        if other is None:
            return True
        # Reuses __eq__'s TypeError for non-ThreadId / non-None.
        return not self.__eq__(other)

    def __hash__(self) -> int:
        # Distinct hash space from `str` so a `ThreadId` never collides
        # with a raw `str` in a dict/set. Without this, `tid in {"X"}`
        # would compute matching hashes (since we'd hash `self.bare`),
        # land on a slot, and call __eq__ — which raises. Routing
        # through a tuple gives a different hash than `hash("X")` so
        # the lookup short-circuits at "no matching slot" → False.
        return hash(("ThreadId", self.bare))

    def __str__(self) -> str:
        # `url_for(... thread_id=tid)` and Jinja `{{ tid }}` both coerce
        # via str() — they get the bare form, matching the URL pattern.
        return self.bare

    def __repr__(self) -> str:
        return f"ThreadId({self.bare!r})"
