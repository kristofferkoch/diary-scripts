"""Tests for the diary-sync broker — the allow-list is the security boundary."""
from __future__ import annotations

import json
import socket
import threading

import pytest

import scripts.diary_sync_broker as broker


def _limiters(sync_burst: int = 6) -> tuple[broker.Limiter, broker.Limiter]:
    return broker.Limiter(sync_burst, 3600.0), broker.Limiter(120, 60.0)


def _handle(request, runner=None, sync_burst: int = 6):
    sync_limiter, status_limiter = _limiters(sync_burst)
    return broker.handle(
        request,
        sync_limiter=sync_limiter,
        status_limiter=status_limiter,
        busy=threading.Lock(),
        runner=runner or (lambda argv, timeout: {"ok": True, "output": "ran", "argv": " ".join(argv)}),
    )


def test_ping_does_not_touch_the_runner():
    def boom(argv, timeout):  # pragma: no cover - must not run
        raise AssertionError("ping must not run a command")

    reply = _handle({"cmd": "ping"}, runner=boom)
    assert reply["ok"] is True and reply["version"] == broker.VERSION


def test_unknown_cmd_is_rejected():
    assert _handle({"cmd": "exec", "argv": ["rm", "-rf", "/"]})["ok"] is False


def test_unknown_sync_target_is_rejected_and_never_run():
    def boom(argv, timeout):  # pragma: no cover - must not run
        raise AssertionError("rejected target must not reach the runner")

    reply = _handle({"cmd": "sync", "target": "../../bin/sh"}, runner=boom)
    assert reply["ok"] is False and "unknown sync target" in reply["error"]


def test_sync_runs_the_allowlisted_argv():
    seen: list[list[str]] = []

    def runner(argv, timeout):
        seen.append(list(argv))
        return {"ok": True, "output": "ok", "exit_code": 0}

    reply = _handle({"cmd": "sync", "target": "spond"}, runner=runner)
    assert seen == [list(broker.SYNC_TARGETS["spond"])]
    assert reply["target"] == "spond" and reply["ok"] is True


@pytest.mark.parametrize("payload", [
    {"cmd": "sync", "target": "spond", "argv": ["/bin/sh"]},
    {"cmd": "sync", "target": "spond", "cwd": "/"},
    {"cmd": "sync", "target": "spond", "env": {"DIARY_ROOT": "/"}},
    {"cmd": "start", "unit": "mail-sync.service", "path": "/etc/passwd"},
    {"cmd": "status", "paths": ["/etc", "/home"]},
    {"cmd": "ping", "output": "/tmp/agent-selected"},
])
def test_agent_selected_process_and_filesystem_arguments_are_rejected(payload):
    def boom(argv, timeout):  # pragma: no cover - must not run
        raise AssertionError("a request with extra fields must not reach the runner")

    reply = _handle(payload, runner=boom)
    assert reply["ok"] is False
    assert "unexpected request fields" in reply["error"]


def test_non_scalar_sync_target_is_rejected_without_running():
    def boom(argv, timeout):  # pragma: no cover - must not run
        raise AssertionError("a malformed target must not reach the runner")

    reply = _handle({"cmd": "sync", "target": ["spond"]}, runner=boom)
    assert reply["ok"] is False and "unknown sync target" in reply["error"]


def test_sync_all_runs_every_target():
    seen: list[str] = []

    def runner(argv, timeout):
        seen.append(argv[0])
        return {"ok": True, "output": "", "exit_code": 0}

    reply = _handle({"cmd": "sync", "target": "all"}, runner=runner)
    assert set(seen) == {"spond-sync", "vigilo-sync", "skolemelding-sync"}
    assert reply["ok"] is True and reply["target"] == "all"


def test_start_accepts_only_mail_sync():
    ok = _handle({"cmd": "start", "unit": "mail-sync.service"})
    assert ok["ok"] is True and ok["unit"] == "mail-sync.service"
    for unit in ("sshd.service", "mail-sync.timer", "anything"):
        assert _handle({"cmd": "start", "unit": unit})["ok"] is False, unit


def test_sync_is_rate_limited():
    sync_limiter, status_limiter = _limiters(sync_burst=2)
    lock = threading.Lock()
    replies = [
        broker.handle({"cmd": "sync", "target": "spond"},
                      sync_limiter=sync_limiter, status_limiter=status_limiter,
                      busy=lock,
                      runner=lambda argv, timeout: {"ok": True, "output": ""})
        for _ in range(3)
    ]
    assert [r["ok"] for r in replies] == [True, True, False]
    assert "rate limit" in replies[-1]["error"]


