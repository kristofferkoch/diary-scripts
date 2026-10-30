"""End-to-end test for the sudo broker: a real daemon on a pty, simulated operator.

The daemon refuses to run without root and a terminal, so this test needs both —
it drives the pty itself, typing `y`/`n` the way a human would:

    sudo /usr/bin/python3 scripts/test_sudo_broker.py

Under pytest it is skipped unless euid is 0. It uses its own socket path
(SUDO_BROKER_SOCK), so a broker you have open in another terminal is untouched.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAEMON = HERE / "sudo_broker.py"
CLIENT = HERE / "sudo_ask.py"


def _read_until(fd: int, needle: bytes, timeout: float = 10.0) -> bytes:
    """Read from the pty master until `needle` shows up (or time out)."""
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


class Broker:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sudo-broker-test-")
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.env = {**os.environ, "SUDO_BROKER_SOCK": self.sock}
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            [sys.executable, str(DAEMON)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=self.env,
        )
        os.close(slave)
        deadline = time.monotonic() + 10.0
        while not os.path.exists(self.sock) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not os.path.exists(self.sock):
            self.stop()
            raise RuntimeError("daemon did not create its socket")

    def ask(self, command: str, answer: bytes) -> subprocess.CompletedProcess:
        """Run the client, answer the prompt with `answer`, return the client result."""
        client = subprocess.Popen(
            [sys.executable, str(CLIENT), command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        prompt = _read_until(self.master, b"[y/N]")
        assert b"[y/N]" in prompt, f"no prompt appeared; daemon said: {prompt!r}"
        os.write(self.master, answer)
        out, err = client.communicate(timeout=20)
        return subprocess.CompletedProcess(client.args, client.returncode, out, err)

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.close(self.master)


try:  # keep the file runnable without pytest: sudo /usr/bin/python3 test_sudo_broker.py
    import pytest

    requires_root = pytest.mark.skipif(
        os.geteuid() != 0, reason="the broker daemon requires root and a tty"
    )
except ImportError:  # pragma: no cover - environment without pytest

    def requires_root(fn):  # type: ignore[no-redef]
        return fn


@requires_root
def test_approved_command_runs_and_streams_exit_code() -> None:
    broker = Broker()
    try:
        res = broker.ask("echo BROKER_OK && exit 7", b"y\n")
        assert res.returncode == 7, res.stderr.decode()
        assert b"BROKER_OK" in res.stdout
        assert b"approved" in res.stderr.lower()
    finally:
        broker.stop()


@requires_root
def test_denied_command_does_not_run() -> None:
    broker = Broker()
    marker = Path(broker.tmp) / "must-not-exist"
    try:
        res = broker.ask(f"touch {marker}", b"n\n")
        assert res.returncode == 1, res.stderr.decode()
        assert not marker.exists(), "a denied command was executed"
        assert b"denied" in res.stderr.lower()
    finally:
        broker.stop()


@requires_root
def test_empty_answer_is_a_denial() -> None:
    broker = Broker()
    marker = Path(broker.tmp) / "enter-key-must-not-approve"
    try:
        res = broker.ask(f"touch {marker}", b"\n")
        assert res.returncode == 1
        assert not marker.exists()
    finally:
        broker.stop()


@requires_root
def test_client_without_broker_fails_closed() -> None:
    env = {**os.environ, "SUDO_BROKER_SOCK": "/nonexistent/agent.sock"}
    res = subprocess.run(
        [sys.executable, str(CLIENT), "id"],
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert res.returncode == 2
    assert b"no broker running" in res.stderr


def main() -> int:
    if os.geteuid() != 0:
        print("run as root: sudo /usr/bin/python3 scripts/test_sudo_broker.py", file=sys.stderr)
        return 2
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
