"""Stop and disable every legacy collector writer before state migration."""
from __future__ import annotations

import os
import subprocess

TIMERS = (
    "diary-collect-spond.timer",
    "diary-collect-vigilo.timer",
    "diary-collect-skolemelding.timer",
)
COLLECTORS = tuple(f"diary-collect@{track}.service" for track in ("spond", "vigilo", "skolemelding"))
BROKER = "diary-sync-broker.service"
STOPPED_STATES = {"inactive", "dead", "failed"}


def _show(unit: str, property_name: str, runner) -> str:
    result = runner(
        ["systemctl", "show", unit, "-p", property_name, "--value"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"cannot inspect collector unit state: {unit}")
    return result.stdout.strip()


def _exists(unit: str, runner) -> bool:
    state = _show(unit, "LoadState", runner)
    if state == "not-found":
        return False
    if state in {"loaded", "masked"}:
        return True
    raise RuntimeError(f"cannot load collector unit: {unit}")


def quiesce(*, runner=subprocess.run) -> None:
    """Disable old timers and stop all old writers, failing on real errors."""
    for unit in TIMERS:
        if _exists(unit, runner):
            result = runner(["systemctl", "disable", "--now", unit], check=False)
            if result.returncode:
                raise RuntimeError(f"cannot disable and stop collector timer: {unit}")
    for unit in (*COLLECTORS, BROKER):
        if _exists(unit, runner) and _show(unit, "ActiveState", runner) not in STOPPED_STATES:
            result = runner(["systemctl", "stop", unit], check=False)
            if result.returncode:
                raise RuntimeError(f"cannot stop legacy collector writer: {unit}")
    assert_quiesced(runner=runner)


def assert_quiesced(*, runner=subprocess.run) -> None:
    for unit in (*TIMERS, *COLLECTORS, BROKER):
        if _exists(unit, runner) and _show(unit, "ActiveState", runner) not in STOPPED_STATES:
            raise RuntimeError(f"legacy collector writer is still active: {unit}")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("collector quiescing must run as root")
    quiesce()


if __name__ == "__main__":
    main()
