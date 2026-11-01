#!/usr/bin/python3
"""Prepare and verify a contained point-to-point local-model link migration."""
from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

CONFIG_PATH = Path("/etc/diary/mac-link.toml")
LEGACY_PATH = Path("/etc/diary/mac-link-legacy.nft")
ACT01_RECORD = Path("/etc/diary/act01-accepted")
MAC_EVIDENCE = Path("/etc/diary/mac-link-evidence.json")
NEW_TABLE = "diary_pipeline_mac_link"
PROFILE_NAME = "diary-mac-model"
LINK_UNIT = "diary-mac-link.service"
PIPELINE_DROPIN = "50-mac-link.conf"
SCHEMA_VERSION = 1
ACT01_TEXT = "ACT-01 accepted\n"
IFACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,31}")
NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
UNIT_RE = re.compile(r"[A-Za-z0-9_.@-]+\.service")
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


class MacLinkError(RuntimeError):
    """A migration gate failed without relaxing containment."""


@dataclasses.dataclass(frozen=True)
class Config:
    interface: str
    host_address: ipaddress.IPv4Interface
    model_address: ipaddress.IPv4Address
    model_port: int
    legacy_model_address: ipaddress.IPv4Address
    legacy_nft_table: str
    pipeline_user: str
    agent_user: str
    acceptance_unit: str
    mac_interface: str
    mac_service_label: str
    mac_program_arguments: tuple[str, ...]

    @property
    def endpoint(self) -> str:
        return f"http://{self.model_address}:{self.model_port}"


