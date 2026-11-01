"""Tests for stopping every legacy writer before migration."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.legacy_state_quiesce import BROKER, TIMERS, assert_quiesced, quiesce


class Systemctl:
    def __init__(self, units):
        self.units = units
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "show"]:
            unit, prop = argv[2], argv[4]
            state = self.units.get(unit, {"LoadState": "not-found", "ActiveState": "inactive"})
            return SimpleNamespace(returncode=0, stdout=state[prop] + "\n")
        if argv[:2] == ["systemctl", "disable"]:
            self.units[argv[-1]]["ActiveState"] = "inactive"
        if argv[:2] == ["systemctl", "stop"]:
            self.units[argv[-1]]["ActiveState"] = "inactive"
        return SimpleNamespace(returncode=0, stdout="")


def test_active_timers_and_writers_are_stopped_before_migration():
    units = {
        **{unit: {"LoadState": "loaded", "ActiveState": "active"} for unit in TIMERS},
        BROKER: {"LoadState": "loaded", "ActiveState": "active"},
        "diary-collect@spond.service": {"LoadState": "loaded", "ActiveState": "active"},
    }
    systemctl = Systemctl(units)
    quiesce(runner=systemctl)
    for unit in TIMERS:
        assert ["systemctl", "disable", "--now", unit] in systemctl.calls
    assert ["systemctl", "stop", "diary-collect@spond.service"] in systemctl.calls
    assert ["systemctl", "stop", BROKER] in systemctl.calls
    assert_quiesced(runner=systemctl)


def test_missing_units_are_noops():
    systemctl = Systemctl({})
    quiesce(runner=systemctl)
    assert not any(call[:2] == ["systemctl", "disable"] for call in systemctl.calls)
    assert not any(call[:2] == ["systemctl", "stop"] for call in systemctl.calls)


def test_active_unit_after_stop_attempt_fails_preflight():
    units = {BROKER: {"LoadState": "loaded", "ActiveState": "active"}}
    systemctl = Systemctl(units)
    with pytest.raises(RuntimeError, match="still active"):
        assert_quiesced(runner=systemctl)


def test_genuine_systemctl_failure_is_fatal():
    def failing(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="")

    with pytest.raises(RuntimeError, match="cannot inspect"):
        quiesce(runner=failing)


def test_disable_failure_is_fatal():
    class FailingDisable(Systemctl):
        def __call__(self, argv, **kwargs):
            if argv[:2] == ["systemctl", "disable"]:
                return SimpleNamespace(returncode=1, stdout="")
            return super().__call__(argv, **kwargs)

    units = {TIMERS[0]: {"LoadState": "loaded", "ActiveState": "active"}}
    with pytest.raises(RuntimeError, match="cannot disable and stop"):
        quiesce(runner=FailingDisable(units))


def test_migration_entry_point_quiesces_before_any_copy(monkeypatch):
    from scripts import legacy_state_install

    calls = []
    monkeypatch.setattr(legacy_state_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        "scripts.legacy_state_quiesce.quiesce", lambda: calls.append("quiesce")
    )
    monkeypatch.setattr(
        legacy_state_install, "migrate_legacy_state",
        lambda **kwargs: calls.append("migrate") or {"copied": 0},
    )
    legacy_state_install.main()
    assert calls == ["quiesce", "migrate"]