def test_busy_when_another_sync_runs():
    sync_limiter, status_limiter = _limiters()
    lock = threading.Lock()
    lock.acquire()  # simulate a running sync
    reply = broker.handle({"cmd": "sync", "target": "spond"},
                          sync_limiter=sync_limiter, status_limiter=status_limiter,
                          busy=lock, busy_wait=0,
                          runner=lambda argv, timeout: {"ok": True, "output": ""})
    assert reply["ok"] is False and "busy" in reply["error"]


def test_a_second_request_waits_for_the_slot_instead_of_failing():
    sync_limiter, status_limiter = _limiters()
    lock = threading.Lock()
    lock.acquire()

    def release_soon():
        threading.Event().wait(0.2)
        lock.release()

    threading.Thread(target=release_soon, daemon=True).start()
    reply = broker.handle({"cmd": "sync", "target": "spond"},
                          sync_limiter=sync_limiter, status_limiter=status_limiter,
                          busy=lock, busy_wait=5.0,
                          runner=lambda argv, timeout: {"ok": True, "output": ""})
    assert reply["ok"] is True


def test_status_reports_every_unit():
    reply = _handle({"cmd": "status"},
                    runner=lambda argv, timeout: {"ok": True, "output": "active\n"})
    assert reply["ok"] is True
    assert set(reply["units"]) == set(broker.STATUS_UNITS)
    assert reply["units"]["mail-sync.timer"]["active"] == "active"


def test_tail_truncates_with_a_marker():
    text = "x" * 5000
    out = broker.tail(text, limit=100)
    assert out.endswith("x" * 100) and out.startswith("…(truncated)…")


def test_call_raises_when_no_socket(tmp_path):
    with pytest.raises(broker.BrokerUnavailable):
        broker.call({"cmd": "ping"}, socket_path=str(tmp_path / "nope.sock"), timeout=1)


def test_socket_round_trip(tmp_path):
    sock_path = str(tmp_path / "s.sock")
    thread = threading.Thread(
        target=broker.serve, args=(sock_path,),
        kwargs={"runner": lambda argv, timeout: {"ok": True, "output": "ok"}},
        daemon=True)
    thread.start()
    for _ in range(100):
        try:
            reply = broker.call({"cmd": "ping"}, socket_path=sock_path, timeout=2)
            break
        except broker.BrokerUnavailable:
            threading.Event().wait(0.05)
    else:  # pragma: no cover - only on a badly broken broker
        pytest.fail("broker never came up")
    assert reply["ok"] is True and reply["cmd"] == "ping"

    status = broker.call({"cmd": "status"}, socket_path=sock_path, timeout=5)
    assert status["ok"] is True and "mail-sync.timer" in status["units"]


def test_local_fallback_status_uses_systemctl(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, timeout=broker.SYNC_TIMEOUT):
        calls.append(list(argv))
        return {"ok": True, "output": "active"}

    monkeypatch.setattr(broker, "run_command", fake_run)
    reply = broker._local("status", {})
    assert reply["ok"] is True
    assert any("is-active" in c for c in calls)


def test_client_prints_json(monkeypatch, capsys, tmp_path):
    sock_path = str(tmp_path / "s.sock")
    thread = threading.Thread(
        target=broker.serve, args=(sock_path,),
        kwargs={"runner": lambda argv, timeout: {"ok": True, "output": "ok"}},
        daemon=True)
    thread.start()
    for _ in range(100):
        try:
            broker.call({"cmd": "ping"}, socket_path=sock_path, timeout=1)
            break
        except broker.BrokerUnavailable:
            threading.Event().wait(0.05)
    assert broker.client_main(["--socket", sock_path, "--json", "status"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["cmd"] == "status"


def test_global_flags_are_accepted_after_the_subcommand():
    assert broker._normalise_argv(["status", "--json", "--fallback"]) == [
        "--json", "--fallback", "status"]
    assert broker._normalise_argv(["--json", "status"]) == ["--json", "status"]


def test_client_status_with_flags_after_the_subcommand(monkeypatch, capsys, tmp_path):
    sock_path = str(tmp_path / "s.sock")
    thread = threading.Thread(
        target=broker.serve, args=(sock_path,),
        kwargs={"runner": lambda argv, timeout: {"ok": True, "output": "active"}},
        daemon=True)
    thread.start()
    for _ in range(100):
        try:
            broker.call({"cmd": "ping"}, socket_path=sock_path, timeout=1)
            break
        except broker.BrokerUnavailable:
            threading.Event().wait(0.05)
    # the shape sjekk-collect uses
    assert broker.client_main(["--socket", sock_path, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["cmd"] == "status"
