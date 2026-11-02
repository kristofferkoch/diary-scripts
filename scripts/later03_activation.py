#!/usr/bin/python3
"""Validate owner-only LATER-03 activation evidence and integrated cgroup units."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

DEFAULT_EVIDENCE = "/etc/diary/later03-activation.conf"
DEFAULT_DIARY_REPO = "/home/kek/diary-approved"
DEFAULT_SCRIPTS_REPO = "/home/kek/diary-scripts-approved"
AUDIT_SET = "inet:diary_agent_audit:agent_egress"
SCOPE_PREFIXES = "dsh-turn-,kimi-turn-"
OID_RE = re.compile(r"[0-9a-f]{40}")
SET_RE = re.compile(r"(inet|ip|ip6):[A-Za-z0-9_-]+:[A-Za-z0-9_-]+")
FLAGS = {
    "ACT01_ACCEPTED": "yes",
    "CTL02_LIVE_VERIFIED": "yes",
    "SCOPE_PREFIXES": SCOPE_PREFIXES,
}
OID_KEYS = (
    "ACT01_DIARY_OID",
    "ACT01_SCRIPTS_OID",
    "CTL02_DIARY_OID",
    "CTL02_SCRIPTS_OID",
    "INTEGRATED_DIARY_OID",
    "INTEGRATED_SCRIPTS_OID",
)
REQUIRED_KEYS = frozenset({*FLAGS, *OID_KEYS, "ENFORCEMENT_SETS"})


class ActivationError(ValueError):
    """Activation evidence or integrated configuration cannot be trusted."""


def _regular_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ActivationError(f"missing integration file: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ActivationError(f"integration file is not regular: {path}")


def read_evidence(path: Path, expected_uid: int = 0) -> dict[str, str]:
    try:
        parent = path.parent.stat()
        info = path.lstat()
    except OSError as exc:
        raise ActivationError("activation evidence is unavailable") from exc
    if parent.st_uid != expected_uid or parent.st_mode & 0o022:
        raise ActivationError("activation evidence directory is not owner-controlled")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise ActivationError("activation evidence must be a non-symlink owner-only 0600 file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ActivationError("activation evidence is unreadable") from exc
    if len(raw) > 4096:
        raise ActivationError("activation evidence is oversized")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ActivationError("activation evidence is not ASCII") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in values or not value:
            raise ActivationError("activation evidence has malformed or duplicate fields")
        values[key] = value
    if set(values) != REQUIRED_KEYS:
        raise ActivationError("activation evidence has missing or unknown fields")
    for key, expected in FLAGS.items():
        if values[key] != expected:
            raise ActivationError(f"activation evidence does not prove {key}")
    for key in OID_KEYS:
        if not OID_RE.fullmatch(values[key]):
            raise ActivationError(f"activation evidence has invalid {key}")
    return values


def enforcement_sets(value: str) -> tuple[str, ...]:
    sets = tuple(value.split(","))
    if not sets or any(not SET_RE.fullmatch(item) for item in sets):
        raise ActivationError("activation evidence has invalid enforcement sets")
    if len(set(sets)) != len(sets) or AUDIT_SET in sets:
        raise ActivationError("enforcement sets must be distinct from each other and the audit set")
    return sets


def _git(
    repo: Path,
    args: list[str],
    runner: Callable[..., Any],
) -> Any:
    try:
        result = runner(
            ["/usr/bin/git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActivationError(f"cannot inspect approved repository {repo}") from exc
    return result


def _check_repo(
    repo: Path,
    integrated_key: str,
    prerequisite_keys: tuple[str, str],
    values: dict[str, str],
    runner: Callable[..., Any],
) -> None:
    result = _git(repo, ["rev-parse", "--verify", "HEAD^{commit}"], runner)
    if result.returncode != 0 or result.stdout.strip() != values[integrated_key]:
        raise ActivationError(f"approved HEAD does not match {integrated_key}")
    for key in prerequisite_keys:
        result = _git(repo, ["merge-base", "--is-ancestor", values[key], values[integrated_key]], runner)
        if result.returncode != 0:
            raise ActivationError(f"{key} is not an ancestor of {integrated_key}")


def _unit_text(root: Path, relative: str) -> str:
    path = root / relative
    _regular_file(path)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ActivationError(f"cannot read integration unit {path}") from exc


def _exact_set_collection(actual: list[str], expected: list[str], component: str) -> None:
    if len(actual) != len(set(actual)):
        raise ActivationError(f"{component} contains duplicate cgroup sets")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing or extra:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ActivationError(f"{component} set collection mismatch ({'; '.join(details)})")


def _slice_sets(unit_text: str) -> list[str]:
    values: list[str] = []
    for line in unit_text.splitlines():
        line = line.strip()
        if not line.startswith("NFTSet="):
            continue
        value = line.removeprefix("NFTSet=")
        if not value.startswith("cgroup:") or not SET_RE.fullmatch(value.removeprefix("cgroup:")):
            raise ActivationError("agent.slice contains a malformed or non-cgroup NFTSet")
        values.append(value)
    return values


def _controller_sets(unit_text: str) -> list[str]:
    values: list[str] = []
    for line in unit_text.splitlines():
        line = line.strip()
        if not line.startswith("ExecStart="):
            continue
        try:
            tokens = shlex.split(line.removeprefix("ExecStart="))
        except ValueError as exc:
            raise ActivationError("diary-lockd has malformed ExecStart quoting") from exc
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--cgroup-set":
                if index + 1 >= len(tokens):
                    raise ActivationError("diary-lockd has an empty --cgroup-set argument")
                value = tokens[index + 1]
                index += 2
            elif token.startswith("--cgroup-set="):
                value = token.partition("=")[2]
                index += 1
            else:
                index += 1
                continue
            if not SET_RE.fullmatch(value):
                raise ActivationError("diary-lockd has a malformed --cgroup-set argument")
            values.append(value)
    return values


def validate_units(
    diary_repo: Path,
    scripts_repo: Path,
    required_sets: tuple[str, ...],
) -> None:
    slice_text = _unit_text(diary_repo, "systemd/units/agent.slice")
    controller_unit = _unit_text(
        diary_repo, "systemd/units/diary-lockd.service.d/60-cgroup-identity.conf",
    )
    expected = [AUDIT_SET, *required_sets]
    _exact_set_collection(_slice_sets(slice_text), [f"cgroup:{item}" for item in expected], "agent.slice")
    _exact_set_collection(_controller_sets(controller_unit), expected, "diary-lockd")
    for relative in ("dsh/dsh-web.service", "kimi/kimi-web.service"):
        if "Slice=agent.slice" not in _unit_text(diary_repo, relative).splitlines():
            raise ActivationError(f"{relative} is not integrated with agent.slice")
    controller_source = _unit_text(scripts_repo, "scripts/diary_lock.py")
    for marker in (
        "def _turn_scopes", "def verify_scope", "self.identity.verify()",
        "dsh-turn-", "kimi-turn-",
    ):
        if marker not in controller_source:
            raise ActivationError(f"integrated controller lacks {marker}")


def validate_activation(
    evidence: Path = Path(DEFAULT_EVIDENCE),
    diary_repo: Path = Path(DEFAULT_DIARY_REPO),
    scripts_repo: Path = Path(DEFAULT_SCRIPTS_REPO),
    expected_uid: int = 0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    values = read_evidence(evidence, expected_uid)
    sets = enforcement_sets(values["ENFORCEMENT_SETS"])
    _check_repo(
        diary_repo,
        "INTEGRATED_DIARY_OID",
        ("ACT01_DIARY_OID", "CTL02_DIARY_OID"),
        values,
        runner,
    )
    _check_repo(
        scripts_repo,
        "INTEGRATED_SCRIPTS_OID",
        ("ACT01_SCRIPTS_OID", "CTL02_SCRIPTS_OID"),
        values,
        runner,
    )
    validate_units(diary_repo, scripts_repo, sets)
    return {"enforcement_sets": sets, "scope_prefixes": SCOPE_PREFIXES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE)
    parser.add_argument("--diary-repo", default=DEFAULT_DIARY_REPO)
    parser.add_argument("--scripts-repo", default=DEFAULT_SCRIPTS_REPO)
    args = parser.parse_args(argv)
    try:
        result = validate_activation(Path(args.evidence), Path(args.diary_repo), Path(args.scripts_repo))
    except ActivationError as exc:
        print(f"LATER-03 activation refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
