"""Tests for ThreadId — canonical form + the no-implicit-str-eq guard."""
from __future__ import annotations

import pytest

from mail_reader.thread_id import ThreadId


def test_constructs_from_bare():
    assert ThreadId("00000000000348fc").bare == "00000000000348fc"


def test_constructs_from_prefixed():
    """Prefixed input is normalized; .bare drops the prefix."""
    assert ThreadId("thread:00000000000348fc").bare == "00000000000348fc"


def test_idempotent_on_threadid():
    """ThreadId(ThreadId(x)) == ThreadId(x). Re-wrapping is a no-op so
    boundary code can call ThreadId() defensively without worrying
    about the input shape."""
    a = ThreadId("00000000000348fc")
    b = ThreadId(a)
    assert b.bare == a.bare


def test_notmuch_query_and_db_form_are_prefixed():
    tid = ThreadId("00000000000348fc")
    assert tid.notmuch_query == "thread:00000000000348fc"
    assert tid.db_form == "thread:00000000000348fc"


def test_str_returns_bare_for_url_building():
    """url_for and Jinja both coerce via str(). The bare form is the
    URL-correct form (the /t/ route re-prefixes when querying notmuch)."""
    assert str(ThreadId("00000000000348fc")) == "00000000000348fc"


def test_repr_is_descriptive():
    """Distinguish ThreadId from raw str in tracebacks / log lines."""
    r = repr(ThreadId("XXX"))
    assert "ThreadId" in r and "XXX" in r


def test_equality_two_threadids():
    assert ThreadId("XXX") == ThreadId("XXX")
    assert ThreadId("XXX") == ThreadId("thread:XXX")
    assert ThreadId("XXX") != ThreadId("YYY")


def test_equality_to_str_raises_type_error():
    """The whole point of the wrapper: `tid == "XXX"` should not silently
    compare. The prefix bug was someone treating a ThreadId as a str
    across a boundary; this turns that into an immediate exception."""
    with pytest.raises(TypeError):
        _ = ThreadId("XXX") == "XXX"


def test_inequality_to_str_raises_type_error():
    """`!=` shares the same TypeError path as `==`."""
    with pytest.raises(TypeError):
        _ = ThreadId("XXX") != "XXX"


def test_equality_to_int_raises_type_error():
    """Any non-ThreadId, non-None comparand raises — int, bytes, dict, …"""
    with pytest.raises(TypeError):
        _ = ThreadId("XXX") == 42


def test_equality_to_none_is_false_no_raise():
    """`tid == None` is allowed (idiomatic optional check) — returns
    False without raising. Same for `!=`."""
    assert (ThreadId("XXX") == None) is False  # noqa: E711
    assert (ThreadId("XXX") != None) is True   # noqa: E711


def test_membership_in_str_set_is_false_not_raise():
    """`tid in {"XXX"}` must be False, not TypeError — otherwise normal
    set / dict lookups against unknown strings would explode. Achieved
    by giving ThreadId a distinct hash space from str."""
    assert (ThreadId("XXX") in {"XXX", "YYY"}) is False


def test_membership_in_threadid_set():
    """Two `ThreadId`s of the same bare value collide in a set."""
    s = {ThreadId("XXX")}
    assert ThreadId("XXX") in s
    assert ThreadId("thread:XXX") in s   # normalized → same hash
    assert ThreadId("YYY") not in s


def test_can_be_dict_key():
    """ThreadId is hashable; can serve as a dict key. Same-bare lookups
    succeed regardless of which form was used at insert vs lookup."""
    d = {ThreadId("XXX"): 1}
    assert d[ThreadId("XXX")] == 1
    assert d[ThreadId("thread:XXX")] == 1
    with pytest.raises(KeyError):
        d[ThreadId("YYY")]