@dataclasses.dataclass(frozen=True)
class Layout:
    config: Path = CONFIG_PATH
    legacy: Path = LEGACY_PATH
    act01_record: Path = ACT01_RECORD
    mac_evidence: Path = MAC_EVIDENCE
    state_dir: Path = Path("/var/lib/diary/mac-link")
    nm_profile: Path = Path("/etc/NetworkManager/system-connections/diary-mac-model.nmconnection")
    link_unit: Path = Path("/etc/systemd/system/diary-mac-link.service")
    pipeline_dropin: Path = Path("/etc/systemd/system/diary-pipeline.service.d/50-mac-link.conf")

    @classmethod
    def under(cls, root: Path) -> "Layout":
        return cls(
            config=root / "etc/diary/mac-link.toml",
            legacy=root / "etc/diary/mac-link-legacy.nft",
            act01_record=root / "etc/diary/act01-accepted",
            mac_evidence=root / "etc/diary/mac-link-evidence.json",
            state_dir=root / "var/lib/diary/mac-link",
            nm_profile=root / "etc/NetworkManager/system-connections/diary-mac-model.nmconnection",
            link_unit=root / "etc/systemd/system/diary-mac-link.service",
            pipeline_dropin=root / "etc/systemd/system/diary-pipeline.service.d/50-mac-link.conf",
        )

    @property
    def state(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def mac_plist(self) -> Path:
        return self.state_dir / "mac_plist"

    @property
    def pending_nft(self) -> Path:
        return self.state_dir / "pending.nft"


def _required(data: dict[str, Any], key: str, kind: type) -> Any:
    if key not in data or type(data[key]) is not kind:
        raise MacLinkError(f"required config key {key!r} must be {kind.__name__}")
    return data[key]


def load_config(path: Path = CONFIG_PATH) -> Config:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MacLinkError(f"cannot read mac-link config: {exc}") from exc
    expected = {
        "schema_version", "interface", "host_address", "model_address", "model_port",
        "legacy_model_address", "legacy_nft_table", "pipeline_user", "agent_user",
        "acceptance_unit", "mac_interface", "mac_service_label", "mac_program_arguments",
    }
    if set(data) != expected:
        raise MacLinkError(f"config keys must be exactly {sorted(expected)}")
    if _required(data, "schema_version", int) != SCHEMA_VERSION:
        raise MacLinkError("unsupported mac-link config schema")
    strings = {
        key: _required(data, key, str)
        for key in expected - {"schema_version", "model_port", "mac_program_arguments"}
    }
    for key in ("interface", "mac_interface"):
        if not IFACE_RE.fullmatch(strings[key]) or strings[key] in {"lo", "any"}:
            raise MacLinkError(f"invalid dedicated interface in {key}")
    for key in ("pipeline_user", "agent_user"):
        if not USER_RE.fullmatch(strings[key]):
            raise MacLinkError(f"invalid user in {key}")
    if strings["pipeline_user"] == strings["agent_user"]:
        raise MacLinkError("pipeline and general agent must have separate identities")
    if not NAME_RE.fullmatch(strings["legacy_nft_table"]) or strings["legacy_nft_table"] == NEW_TABLE:
        raise MacLinkError("legacy nft table name is invalid or collides with the migration table")
    if not UNIT_RE.fullmatch(strings["acceptance_unit"]):
        raise MacLinkError("acceptance_unit must be a systemd service name")
    if not LABEL_RE.fullmatch(strings["mac_service_label"]):
        raise MacLinkError("invalid Mac service label")
    try:
        host = ipaddress.IPv4Interface(strings["host_address"])
        model = ipaddress.IPv4Address(strings["model_address"])
        legacy = ipaddress.IPv4Address(strings["legacy_model_address"])
    except ValueError as exc:
        raise MacLinkError(f"invalid IPv4 address: {exc}") from exc
    if host.network.prefixlen not in (30, 31):
        raise MacLinkError("host_address must use a point-to-point /30 or /31 prefix")
    if model not in host.network or model == host.ip:
        raise MacLinkError("model_address must be the other address in the dedicated subnet")
    if host.network.prefixlen == 30 and model in {host.network.network_address, host.network.broadcast_address}:
        raise MacLinkError("model_address cannot be the /30 network or broadcast address")
    if host.ip in {host.network.network_address, host.network.broadcast_address} and host.network.prefixlen == 30:
        raise MacLinkError("host_address cannot be the /30 network or broadcast address")
    if legacy == model or legacy in host.network:
        raise MacLinkError("legacy model address must be outside the dedicated subnet")
    port = _required(data, "model_port", int)
    if not 1 <= port <= 65_535:
        raise MacLinkError("model_port must be between 1 and 65535")
    raw_arguments = _required(data, "mac_program_arguments", list)
    if not raw_arguments or any(type(item) is not str or not item for item in raw_arguments):
        raise MacLinkError("mac_program_arguments must be a non-empty string array")
    if not raw_arguments[0].startswith("/") or any("\x00" in item or "\n" in item for item in raw_arguments):
        raise MacLinkError("Mac executable must be absolute and arguments must be single-line")
    if any(item in {"--host", "--port"} or item.startswith(("--host=", "--port=")) for item in raw_arguments):
        raise MacLinkError("Mac endpoint flags are generated from the dedicated-address fields")
    return Config(
        interface=strings["interface"], host_address=host, model_address=model,
        model_port=port, legacy_model_address=legacy,
        legacy_nft_table=strings["legacy_nft_table"], pipeline_user=strings["pipeline_user"],
        agent_user=strings["agent_user"], acceptance_unit=strings["acceptance_unit"],
        mac_interface=strings["mac_interface"], mac_service_label=strings["mac_service_label"],
        mac_program_arguments=tuple(raw_arguments),
    )


def _nft_rule(config: Config, uid: int, destination: ipaddress.IPv4Address, interface: str | None) -> str:
    selector = f' oifname "{interface}"' if interface else ""
    return (
        f"    meta skuid {uid}{selector} ip daddr {destination} tcp dport {config.model_port} "
        "ct state { new, established } accept"
    )


def _nft_table(name: str, rules: list[str]) -> str:
    body = "\n".join(rules)
    return f"""table inet {name} {{
    chain output {{
        type filter hook output priority filter + 30; policy accept;
{body}
    }}
}}
"""


def render_legacy_nft(config: Config, uid: int) -> str:
    rules = [_nft_rule(config, uid, config.legacy_model_address, None), f"    meta skuid {uid} drop"]
    return _nft_table(config.legacy_nft_table, rules)


def render_nft(config: Config, uid: int, phase: str) -> str:
    if phase not in {"staged", "final"}:
        raise MacLinkError("nft phase must be staged or final")
    rules = []
    if phase == "staged":
        rules.append(_nft_rule(config, uid, config.legacy_model_address, None))
    rules.extend([
        _nft_rule(config, uid, config.model_address, config.interface),
        f"    meta skuid {uid} drop",
    ])
    return _nft_table(NEW_TABLE, rules)


def nft_transaction(config: Config, uid: int, phase: str, legacy: str) -> str:
    if phase == "staged":
        return f"destroy table inet {NEW_TABLE}\ndestroy table inet {config.legacy_nft_table}\n" + render_nft(config, uid, phase)
    if phase == "final":
        return f"destroy table inet {NEW_TABLE}\n" + render_nft(config, uid, phase)
    if phase == "rollback":
        if _normalize_nft(legacy) != _normalize_nft(render_legacy_nft(config, uid)):
            raise MacLinkError("rollback snapshot is not the exact contained legacy policy")
        return f"destroy table inet {NEW_TABLE}\ndestroy table inet {config.legacy_nft_table}\n{legacy}"
    raise MacLinkError("unknown nft transaction phase")


def render_nmconnection(config: Config) -> str:
    return f"""[connection]
id={PROFILE_NAME}
type=ethernet
interface-name={config.interface}
autoconnect=true

[ipv4]
method=manual
address1={config.host_address.with_prefixlen}
never-default=true
ignore-auto-dns=true
ignore-auto-routes=true
gateway=
dns=
dns-search=

[ipv6]
method=disabled
"""


def render_link_unit() -> str:
    return f"""[Unit]
Description=Dedicated contained local-model link
Requires=NetworkManager.service
After=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nmcli connection up id {PROFILE_NAME}
ExecStop=/usr/bin/nmcli connection down id {PROFILE_NAME}
"""


def render_pipeline_dropin(config: Config) -> str:
    return f"""[Unit]
Requires={LINK_UNIT}
After={LINK_UNIT}

[Service]
Environment=DIARY_MODEL_ENDPOINT={config.endpoint}
RestrictNetworkInterfaces=
RestrictNetworkInterfaces={config.interface}
IPAddressAllow=
IPAddressAllow={config.model_address}/32
IPAddressDeny=any
"""


def render_mac_plist(config: Config) -> str:
    arguments = [*config.mac_program_arguments, "--host", str(config.model_address), "--port", str(config.model_port)]
    plist = {
        "Label": config.mac_service_label,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    return ET.tostring(ET.fromstring(_plist_xml(plist)), encoding="unicode") + "\n"


def _plist_xml(value: dict[str, Any]) -> str:
    def render(item: Any) -> str:
        if isinstance(item, dict):
            body = "".join(f"<key>{key}</key>{render(val)}" for key, val in item.items())
            return f"<dict>{body}</dict>"
        if isinstance(item, list):
            return "<array>" + "".join(render(entry) for entry in item) + "</array>"
        if isinstance(item, bool):
            return "<true/>" if item else "<false/>"
        escaped = str(item).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<string>{escaped}</string>"
    return '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0">' + render(value) + "</plist>"


def render_artifacts(config: Config, uid: int, legacy: str) -> dict[str, str]:
    return {
        "nmconnection": render_nmconnection(config),
        "link_unit": render_link_unit(),
        "pipeline_dropin": render_pipeline_dropin(config),
        "mac_plist": render_mac_plist(config),
        "legacy_nft": legacy,
        "staged_nft": nft_transaction(config, uid, "staged", legacy),
        "final_nft": nft_transaction(config, uid, "final", legacy),
    }


def _normalize_nft(value: str) -> str:
    return " ".join(value.split())


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _safe_file(path: Path, *, isolated: bool) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MacLinkError(f"required file is unavailable: {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MacLinkError(f"required path must be a regular non-symlink file: {path}")
    if not isolated and (info.st_uid != 0 or info.st_mode & 0o022):
        raise MacLinkError(f"required file must be root-owned and not group/other writable: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise MacLinkError(f"cannot read required file {path}: {exc}") from exc


def _routing_snapshot(run: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    commands = {
        "routes4": ["/usr/bin/ip", "-json", "route", "show", "table", "all"],
        "routes6": ["/usr/bin/ip", "-6", "-json", "route", "show", "table", "all"],
        "rules4": ["/usr/bin/ip", "-json", "rule", "show"],
        "rules6": ["/usr/bin/ip", "-6", "-json", "rule", "show"],
    }
    result = {}
    for key, command in commands.items():
        try:
            completed = run(command, capture_output=True, text=True, check=True)
            result[key] = json.loads(completed.stdout)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise MacLinkError(f"cannot snapshot host routing for {key}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise MacLinkError(f"ip returned invalid JSON for {key}") from exc
    return result


def _without_dedicated_routes(snapshot: dict[str, Any], interface: str) -> dict[str, Any]:
    return {
        key: [entry for entry in value if not isinstance(entry, dict) or entry.get("dev") != interface]
        if key.startswith("routes") else value
        for key, value in snapshot.items()
    }


def require_no_dedicated_routes(snapshot: dict[str, Any], interface: str) -> None:
    if any(
        isinstance(route, dict) and route.get("dev") == interface
        for family in ("routes4", "routes6") for route in snapshot[family]
    ):
        raise MacLinkError("dedicated interface still has routes")


def _validate_dedicated_routes(snapshot: dict[str, Any], config: Config) -> None:
    if any(
        isinstance(route, dict) and route.get("dev") == config.interface
        for route in snapshot["routes6"]
    ):
        raise MacLinkError("dedicated interface has an IPv6 route")
    network = config.host_address.network
    for route in snapshot["routes4"]:
        if not isinstance(route, dict) or route.get("dev") != config.interface:
            continue
        destination = route.get("dst", "default")
        kind = route.get("type", "unicast")
        source = route.get("src", str(config.host_address.ip))
        connected = (
            kind == "unicast" and destination == str(network)
            and route.get("scope") == "link"
        )
        local = (
            kind == "local" and destination == str(config.host_address.ip)
            and route.get("scope") == "host"
        )
        broadcast = (
            kind == "broadcast" and network.prefixlen == 30
            and destination in {str(network.network_address), str(network.broadcast_address)}
            and route.get("scope") == "link"
        )
        if (
            route.get("via") or route.get("gateway") or source != str(config.host_address.ip)
            or not (connected or local or broadcast)
        ):
            raise MacLinkError(f"unexpected route on dedicated interface: {route}")


def verify_routing(before: dict[str, Any], after: dict[str, Any], config: Config) -> None:
    if _canonical(_without_dedicated_routes(before, config.interface)) != _canonical(
        _without_dedicated_routes(after, config.interface)
    ):
        raise MacLinkError("an unrelated host route or policy rule changed")
    _validate_dedicated_routes(after, config)


def validate_mac_evidence(raw: str, config: Config) -> None:
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MacLinkError("Mac evidence is not valid JSON") from exc
    expected = {
        "schema_version": SCHEMA_VERSION,
        "interface": config.mac_interface,
        "service_label": config.mac_service_label,
        "listen_address": str(config.model_address),
        "listen_port": config.model_port,
        "dedicated_listener_only": True,
        "old_listener_absent": True,
        "default_route_unchanged": True,
        "other_routes_unchanged": True,
        "dns_unchanged": True,
    }
    if evidence != expected:
        raise MacLinkError(f"Mac evidence must exactly attest {expected}")


Runner = Callable[..., subprocess.CompletedProcess[str]]


class Migration:
    def __init__(
        self,
        config: Config,
        pipeline_uid: int,
        *,
        layout: Layout = Layout(),
        runner: Runner = subprocess.run,
        isolated: bool = False,
    ) -> None:
        if not 0 <= pipeline_uid <= 65_535:
            raise MacLinkError("pipeline UID is outside the nftables UID range")
        self.config = config
        self.uid = pipeline_uid
        self.layout = layout
        self.run = runner
        self.isolated = isolated

    def _call(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return self.run(list(args), capture_output=True, text=True, check=check)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise MacLinkError(f"system command failed safely: {args[0]}: {exc}") from exc

    def _require_root(self) -> None:
        if not self.isolated and os.geteuid() != 0:
            raise MacLinkError("live migration commands must run as root through the approved installer")

    def _legacy(self) -> str:
        legacy = _safe_file(self.layout.legacy, isolated=self.isolated)
        if _normalize_nft(legacy) != _normalize_nft(render_legacy_nft(self.config, self.uid)):
            raise MacLinkError("legacy snapshot does not match the required fixed-IP deny-all policy")
        return legacy

    def _act01(self) -> None:
        if _safe_file(self.layout.act01_record, isolated=self.isolated) != ACT01_TEXT:
            raise MacLinkError("ACT-01 acceptance record is missing or has unexpected content")
        if self._call("/usr/bin/systemctl", "is-active", "--quiet", "diary-locked.service", check=False).returncode != 0:
            raise MacLinkError("migration requires the ACT-01 locked state")
        if self._call("/usr/bin/systemctl", "is-active", "--quiet", "diary-attended.service", check=False).returncode == 0:
            raise MacLinkError("migration is forbidden in attended mode")

    def _unit_file(self, path: str) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise MacLinkError("acceptance unit path is not absolute")
        _safe_file(candidate, isolated=self.isolated)

    def _acceptance_unit(self) -> None:
        fragment = self._call(
            "/usr/bin/systemctl", "show", self.config.acceptance_unit,
            "--property=FragmentPath", "--value",
        ).stdout.strip()
        self._unit_file(fragment)
        dropins = self._call(
            "/usr/bin/systemctl", "show", self.config.acceptance_unit,
            "--property=DropInPaths", "--value",
        ).stdout.split()
        for path in dropins:
            self._unit_file(path)

    def _interface_and_routes(self) -> dict[str, Any]:
        link = self._call("/usr/bin/ip", "-json", "link", "show", "dev", self.config.interface).stdout
        try:
            link_data = json.loads(link)
        except json.JSONDecodeError as exc:
            raise MacLinkError("ip link returned invalid JSON") from exc
        if len(link_data) != 1 or link_data[0].get("ifname") != self.config.interface:
            raise MacLinkError("owner-supplied dedicated interface does not exist unambiguously")
        if link_data[0].get("master") or link_data[0].get("link_type") == "loopback":
            raise MacLinkError("dedicated interface must not be loopback or enslaved to a bridge")
        addresses = json.loads(self._call(
            "/usr/bin/ip", "-json", "addr", "show", "dev", self.config.interface,
        ).stdout)
        if len(addresses) != 1 or any(
            item.get("scope") == "global" for item in addresses[0].get("addr_info", [])
        ):
            raise MacLinkError("dedicated interface already has a global address; refusing to reconfigure it")
        before = _routing_snapshot(self.run)
        require_no_dedicated_routes(before, self.config.interface)
        for route in before["routes4"]:
            if not isinstance(route, dict) or route.get("dst", "default") == "default":
                continue
            try:
                network = ipaddress.IPv4Network(route["dst"], strict=False)
            except (KeyError, ValueError) as exc:
                raise MacLinkError(f"unparseable existing IPv4 route: {route}") from exc
            if network.overlaps(self.config.host_address.network):
                raise MacLinkError("dedicated subnet collides with an existing route on another interface")
        return before

    def _pipeline_active(self) -> None:
        state = self._call(
            "/usr/bin/systemctl", "show", "diary-pipeline.service",
            "--property=ActiveState", "--value",
        ).stdout.strip()
        if state != "active":
            raise MacLinkError(f"integrated pipeline must initially be active, not {state or 'unknown'}")

    def _probe_prerequisites(self) -> None:
        for user in (self.config.pipeline_user, self.config.agent_user):
            raw_uid = self._call("/usr/bin/id", "-u", user).stdout.strip()
            try:
                uid = int(raw_uid)
            except ValueError as exc:
                raise MacLinkError(f"probe identity returned an invalid UID for {user}") from exc
            if user == self.config.pipeline_user and uid != self.uid:
                raise MacLinkError("pipeline UID changed after configuration")
            version = self._call(
                "/usr/bin/runuser", "-u", user, "--", "/usr/bin/curl", "-q", "--version",
            ).stdout
            if not version.startswith("curl "):
                raise MacLinkError(f"curl is not operational for probe identity {user}")

    def preflight(self) -> dict[str, Any]:
        self._require_root()
        _safe_file(self.layout.config, isolated=self.isolated)
        self._act01()
        self._pipeline_active()
        self._probe_prerequisites()
        legacy = self._legacy()
        validate_mac_evidence(_safe_file(self.layout.mac_evidence, isolated=self.isolated), self.config)
        self._acceptance_unit()
        live = self._call("/usr/bin/nft", "list", "table", "inet", self.config.legacy_nft_table).stdout
        if _normalize_nft(live) != _normalize_nft(legacy):
            raise MacLinkError("live legacy nftables policy differs from the approved contained snapshot")
        return self._interface_and_routes()

    def _acceptance(self) -> None:
        self._acceptance_unit()
        self._call("/usr/bin/systemctl", "restart", "--wait", self.config.acceptance_unit)
        result = self._call(
            "/usr/bin/systemctl", "show", self.config.acceptance_unit, "--property=Result", "--value",
        ).stdout.strip()
        if result != "success":
            raise MacLinkError(f"contained pipeline acceptance unit failed: Result={result}")

    def _write(self, path: Path, content: str, mode: int) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise MacLinkError(f"refusing to replace symlink: {path}")
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            if not self.isolated:
                os.fchown(fd, 0, 0)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _state(self) -> dict[str, Any]:
        return json.loads(_safe_file(self.layout.state, isolated=self.isolated))

    def _save_state(self, state: dict[str, Any]) -> None:
        self._write(self.layout.state, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)

    def _apply_nft(self, phase: str) -> None:
        legacy = self._legacy()
        transaction = nft_transaction(self.config, self.uid, phase, legacy)
        self._write(self.layout.pending_nft, transaction, 0o600)
        self._call("/usr/bin/nft", "--check", "--file", str(self.layout.pending_nft))
        self._call("/usr/bin/nft", "--file", str(self.layout.pending_nft))
        table = self.config.legacy_nft_table if phase == "rollback" else NEW_TABLE
        expected = legacy if phase == "rollback" else render_nft(self.config, self.uid, phase)
        live = self._call("/usr/bin/nft", "list", "table", "inet", table).stdout
        if _normalize_nft(live) != _normalize_nft(expected):
            raise MacLinkError(f"live nftables verification failed after {phase}")

    def _artifacts(self) -> dict[Path, tuple[str, int]]:
        return {
            self.layout.nm_profile: (render_nmconnection(self.config), 0o600),
            self.layout.link_unit: (render_link_unit(), 0o644),
            self.layout.pipeline_dropin: (render_pipeline_dropin(self.config), 0o644),
            self.layout.mac_plist: (render_mac_plist(self.config), 0o644),
        }

    def install(self) -> None:
        self._require_root()
        if self.layout.state.exists():
            raise MacLinkError("a migration is already staged; finalize or roll it back")
        before = self.preflight()
        artifacts = self._artifacts()
        backups = {}
        for path in artifacts:
            backups[str(path)] = None if not path.exists() else {
                "content": _safe_file(path, isolated=self.isolated),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        self._save_state({"schema_version": 1, "phase": "installing", "before": before,
                          "pipeline_was_active": True, "backups": backups})
        try:
            for path, (content, mode) in artifacts.items():
                self._write(path, content, mode)
            self._call("/usr/bin/systemd-analyze", "verify", str(self.layout.link_unit))
            self._call("/usr/bin/systemctl", "daemon-reload")
            self._call("/usr/bin/systemd-analyze", "verify", "diary-pipeline.service")
            self._call("/usr/bin/nmcli", "connection", "reload")
            self._call("/usr/bin/systemctl", "start", LINK_UNIT)
            verify_routing(before, _routing_snapshot(self.run), self.config)
            self._apply_nft("staged")
            self._call("/usr/bin/systemctl", "restart", "diary-pipeline.service")
            self._acceptance()
            verify_routing(before, _routing_snapshot(self.run), self.config)
            state = self._state()
            state["phase"] = "staged"
            self._save_state(state)
        except BaseException:
            self.rollback()
            raise

    def _expect_denied(self, user: str, address: ipaddress.IPv4Address) -> None:
        url = f"http://{address}:{self.config.model_port}/"
        result = self._call(
            "/usr/bin/runuser", "-u", user, "--", "/usr/bin/curl", "-q", "--noproxy", "*",
            "--connect-timeout", "2", "--max-time", "3", "--silent", "--show-error",
            "--output", "/dev/null", url, check=False,
        )
        if result.returncode == 0:
            raise MacLinkError(f"containment probe unexpectedly reached {url} as {user}")
        if result.returncode not in {7, 28}:
            raise MacLinkError(
                f"containment probe for {user} had an operational error: curl exit {result.returncode}"
            )

    def _verify_final_probes(self) -> None:
        self._probe_prerequisites()
        self._expect_denied(self.config.pipeline_user, self.config.legacy_model_address)
        self._expect_denied(self.config.agent_user, self.config.legacy_model_address)
        self._expect_denied(self.config.agent_user, self.config.model_address)

    def finalize(self) -> None:
        self._require_root()
        state = self._state()
        if state.get("phase") != "staged":
            raise MacLinkError("only a verified staged migration can be finalized")
        self._act01()
        self._pipeline_active()
        validate_mac_evidence(_safe_file(self.layout.mac_evidence, isolated=self.isolated), self.config)
        for path, (content, _mode) in self._artifacts().items():
            if _safe_file(path, isolated=self.isolated) != content:
                raise MacLinkError(f"installed migration artifact changed: {path}")
        try:
            self._acceptance()
            self._apply_nft("final")
            self._acceptance()
            self._verify_final_probes()
            verify_routing(state["before"], _routing_snapshot(self.run), self.config)
            state["phase"] = "final"
            self._save_state(state)
        except BaseException:
            self.rollback()
            raise

    def verify(self) -> None:
        self._require_root()
        state = self._state()
        if state.get("phase") not in {"staged", "final"}:
            raise MacLinkError("no staged or final migration is installed")
        self._act01()
        self._pipeline_active()
        validate_mac_evidence(_safe_file(self.layout.mac_evidence, isolated=self.isolated), self.config)
        table = self._call("/usr/bin/nft", "list", "table", "inet", NEW_TABLE).stdout
        if _normalize_nft(table) != _normalize_nft(render_nft(self.config, self.uid, state["phase"])):
            raise MacLinkError("live migration table does not match its recorded phase")
        self._acceptance()
        if state["phase"] == "final":
            self._verify_final_probes()
        verify_routing(state["before"], _routing_snapshot(self.run), self.config)

    def _stop_link(self) -> None:
        load_state = self._call(
            "/usr/bin/systemctl", "show", LINK_UNIT, "--property=LoadState", "--value",
        ).stdout.strip()
        if load_state == "not-found":
            return
        if load_state != "loaded":
            raise MacLinkError(f"cannot confirm dedicated link unit state: {load_state or 'unknown'}")
        self._call("/usr/bin/systemctl", "stop", LINK_UNIT)

    def _verify_link_down(self, before: dict[str, Any]) -> None:
        active_state = self._call(
            "/usr/bin/systemctl", "show", LINK_UNIT, "--property=ActiveState", "--value",
        ).stdout.strip()
        if active_state != "inactive":
            raise MacLinkError(f"dedicated link unit is still {active_state or 'unknown'}")
        active_profiles = self._call(
            "/usr/bin/nmcli", "-t", "-f", "NAME", "connection", "show", "--active",
        ).stdout.splitlines()
        if PROFILE_NAME in active_profiles:
            raise MacLinkError("dedicated NetworkManager profile is still active")
        link = json.loads(self._call(
            "/usr/bin/ip", "-json", "link", "show", "dev", self.config.interface,
        ).stdout)
        if len(link) != 1 or link[0].get("operstate") != "DOWN":
            raise MacLinkError("dedicated interface is not down")
        addresses = json.loads(self._call(
            "/usr/bin/ip", "-json", "addr", "show", "dev", self.config.interface,
        ).stdout)
        if len(addresses) != 1 or any(
            item.get("scope") == "global" for item in addresses[0].get("addr_info", [])
        ):
            raise MacLinkError("dedicated interface still has a global address")
        after = _routing_snapshot(self.run)
        require_no_dedicated_routes(after, self.config.interface)
        verify_routing(before, after, self.config)

    def rollback(self) -> None:
        self._require_root()
        state = self._state()
        self._call("/usr/bin/systemctl", "stop", "diary-pipeline.service")
        self._apply_nft("rollback")
        self._stop_link()
        self._verify_link_down(state["before"])
        for path_text, backup in state["backups"].items():
            path = Path(path_text)
            if backup is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                self._write(path, backup["content"], backup["mode"])
        self._call("/usr/bin/systemctl", "daemon-reload")
        self._call("/usr/bin/nmcli", "connection", "reload")
        if state.get("pipeline_was_active"):
            self._call("/usr/bin/systemctl", "start", "diary-pipeline.service")
        live = self._call("/usr/bin/nft", "list", "table", "inet", self.config.legacy_nft_table).stdout
        if _normalize_nft(live) != _normalize_nft(render_legacy_nft(self.config, self.uid)):
            raise MacLinkError("rollback did not restore the exact contained legacy policy")
        self.layout.state.unlink()


def _real_uid(user: str) -> int:
    import pwd
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError as exc:
        raise MacLinkError(f"required service identity does not exist: {user}") from exc


def _live(args: argparse.Namespace) -> None:
    config = load_config()
    migration = Migration(config, _real_uid(config.pipeline_user))
    if args.command == "preflight":
        migration.preflight()
    elif args.command == "install":
        migration.install()
    elif args.command == "finalize":
        migration.finalize()
    elif args.command == "verify":
        migration.verify()
    elif args.command == "rollback":
        migration.rollback()
    else:
        raise MacLinkError("unsupported live command")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="validate an owner-supplied config without system access")
    check.add_argument("--config", type=Path, default=CONFIG_PATH)
    render = subparsers.add_parser("render", help="render reviewed artifacts without system access")
    render.add_argument("--config", type=Path, default=CONFIG_PATH)
    render.add_argument("--pipeline-uid", type=int, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    for command in ("preflight", "install", "finalize", "verify", "rollback"):
        subparsers.add_parser(command)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            load_config(args.config)
        elif args.command == "render":
            config = load_config(args.config)
            artifacts = render_artifacts(config, args.pipeline_uid, render_legacy_nft(config, args.pipeline_uid))
            args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, content in artifacts.items():
                (args.output_dir / f"{name}").write_text(content, encoding="utf-8")
        else:
            _live(args)
    except (MacLinkError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
