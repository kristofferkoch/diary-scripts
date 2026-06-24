from __future__ import annotations

from pathlib import Path

from scripts.kindle_dashboard import serve
from scripts.kindle_dashboard.health import Health


def _fresh(tmp_path: Path, key: Path | None = None) -> Health:
    """A Health writing its state file under tmp so tests don't touch real state."""
    h = Health(signing_key=key)
    # redirect the module-level state file for this instance's _persist
    import scripts.kindle_dashboard.health as health_mod

    health_mod._HEALTH_FILE = tmp_path / "health.json"
    return h


def test_render_failure_then_recovery_tracks_failing_since(tmp_path):
    h = _fresh(tmp_path)
    assert h.snapshot()["render"]["healthy"] is False  # never rendered yet

    h.render_failed("boom")
    snap = h.snapshot()
    assert snap["render"]["consecutive_failures"] == 1
    first_since = snap["render"]["failing_since"]
    assert first_since is not None
    assert snap["render"]["last_error"] == "boom"

    h.render_failed("boom again")
    snap = h.snapshot()
    assert snap["render"]["consecutive_failures"] == 2
    # failing_since pins to the FIRST failure of the streak, not the latest
    assert snap["render"]["failing_since"] == first_since

    h.render_ok()
    snap = h.snapshot()
    assert snap["render"]["consecutive_failures"] == 0
    assert snap["render"]["failing_since"] is None
    assert snap["render"]["last_error"] is None
    assert snap["render"]["healthy"] is True


def test_status_degraded_until_both_render_and_signing_ok(tmp_path):
    key = tmp_path / "sign-ec.key"
    h = _fresh(tmp_path, key=key)

    # key missing + no render -> degraded
    assert h.snapshot()["status"] == "degraded"

    key.write_text("not-a-real-key")  # present on disk
    h.render_ok()
    h.sign_ok()
    snap = h.snapshot()
    assert snap["signing"]["key_present"] is True
    assert snap["status"] == "ok"

    # a signing failure flips it back to degraded even with the key present
    h.sign_failed("openssl rc=1")
    assert h.snapshot()["status"] == "degraded"


def test_state_file_is_written_atomically(tmp_path):
    import json

    import scripts.kindle_dashboard.health as health_mod

    h = _fresh(tmp_path)
    h.render_failed("disk check")
    written = json.loads((tmp_path / "health.json").read_text())
    assert written["render"]["last_error"] == "disk check"
    assert health_mod._HEALTH_FILE == tmp_path / "health.json"


def test_stale_message_names_the_streak(tmp_path):
    h = _fresh(tmp_path)
    h.render_failed("x")
    h.render_failed("x")
    msg = h.stale_message()
    assert "STALE" in msg
    assert "2 forsok" in msg  # surfaces the failure count on the wall display


def test_control_sh_refuses_to_serve_unsigned_by_default(tmp_path, monkeypatch):
    """A missing signing key must yield 503, not a 200 unsigned body the device
    silently rejects (the 2026-06-22 restore-gap failure mode)."""
    monkeypatch.setattr(serve, "_SIGN_KEY", tmp_path / "absent.key")
    monkeypatch.delenv("KINDLE_ALLOW_UNSIGNED", raising=False)

    resp = serve.control_sh()
    assert resp.status_code == 503
    assert resp.headers.get("X-Control-Unsigned") == "1"
    assert "X-Control-Sig" not in resp.headers


def test_control_sh_unsigned_escape_hatch(tmp_path, monkeypatch):
    """KINDLE_ALLOW_UNSIGNED=1 serves the body (200) for deliberate rotation,
    still flagged X-Control-Unsigned so it's never mistaken for signed."""
    monkeypatch.setattr(serve, "_SIGN_KEY", tmp_path / "absent.key")
    monkeypatch.setenv("KINDLE_ALLOW_UNSIGNED", "1")

    resp = serve.control_sh()
    assert resp.status_code == 200
    assert resp.headers.get("X-Control-Unsigned") == "1"
    assert "X-Control-Sig" not in resp.headers
