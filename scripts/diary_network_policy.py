#!/usr/bin/python3
"""Apply and verify dark, locked, or attended agent nftables policy."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, cast

TABLE = "diary_agent_policy"
LEGACY_TABLE = "diary_agent_audit"
ARM_FILE = "/etc/diary/network-policy-enabled"
MODES = frozenset({"dark", "locked", "attended"})
ANY = object()


class PolicyError(ValueError):
    """The live firewall does not prove the requested policy."""


def meta_match(key: str, value: Any) -> dict[str, Any]:
    return {"match": {"op": "==", "left": {"meta": {"key": key}}, "right": value}}


def ct_match(state: str) -> dict[str, Any]:
    return {"match": {"op": "in", "left": {"ct": {"key": "state"}}, "right": state}}


def nflog(prefix: str) -> dict[str, Any]:
    return {"log": {"prefix": prefix, "group": 30, "snaplen": 256}}


def policy_spec(mode: str, uid: int) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    if mode not in MODES or isinstance(uid, bool) or not 0 <= uid <= 2_147_483_647:
        raise PolicyError("invalid network policy mode or agent UID")
    identity = [meta_match("skuid", uid), meta_match("l4proto", {"set": ["tcp", "udp"]})]
    loopback = [[meta_match("oifname", "lo"), {"return": None}]]
    new = [nflog(f"diary-{mode}-new")]
    established = [
        {"counter": ANY},
        {
            "limit": {
                "rate": 10,
                "per": "second",
                "burst": 20,
            }
        },
        nflog(f"diary-{mode}-established"),
    ]
    if mode == "attended":
        rules = loopback + [
            identity + [ct_match("new"), {"counter": ANY}, nflog("diary-attended-new")],
        ]
    else:
        rules = loopback + [
            identity + [ct_match("new"), {"counter": ANY}, *new],
            identity + [ct_match("established"), *established],
        ]
        if mode == "locked":
            rules = [rule + [{"drop": None}] for rule in rules[1:]]
            rules.insert(0, loopback[0])
            rules.append(identity + [{"counter": ANY}, {"drop": None}])
    chain = {
        "family": "inet",
        "table": TABLE,
        "name": "output",
        "type": "filter",
        "hook": "output",
        "prio": -10,
        "policy": "accept",
    }
    return chain, rules


def ruleset(mode: str, uid: int) -> str:
    policy_spec(mode, uid)
    lines = [
        f"destroy table inet {LEGACY_TABLE}",
        f"destroy table inet {TABLE}",
        f"table inet {TABLE} {{",
        "    chain output {",
        "        type filter hook output priority filter - 10; policy accept;",
        '        oifname "lo" return',
    ]
    selector = f"meta skuid {uid} meta l4proto {{ tcp, udp }}"
    if mode == "attended":
        lines.append(
            f'        {selector} ct state new counter log prefix "diary-attended-new" '
            "group 30 snaplen 256"
        )
    else:
        verdict = " drop" if mode == "locked" else ""
        lines.extend([
            f'        {selector} ct state new counter log prefix "diary-{mode}-new" '
            f"group 30 snaplen 256{verdict}",
            f'        {selector} ct state established counter limit rate 10/second burst 20 packets '
            f'log prefix "diary-{mode}-established" group 30 snaplen 256{verdict}',
        ])
        if mode == "locked":
            lines.append(f"        {selector} counter drop")
    lines.extend(["    }", "}", ""])
    return "\n".join(lines)


def expression_matches(actual: Any, expected: Any) -> bool:
    if expected is ANY:
        return True
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return False
        return all(expression_matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(expression_matches(a, e) for a, e in zip(actual, expected))
        )
    return actual == expected


def rule_matches(actual: dict[str, Any], expected: list[dict[str, Any]]) -> bool:
    if set(actual) != {"expr"}:
        return False
    expressions = actual["expr"]
    if not isinstance(expressions, list) or len(expressions) != len(expected):
        return False
    for actual_expression, expected_expression in zip(expressions, expected):
        if not isinstance(actual_expression, dict):
            return False
        actual_expression = cast(dict[str, Any], actual_expression)
        if set(actual_expression) == {"limit"} and set(expected_expression) == {"limit"}:
            actual_limit = actual_expression["limit"]
            expected_limit = expected_expression["limit"]
            if not isinstance(actual_limit, dict) or not isinstance(expected_limit, dict):
                return False
            if any(actual_limit.get(key) != value for key, value in expected_limit.items()):
                return False
        elif not expression_matches(actual_expression, expected_expression):
            return False
    return True


def verify_document(document: Any, mode: str, uid: int) -> None:
    expected_chain, expected_rules = policy_spec(mode, uid)
    try:
        objects = document["nftables"]
        if not isinstance(objects, list) or any(not isinstance(item, dict) for item in objects):
            raise PolicyError("nftables response has an unexpected object layout")
        tables = [item["table"] for item in objects if "table" in item]
        chains = [item["chain"] for item in objects if "chain" in item]
        rules = [item["rule"] for item in objects if "rule" in item]
        if any(set(item) - {"metainfo", "table", "chain", "rule"} for item in objects):
            raise PolicyError("nftables response contains an unexpected object")
        if len(tables) != 1 or len(chains) != 1 or len(rules) != len(expected_rules):
            raise PolicyError("nftables response has an unexpected object count")
        if any(not isinstance(item, dict) for item in [tables[0], chains[0], *rules]):
            raise PolicyError("nftables response contains a malformed object")
    except (KeyError, TypeError) as exc:
        raise PolicyError("nftables response has an unexpected object layout") from exc
    table = tables[0]
    if table.get("family") != "inet" or table.get("name") != TABLE:
        raise PolicyError("unexpected nftables table")
    chain = {key: value for key, value in chains[0].items() if key in expected_chain}
    if chain != expected_chain:
        raise PolicyError("agent output base chain does not match the requested policy")
    for index, (rule, expected) in enumerate(zip(rules, expected_rules), start=1):
        rule = {key: value for key, value in rule.items() if key in {"family", "table", "chain", "expr"}}
        if rule.get("family") != "inet" or rule.get("table") != TABLE or rule.get("chain") != "output":
            raise PolicyError(f"rule {index} belongs to an unexpected chain")
        if "expr" not in rule or not rule_matches({"expr": rule["expr"]}, expected):
            raise PolicyError(f"rule {index} does not match {mode} policy")


def tables_document_has_only_policy(document: Any) -> bool:
    try:
        objects = document["nftables"]
        if not isinstance(objects, list) or any(not isinstance(item, dict) for item in objects):
            raise PolicyError("cannot parse nftables table inventory")
        tables = [item["table"] for item in objects if "table" in item]
        if any(set(item) - {"metainfo", "table"} for item in objects):
            raise PolicyError("nftables table inventory contains an unexpected object")
        if any(not isinstance(table, dict) for table in tables):
            raise PolicyError("nftables table inventory contains a malformed table")
    except (KeyError, TypeError) as exc:
        raise PolicyError("cannot parse nftables table inventory") from exc
    return not any(
        table.get("family") == "inet" and table.get("name") == LEGACY_TABLE
        for table in tables
    )


class NetworkPolicy:
    def __init__(
        self,
        uid: int,
        arm_file: str = ARM_FILE,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        policy_spec("dark", uid)
        self.uid = uid
        self.arm_file = Path(arm_file)
        self.runner = runner

    @property
    def armed(self) -> bool:
        return self.arm_file.exists()

    def _run(self, args: list[str], *, input_text: str | None = None) -> Any:
        try:
            result = self.runner(
                ["/usr/bin/nft", *args],
                input=input_text,
                stdin=subprocess.DEVNULL if input_text is None else None,
                capture_output=True,
                text=True,
                timeout=15,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError("nftables command failed") from exc
        if result.returncode != 0:
            raise PolicyError(f"nftables command failed: {result.stderr.strip()}")
        return result

    def _table_document(self) -> Any:
        result = self._run(["--json", "list", "table", "inet", TABLE])
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PolicyError("nftables returned invalid JSON") from exc

    def _verify_legacy_absent(self) -> None:
        result = self._run(["--json", "list", "tables"])
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PolicyError("nftables returned invalid table inventory") from exc
        if not tables_document_has_only_policy(document):
            raise PolicyError("legacy audit table remains active")

    def _verify(self, mode: str) -> None:
        verify_document(self._table_document(), mode, self.uid)
        self._verify_legacy_absent()

    def _apply(self, mode: str) -> None:
        self._run(["--file", "-"], input_text=ruleset(mode, self.uid))
        self._verify(mode)

    def _stop_agents(self) -> None:
        try:
            result = self.runner(
                ["/usr/bin/systemctl", "stop", f"user@{self.uid}.service"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError("could not stop the agent user manager") from exc
        if result.returncode != 0:
            raise PolicyError(f"could not stop the agent user manager: {result.stderr.strip()}")

    def _fail_closed(self, original: Exception) -> None:
        errors = [f"requested policy failed: {original}"]
        try:
            self._apply("locked")
        except (PolicyError, OSError) as exc:
            errors.append(f"locked fallback failed: {exc}")
        try:
            self._stop_agents()
        except (PolicyError, OSError) as exc:
            errors.append(str(exc))
        raise PolicyError("; ".join(errors)) from original

    def activate(self, mode: str) -> None:
        if mode not in {"locked", "attended"}:
            raise PolicyError("unsupported activation mode")
        armed = self.armed
        selected = mode if armed else "dark"
        try:
            self._apply(selected)
        except (PolicyError, OSError) as exc:
            if not armed:
                raise
            self._fail_closed(exc)

    def verify(self, mode: str) -> None:
        if mode not in {"locked", "attended"}:
            raise PolicyError("unsupported verification mode")
        armed = self.armed
        selected = mode if armed else "dark"
        try:
            self._verify(selected)
        except (PolicyError, OSError) as exc:
            if not armed:
                raise
            self._fail_closed(exc)

    def failsafe(self) -> None:
        if not self.armed:
            self._apply("dark")
            return
        errors: list[str] = []
        try:
            self._apply("locked")
        except (PolicyError, OSError) as exc:
            errors.append(f"locked failsafe failed: {exc}")
        try:
            self._stop_agents()
        except (PolicyError, OSError) as exc:
            errors.append(str(exc))
        if errors:
            raise PolicyError("; ".join(errors))

    def dark(self) -> None:
        if self.armed:
            raise PolicyError("remove the root-owned enforcement arm file before going dark")
        self._apply("dark")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--uid", type=int, help="agent UID (defaults to the agent account)")
    parser.add_argument("--arm-file", default=ARM_FILE, help=argparse.SUPPRESS)
    parser.add_argument("command", choices=["locked", "attended", "verify", "failsafe", "dark"])
    parser.add_argument("mode", choices=["locked", "attended"], nargs="?")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("diary-network-policy must run as root")
    if (args.command == "verify") != (args.mode is not None):
        parser.error("verify requires locked or attended; other commands take no mode")
    uid = args.uid if args.uid is not None else pwd.getpwnam("agent").pw_uid
    policy = NetworkPolicy(uid, args.arm_file)
    try:
        if args.command in {"locked", "attended"}:
            policy.activate(args.command)
        elif args.command == "verify":
            policy.verify(args.mode)
        elif args.command == "failsafe":
            policy.failsafe()
        else:
            policy.dark()
        return 0
    except (KeyError, PolicyError, OSError) as exc:
        print(f"diary-network-policy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
