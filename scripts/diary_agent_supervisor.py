#!/usr/bin/python3
"""Keep an agent web service active while its runtime child joins turn scopes."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    result.add_argument("command", nargs=argparse.REMAINDER)
    return result


def main(argv: list[str] | None = None) -> int:
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        arg_parser.error("a runtime command is required")
    child = subprocess.Popen(command)

    def forward(signum: int, _frame: object) -> None:
        try:
            os.kill(child.pid, signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
