"""Tests for the sudo broker: in-process prompt tests plus a real daemon on a pty.

The prompt/bell behaviour is exercised **in process** (no root needed): the
timer constants are patched and `sys.stdin`/`sys.stdout`/`sys.stderr` are
pointed at a pty or at `StringIO`, then `approve()` is called directly.

The daemon itself refuses to run without root and a terminal, so the end-to-end
tests drive a real daemon on a pty, typing `y`/`n` the way a human would:

    sudo /usr/bin/python3 scripts/test_sudo_broker.py

Under pytest the end-to-end ones are skipped unless euid is 0. They use their own
socket path (SUDO_BROKER_SOCK), so a broker you have open in another terminal is
untouched.
"""

from __future__ import annotations

import io
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:  # under pytest the repo root is on sys.path (`scripts` is a package)
    from scripts import sudo_broker
except ImportError:  # running this file directly: scripts/ is sys.path[0]
    import sudo_broker  # type: ignore[no-redef]

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
        self.last_tty = b""  # everything the daemon wrote to the pty last time
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
        # The bell follows the prompt immediately; keep it for the bell test.
        # Usually it already arrived with the prompt, so only read on if it did
        # not — otherwise every ask() would sit out the full timeout.
        rest = b"" if b"\a" in prompt else _read_until(self.master, b"\a", timeout=1.0)
        self.last_tty = prompt + rest
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


class _Patch:
    """Temporarily set attributes, restoring them afterwards (no pytest needed)."""

    def __init__(self) -> None:
        self._saved: list[tuple[object, str, object]] = []

    def setattr(self, obj: object, name: str, value: object) -> None:
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        while self._saved:
            obj, name, value = self._saved.pop()
            setattr(obj, name, value)


class _TtySink:
    """A pipe that claims to be a terminal, so bell() writes into it."""

    def __init__(self) -> None:
        self._read, self._write = os.pipe()

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> None:
        os.write(self._write, text.encode())

    def flush(self) -> None:
        pass

    def close(self) -> None:
        for fd in (self._read, self._write):
            try:
                os.close(fd)
            except OSError:
                pass

    def output(self) -> bytes:
        """Everything written to the pipe so far."""
        os.set_blocking(self._read, False)
        chunks = []
        while True:
            try:
                chunk = os.read(self._read, 4096)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _written(stream) -> bytes:
    """Everything written to a prompt stream, whichever kind it is."""
    if isinstance(stream, _TtySink):
        return stream.output()
    return stream.getvalue().encode()


class PromptProbe:
    """Stand-in for the operator's terminal during one in-process prompt.

    No pty needed: stdin is a pipe (select() and readline() work on it, and the
    answer is written into it like a human typing), and each output stream is
    either a pipe that reports isatty() or a StringIO standing in for a
    redirect. That is enough to pin down routing *and* suppression.
    """

    def __init__(self, *, stdout_tty: bool = True, stderr_tty: bool = False) -> None:
        self._patched = _Patch()
        stdin_read, self._stdin = os.pipe()
        self.stdin = os.fdopen(stdin_read, "r")
        self.stdout = _TtySink() if stdout_tty else io.StringIO()
        self.stderr = _TtySink() if stderr_tty else io.StringIO()

    def type(self, answer: bytes) -> None:
        """Write into the prompt as if the human typed it."""
        os.write(self._stdin, answer)

    def run(
        self, *, prompt_timeout: float = 0.5, bell_interval: float = 0.1
    ) -> tuple[bool, str]:
        self._patched.setattr(sudo_broker, "PROMPT_TIMEOUT", prompt_timeout)
        self._patched.setattr(sudo_broker, "BELL_INTERVAL", bell_interval)
        self._patched.setattr(sys, "stdin", self.stdin)
        self._patched.setattr(sys, "stdout", self.stdout)
        self._patched.setattr(sys, "stderr", self.stderr)
        return sudo_broker.approve((1234, 0, 0, "tester"), "true", "/")

    def close(self) -> None:
        self._patched.restore()  # put sys.* and the timers back first
        self.stdin.close()
        for stream in (self.stdout, self.stderr):
            stream.close()
        try:
            os.close(self._stdin)  # the write end of the stdin pipe
        except OSError:
            pass


def test_prompt_rings_the_bell_and_keeps_ringing_until_answered() -> None:
    prompt = PromptProbe()
    try:
        started = time.monotonic()
        ok, why = prompt.run(prompt_timeout=0.5, bell_interval=0.1)
        elapsed = time.monotonic() - started
        written = _written(prompt.stdout) + _written(prompt.stderr)
    finally:
        prompt.close()
    assert (ok, why) == (False, "timed out waiting for approval")
    assert 0.3 <= elapsed < 2.0, f"the prompt did not respect PROMPT_TIMEOUT: {elapsed:.2f}s"
    assert written.count(b"\a") >= 3, f"the bell should repeat while unanswered: {written!r}"


def test_answered_prompt_rings_once_and_approves() -> None:
    prompt = PromptProbe()
    try:
        prompt.type(b"y\n")
        ok, why = prompt.run(prompt_timeout=5.0, bell_interval=0.5)
        written = _written(prompt.stdout) + _written(prompt.stderr)
    finally:
        prompt.close()
    assert (ok, why) == (True, "")
    assert written.count(b"\a") == 1, f"an answered prompt should ring once: {written!r}"


def test_bell_follows_stdout_when_stderr_is_redirected() -> None:
    # The audit setup: stderr -> file, so the terminal is stdout.
    prompt = PromptProbe(stdout_tty=True, stderr_tty=False)
    try:
        prompt.type(b"y\n")
        ok, _why = prompt.run(prompt_timeout=5.0, bell_interval=0.5)
        stdout_bytes = _written(prompt.stdout)
        stderr_bytes = _written(prompt.stderr)
    finally:
        prompt.close()
    assert ok
    assert b"\a" in stdout_bytes, "the bell must follow stdout when stderr is redirected"
    assert b"\a" not in stderr_bytes, "a redirected stderr must stay free of control bytes"


def test_bell_falls_back_to_stderr_when_stdout_is_redirected() -> None:
    prompt = PromptProbe(stdout_tty=False, stderr_tty=True)
    try:
        prompt.type(b"y\n")
        ok, _why = prompt.run(prompt_timeout=5.0, bell_interval=0.5)
        stdout_bytes = _written(prompt.stdout)
        stderr_bytes = _written(prompt.stderr)
    finally:
        prompt.close()
    assert ok
    assert b"\a" in stderr_bytes, "the bell must follow stderr when stdout is redirected"
    assert b"\a" not in stdout_bytes


def test_no_bell_when_neither_stream_is_a_tty() -> None:
    prompt = PromptProbe(stdout_tty=False, stderr_tty=False)
    try:
        prompt.type(b"y\n")
        ok, _why = prompt.run(prompt_timeout=5.0, bell_interval=0.5)
        stdout_bytes = _written(prompt.stdout)
        stderr_bytes = _written(prompt.stderr)
    finally:
        prompt.close()
    assert ok
    assert b"\a" not in stdout_bytes and b"\a" not in stderr_bytes, (
        "no bell may be emitted into a log"
    )


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
def test_request_rings_the_terminal_bell() -> None:
    broker = Broker()
    try:
        res = broker.ask("true", b"y\n")
        assert res.returncode == 0, res.stderr.decode()
        assert b"\a" in broker.last_tty, (
            "the prompt did not ring the terminal bell; daemon said: "
            f"{broker.last_tty!r}"
        )
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
