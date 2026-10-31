"""Tests for spond_sync.py. Run with `uv run pytest scripts/`.

Regression focus: an RSVP change on an already-seen event must re-emit
(the old id-only seen-set silently swallowed it — lesson 2026-05-29).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import spond.spond

from scripts import spond_sync
from scripts.spond_sync import event_activity_key, fetch_events, member_rsvp


class FakeSpond:
    """Minimal stand-in for spond.Spond — only get_events() is exercised."""

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def get_events(self) -> list[dict]:
        return self._events


def _run_fetch(events: list[dict], state: dict, member_id: str | None) -> tuple[int, list[dict]]:
    out: list[dict] = []
    new = asyncio.run(fetch_events(FakeSpond(events), state, out, member_id))
    return new, out


HANS = "H"


def _event(rsvp_bucket: str | None = None, *, start: str = "2026-05-30T08:30:00Z") -> dict:
    ev = {"id": "ev1", "heading": "Lørdagstrening", "startTimestamp": start}
    if rsvp_bucket:
        ev["responses"] = {rsvp_bucket: [HANS]}
    return ev


# ---------- member_rsvp / event_activity_key ----------

def test_member_rsvp_buckets():
    assert member_rsvp(_event("acceptedIds"), HANS) == "acceptedIds"
    assert member_rsvp(_event("declinedIds"), HANS) == "declinedIds"
    assert member_rsvp(_event("acceptedIds"), "someone-else") == ""
    assert member_rsvp(_event("acceptedIds"), None) == ""


def test_key_changes_with_rsvp():
    unanswered = event_activity_key(_event("unansweredIds"), HANS)
    accepted = event_activity_key(_event("acceptedIds"), HANS)
    assert unanswered != accepted


def test_key_ignores_other_members_rsvp():
    """A teammate answering must not change Robin' key — avoids re-emit storms."""
    base = _event("unansweredIds")
    busy = {**base, "responses": {"unansweredIds": [HANS], "acceptedIds": ["X", "Y", "Z"]}}
    assert event_activity_key(base, HANS) == event_activity_key(busy, HANS)


def test_key_changes_on_reschedule_and_cancel():
    a = event_activity_key(_event(start="2026-05-30T08:30:00Z"), None)
    b = event_activity_key(_event(start="2026-05-31T08:30:00Z"), None)
    assert a != b
    cancelled = event_activity_key({"id": "ev1", "cancelled": True}, None)
    assert cancelled != a


# ---------- fetch_events: the actual regression ----------

def test_rsvp_change_reemits_seen_event():
    state: dict = {}

    # First sync: event is unanswered → emitted, recorded.
    new1, out1 = _run_fetch([_event("unansweredIds")], state, HANS)
    assert new1 == 1
    assert out1[0]["data"]["id"] == "ev1"

    # Re-sync, no change → not re-emitted.
    new2, _ = _run_fetch([_event("unansweredIds")], state, HANS)
    assert new2 == 0

    # Robin accepts → MUST re-emit (this is the bug we fixed).
    new3, out3 = _run_fetch([_event("acceptedIds")], state, HANS)
    assert new3 == 1
    assert out3[0]["data"]["responses"] == {"acceptedIds": [HANS]}


def test_legacy_seen_event_ids_is_migrated():
    """Old id-only state is dropped and re-baselined under the new key."""
    state = {"seen_event_ids": ["ev1"]}
    new, out = _run_fetch([_event("acceptedIds")], state, HANS)
    assert new == 1                          # re-emitted once to re-baseline
    assert "seen_event_ids" not in state     # legacy key gone
    assert state["seen_event_activity"]["ev1"]


def test_password_lookup_is_fixed_argv_not_a_shell(monkeypatch):
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(stdout="secret\n")

    monkeypatch.setattr(spond_sync.subprocess, "run", _fake_run)
    assert spond_sync.get_password() == "secret"
    assert seen["argv"] == ("pass", "show", "spond/user")
    assert "shell" not in seen["kwargs"]


def test_collector_settings_ignore_agent_config(monkeypatch):
    seen = {}

    class ClientSession:
        async def close(self):
            pass

    class FakeSpond:
        def __init__(self, *, username, password):
            seen["username"] = username
            seen["password"] = password
            self.clientsession = ClientSession()

    settings = {
        "spond.username": "trusted@example.com",
        "spond.rsvp_member_id": "trusted-member",
    }
    monkeypatch.setattr(spond.spond, "Spond", FakeSpond)
    monkeypatch.setattr(spond_sync, "trusted_cfg", lambda key, default: settings[key])
    monkeypatch.setattr(spond_sync, "get_password", lambda: "fixed-secret")
    monkeypatch.delenv("SPOND_USERNAME", raising=False)
    monkeypatch.delenv("SPOND_RSVP_MEMBER_ID", raising=False)
    monkeypatch.setenv("DIARY_CONFIG", "/agent-controlled/missing.toml")

    total, records, counts = asyncio.run(spond_sync.run_once(set(), {}))

    assert (total, records, counts) == (0, [], {})
    assert seen == {"username": "trusted@example.com", "password": "fixed-secret"}
