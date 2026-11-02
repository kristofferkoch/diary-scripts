"""Isolated validation for the contained dedicated-model-link migration."""
from __future__ import annotations

import ipaddress
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import diary_mac_link as mac


UID = 1234


def config_text() -> str:
    return """schema_version = 1
interface = "enp9s0"
""" + 'host_address = "192.0.2.1' + '/30"\n' + """model_address = "192.0.2.2"
model_port = 8080
legacy_model_address = "198.51.100.2"
legacy_nft_table = "diary_pipeline"
pipeline_user = "diary-pipeline"
agent_user = "agent"
acceptance_unit = "diary-pipeline-acceptance.service"
mac_interface = "en7"
mac_service_label = "local.diary-model"
mac_program_arguments = ["/opt/model/server", "--model", "/Models/triage.gguf"]
"""


def make_config(tmp_path: Path, text: str | None = None) -> mac.Config:
    path = tmp_path / "mac-link.toml"
    path.write_text(text or config_text(), encoding="utf-8")
    return mac.load_config(path)


def evidence(config: mac.Config) -> str:
    return json.dumps({
        "schema_version": 1,
        "interface": config.mac_interface,
        "service_label": config.mac_service_label,
        "listen_address": str(config.model_address),
        "listen_port": config.model_port,
        "dedicated_listener_only": True,
        "old_listener_absent": True,
        "default_route_unchanged": True,
        "other_routes_unchanged": True,
        "dns_unchanged": True,
    })


def test_owner_config_has_no_implicit_live_values(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert config.endpoint == "http" + "://192.0.2.2:8080"
    assert config.host_address.network == ipaddress.IPv4Network("192.0.2.0/30")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('host_address = "192.0.2.1' + '/30"', 'host_address = "192.0.2.1' + '/24"'),
        ('model_address = "192.0.2.2"', 'model_address = "192.0.2.9"'),
        ('model_address = "192.0.2.2"', 'model_address = "192.0.2.0"'),
        ('legacy_model_address = "198.51.100.2"', 'legacy_model_address = "192.0.2.2"'),
        ('interface = "enp9s0"', 'interface = "lo"'),
        ('pipeline_user = "diary-pipeline"', 'pipeline_user = "agent"'),
        ('model_port = 8080', 'model_port = 0'),
        ('"--model", "/Models/triage.gguf"', '"--model", "/Models/triage.gguf", "--host", "0.0.0.0"'),
        ('mac_service_label = "local.diary-model"', 'mac_service_label = "bad label"'),
    ],
)
def test_invalid_or_non_dedicated_config_fails_closed(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(mac.MacLinkError):
        make_config(tmp_path, config_text().replace(old, new))


def test_unknown_and_missing_config_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(mac.MacLinkError, match="exactly"):
        make_config(tmp_path, config_text() + 'fallback_endpoint = "http://example.invalid"\n')
    with pytest.raises(mac.MacLinkError, match="exactly"):
        make_config(tmp_path, config_text().replace('mac_interface = "en7"\n', ""))


def test_network_profile_cannot_change_default_dns_or_unrelated_routes(tmp_path: Path) -> None:
    profile = mac.render_nmconnection(make_config(tmp_path))
    assert "interface-name=enp9s0" in profile
    assert "address1=192.0.2.1/30" in profile
    assert "never-default=true" in profile
    assert "ignore-auto-dns=true" in profile
    assert "ignore-auto-routes=true" in profile
    assert "gateway=\ndns=\ndns-search=" in profile
    assert "method=disabled" in profile
    assert "198.51.100.2" not in profile


def test_pipeline_dropin_binds_endpoint_and_interface_without_loopback_or_legacy(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    dropin = mac.render_pipeline_dropin(config)
    assert "Requires=diary-mac-link.service" in dropin
    assert "Environment=DIARY_MODEL_ENDPOINT=" + config.endpoint in dropin
    assert "RestrictNetworkInterfaces=\nRestrictNetworkInterfaces=enp9s0" in dropin
    assert "IPAddressAllow=\nIPAddressAllow=192.0.2.2/32" in dropin
    assert "IPAddressDeny=any" in dropin
    assert "198.51.100.2" not in dropin
    assert "RestrictNetworkInterfaces=lo" not in dropin


def test_staged_policy_preserves_containment_and_final_removes_only_old_allowance(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    staged = mac.render_nft(config, UID, "staged")
    final = mac.render_nft(config, UID, "final")
    assert staged.count(f"meta skuid {UID}") == 3
    assert 'oifname "enp9s0" ip daddr 192.0.2.2 tcp dport 8080' in staged
    assert "ip daddr 198.51.100.2 tcp dport 8080" in staged
    assert f"meta skuid {UID} drop" in staged
    assert "198.51.100.2" not in final
    assert final.count(f"meta skuid {UID}") == 2
    assert "agent" not in staged
    assert "policy accept" in staged


def test_atomic_transactions_never_leave_an_uncontained_intermediate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    legacy = mac.render_legacy_nft(config, UID)
    staged = mac.nft_transaction(config, UID, "staged", legacy)
    final = mac.nft_transaction(config, UID, "final", legacy)
    rollback = mac.nft_transaction(config, UID, "rollback", legacy)
    assert staged.startswith("destroy table inet diary_pipeline_mac_link\ndestroy table inet diary_pipeline\n")
    assert "198.51.100.2" in staged and "192.0.2.2" in staged
    assert "destroy table inet diary_pipeline\n" not in final
    assert "198.51.100.2" not in final
    assert rollback.startswith("destroy table inet diary_pipeline_mac_link\ndestroy table inet diary_pipeline\n")
    assert rollback.endswith(legacy)
    with pytest.raises(mac.MacLinkError, match="exact contained legacy"):
        mac.nft_transaction(config, UID, "rollback", "table inet diary_pipeline { policy accept }\n")


def test_mac_plist_binds_only_owner_supplied_dedicated_address(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    root = ET.fromstring(mac.render_mac_plist(config))
    values = list(root.iter("dict"))[0]
    children = list(values)
    plist = {children[index].text: children[index + 1] for index in range(0, len(children), 2)}
    arguments = [item.text for item in plist["ProgramArguments"]]
    assert arguments[-4:] == ["--host", "192.0.2.2", "--port", "8080"]
    assert arguments[:3] == ["/opt/model/server", "--model", "/Models/triage.gguf"]
    assert "198.51.100.2" not in arguments
    assert plist["Label"].text == "local.diary-model"


def test_mac_evidence_is_exact_not_a_partial_attestation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    mac.validate_mac_evidence(evidence(config), config)
    value = json.loads(evidence(config))
    value["old_listener_absent"] = False
    with pytest.raises(mac.MacLinkError, match="exactly attest"):
        mac.validate_mac_evidence(json.dumps(value), config)
    value = json.loads(evidence(config))
    del value["default_route_unchanged"]
    with pytest.raises(mac.MacLinkError):
        mac.validate_mac_evidence(json.dumps(value), config)
    value = json.loads(evidence(config))
    value["dns_unchanged"] = False
    with pytest.raises(mac.MacLinkError):
        mac.validate_mac_evidence(json.dumps(value), config)


def route_snapshot() -> dict[str, list]:
    return {
        "routes4": [{"dst": "default", "dev": "eth0"}, {"dst": "203.0.113.0/24", "dev": "eth1"}],
        "routes6": [], "rules4": [{"priority": 0}], "rules6": [],
    }


def test_route_verification_permits_only_expected_connected_and_local_routes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    before = route_snapshot()
    after = json.loads(json.dumps(before))
    after["routes4"].extend([
        {"dst": "192.0.2.0/30", "dev": "enp9s0", "scope": "link", "src": "192.0.2.1"},
        {"type": "local", "dst": "192.0.2.1", "dev": "enp9s0", "scope": "host", "src": "192.0.2.1"},
        {"type": "broadcast", "dst": "192.0.2.3", "dev": "enp9s0", "scope": "link", "src": "192.0.2.1"},
    ])
    mac.verify_routing(before, after, config)
    after["routes4"][1]["dev"] = "eth9"
    with pytest.raises(mac.MacLinkError, match="unrelated"):
        mac.verify_routing(before, after, config)


@pytest.mark.parametrize(
    ("family", "route"),
    [
        ("routes4", {"dst": "default", "dev": "enp9s0", "scope": "link"}),
        ("routes4", {"dst": "192.0.2.0/30", "dev": "enp9s0", "scope": "link", "via": "192.0.2.2"}),
        ("routes4", {"dst": "203.0.113.0/24", "dev": "enp9s0", "scope": "link"}),
        ("routes4", {"type": "local", "dst": "192.0.2.2", "dev": "enp9s0", "scope": "host"}),
        ("routes4", {"dst": "192.0.2.0/30", "dev": "enp9s0", "scope": "link", "src": "192.0.2.2"}),
        ("routes6", {"dst": "2001:db8::/64", "dev": "enp9s0", "scope": "link"}),
    ],
)
def test_route_verification_rejects_dedicated_default_gateway_ipv6_and_unrelated_routes(
    tmp_path: Path, family: str, route: dict,
) -> None:
    config = make_config(tmp_path)
    before = route_snapshot()
    after = json.loads(json.dumps(before))
    after[family].append(route)
    with pytest.raises(mac.MacLinkError, match="dedicated interface|unexpected route"):
        mac.verify_routing(before, after, config)


class FakeSystem:
    def __init__(self, config: mac.Config, layout: mac.Layout) -> None:
        self.config = config
        self.layout = layout
        self.phase = "legacy"
        self.link_up = False
        self.commands: list[list[str]] = []
        self.pipeline_active = True
        self.pipeline_show_error = False
        self.fail_acceptance = False
        self.acceptance_restarts = 0
        self.allow_denied_probe = False
        self.probe_returncode = 28
        self.missing_agent = False
        self.broken_curl = False
        self.change_unrelated_route = False
        self.preexisting_dedicated_route = False
        self.fail_pipeline_stop = False
        self.fail_link_stop = False
        self.link_remains_up = False
        self.profile_remains_active = False
        self.live_legacy = mac.render_legacy_nft(config, UID)

    def result(self, args: list[str], stdout: str = "", returncode: int = 0, check: bool = True):
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, args, stdout, "")
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    def routes(self) -> dict[str, list]:
        routes4 = [{"dst": "default", "dev": "eth0", "protocol": "static"}]
        if self.link_up or self.preexisting_dedicated_route:
            routes4.append({
                "dst": "192.0.2.0/30", "dev": "enp9s0", "protocol": "kernel",
                "scope": "link", "src": "192.0.2.1",
            })
        if self.change_unrelated_route and self.link_up:
            routes4.append({"dst": "203.0.113.0/24", "dev": "eth9", "protocol": "static"})
        return {"routes4": routes4, "routes6": [], "rules4": [{"priority": 0}], "rules6": []}

    def __call__(self, args: list[str], **kwargs):
        self.commands.append(args)
        check = kwargs.get("check", True)
        if args[:3] == ["/usr/bin/systemctl", "is-active", "--quiet"]:
            active = args[3] == "diary-locked.service"
            return self.result(args, returncode=0 if active else 1, check=False)
        if args[:2] == ["/usr/bin/systemctl", "show"]:
            unit, prop = args[2], args[3]
            if prop == "--property=FragmentPath":
                path = self.layout.config.parent.parent / "diary-pipeline-acceptance.service"
                return self.result(args, f"{path}\n")
            if prop == "--property=DropInPaths":
                return self.result(args, "\n")
            if prop == "--property=Result":
                return self.result(args, "success\n")
            if prop == "--property=ActiveState" and unit == "diary-pipeline.service":
                if self.pipeline_show_error:
                    return self.result(args, returncode=1, check=check)
                return self.result(args, "active\n" if self.pipeline_active else "inactive\n")
            if prop == "--property=ActiveState" and unit == mac.LINK_UNIT:
                return self.result(args, "active\n" if self.link_up else "inactive\n")
            if prop == "--property=LoadState" and unit == mac.LINK_UNIT:
                return self.result(args, "loaded\n" if self.layout.link_unit.exists() else "not-found\n")
        if args[:3] == ["/usr/bin/systemctl", "start", "--wait"]:
            return self.result(args)
        if args[:3] == ["/usr/bin/systemctl", "restart", "--wait"]:
            self.acceptance_restarts += 1
            return self.result(args, returncode=1 if self.fail_acceptance else 0, check=check)
        if args[:3] == ["/usr/bin/systemctl", "start", mac.LINK_UNIT]:
            self.link_up = True
            return self.result(args)
        if args[:3] == ["/usr/bin/systemctl", "stop", "diary-pipeline.service"]:
            return self.result(args, returncode=1 if self.fail_pipeline_stop else 0, check=check)
        if args[:3] == ["/usr/bin/systemctl", "stop", mac.LINK_UNIT]:
            if self.fail_link_stop:
                return self.result(args, returncode=1, check=check)
            if not self.link_remains_up:
                self.link_up = False
            return self.result(args, check=check)
        if args[:4] == ["/usr/bin/nft", "list", "table", "inet"]:
            table = args[4]
            if table == self.config.legacy_nft_table:
                if self.phase != "legacy":
                    return self.result(args, returncode=1, check=check)
                return self.result(args, self.live_legacy)
            if table == mac.NEW_TABLE and self.phase in {"staged", "final"}:
                return self.result(args, mac.render_nft(self.config, UID, self.phase))
            return self.result(args, returncode=1, check=check)
        if args[:2] == ["/usr/bin/nft", "--check"]:
            return self.result(args)
        if args[:2] == ["/usr/bin/nft", "--file"]:
            transaction = Path(args[2]).read_text(encoding="utf-8")
            if f"\ntable inet {mac.NEW_TABLE}" in transaction:
                self.phase = "staged" if str(self.config.legacy_model_address) in transaction else "final"
            else:
                self.phase = "legacy"
            return self.result(args)
        if args[:4] == ["/usr/bin/ip", "-json", "link", "show"]:
            return self.result(args, json.dumps([{
                "ifname": "enp9s0", "operstate": "UP" if self.link_up else "DOWN",
            }]))
        if args[:4] == ["/usr/bin/ip", "-json", "addr", "show"]:
            addresses = [{"family": "inet", "scope": "global"}] if self.link_up else []
            return self.result(args, json.dumps([{"ifname": "enp9s0", "addr_info": addresses}]))
        if args[0] == "/usr/bin/ip" and "route" in args:
            key = "routes6" if "-6" in args else "routes4"
            return self.result(args, json.dumps(self.routes()[key]))
        if args[0] == "/usr/bin/ip" and "rule" in args:
            key = "rules6" if "-6" in args else "rules4"
            return self.result(args, json.dumps(self.routes()[key]))
        if args[0] == "/usr/bin/id":
            if args[2] == "agent" and self.missing_agent:
                return self.result(args, returncode=1, check=check)
            return self.result(args, f"{UID if args[2] == self.config.pipeline_user else UID + 1}\n")
        if args[0] == "/usr/bin/runuser":
            if "--version" in args:
                return self.result(args, "" if self.broken_curl else "curl 8.0.0\n",
                                   returncode=127 if self.broken_curl else 0, check=check)
            code = 0 if self.allow_denied_probe else self.probe_returncode
            return self.result(args, returncode=code, check=False)
        if args[:3] == ["/usr/bin/nmcli", "-t", "-f"]:
            active = self.link_up or self.profile_remains_active
            return self.result(args, f"{mac.PROFILE_NAME}\n" if active else "")
        if args[0] in {"/usr/bin/systemctl", "/usr/bin/nmcli", "/usr/bin/systemd-analyze"}:
            return self.result(args, check=check)
        raise AssertionError(f"unexpected system command: {args}")


def isolated_migration(tmp_path: Path):
    root = tmp_path / "root"
    layout = mac.Layout.under(root)
    layout.config.parent.mkdir(parents=True)
    layout.config.write_text(config_text(), encoding="utf-8")
    config = mac.load_config(layout.config)
    layout.legacy.write_text(mac.render_legacy_nft(config, UID), encoding="utf-8")
    layout.act01_record.write_text(mac.ACT01_TEXT, encoding="utf-8")
    layout.mac_evidence.write_text(evidence(config), encoding="utf-8")
    acceptance = root / "etc/diary-pipeline-acceptance.service"
    acceptance.write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    system = FakeSystem(config, layout)
    migration = mac.Migration(config, UID, layout=layout, runner=system, isolated=True)
    return migration, system, layout


def test_isolated_full_lifecycle_preserves_old_until_final_and_restores_it(tmp_path: Path) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    layout.pipeline_dropin.parent.mkdir(parents=True)
    layout.pipeline_dropin.write_text("[Service]\nEnvironment=OLD=1\n", encoding="utf-8")
    migration.install()
    assert system.phase == "staged"
    assert migration._state()["phase"] == "staged"
    assert "198.51.100.2" in mac.render_nft(migration.config, UID, "staged")
    assert layout.mac_plist.is_file()
    migration.verify()
    migration.finalize()
    assert system.phase == "final"
    assert migration._state()["phase"] == "final"
    migration.verify()
    assert system.acceptance_restarts == 5
    migration.rollback()
    assert system.phase == "legacy"
    assert not system.link_up
    assert layout.pipeline_dropin.read_text(encoding="utf-8") == "[Service]\nEnvironment=OLD=1\n"
    assert not layout.nm_profile.exists()
    assert not layout.state.exists()


def test_failed_staged_acceptance_automatically_restores_legacy_containment(tmp_path: Path) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    system.fail_acceptance = True
    with pytest.raises(mac.MacLinkError, match="system command failed safely"):
        migration.install()
    assert system.phase == "legacy"
    assert not system.link_up
    assert not layout.pipeline_dropin.exists()
    assert not layout.state.exists()


def test_successful_old_path_probe_blocks_final_and_rolls_back(tmp_path: Path) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    migration.install()
    system.allow_denied_probe = True
    with pytest.raises(mac.MacLinkError, match="unexpectedly reached"):
        migration.finalize()
    assert system.phase == "legacy"
    assert not system.link_up


def test_unrelated_route_change_fails_closed_and_rolls_back(tmp_path: Path) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    system.change_unrelated_route = True
    with pytest.raises(mac.MacLinkError, match="unrelated"):
        migration.install()
    assert system.phase == "legacy"
    assert not layout.state.exists()


def test_preflight_rejects_unreviewed_live_policy_without_mutating(tmp_path: Path) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    system.live_legacy = "table inet diary_pipeline { chain output { type filter hook output priority filter; policy accept; } }"
    before = len(system.commands)
    with pytest.raises(mac.MacLinkError, match="differs"):
        migration.preflight()
    assert system.phase == "legacy"
    assert not any(command[:2] == ["/usr/bin/nft", "--file"] for command in system.commands[before:])


def test_preflight_requires_act01_and_rejects_bridge_enslaved_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    layout.act01_record.write_text("not accepted\n", encoding="utf-8")
    with pytest.raises(mac.MacLinkError, match="ACT-01"):
        migration.preflight()
    layout.act01_record.write_text(mac.ACT01_TEXT, encoding="utf-8")
    original = FakeSystem.__call__

    def enslaved(self, args, **kwargs):
        if args[:4] == ["/usr/bin/ip", "-json", "link", "show"]:
            return self.result(args, json.dumps([{"ifname": "enp9s0", "master": "br0"}]))
        return original(self, args, **kwargs)

    monkeypatch.setattr(FakeSystem, "__call__", enslaved)
    with pytest.raises(mac.MacLinkError, match="enslaved"):
        migration.preflight()


def test_verification_restarts_active_oneshot_instead_of_reusing_stale_success(tmp_path: Path) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    migration.install()
    previous = system.acceptance_restarts
    system.fail_acceptance = True
    with pytest.raises(mac.MacLinkError, match="system command failed safely"):
        migration.verify()
    assert system.acceptance_restarts == previous + 1


@pytest.mark.parametrize("failure", ["inactive", "systemctl_error"])
def test_preflight_requires_active_integrated_pipeline_and_distinguishes_errors(
    tmp_path: Path, failure: str,
) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    if failure == "inactive":
        system.pipeline_active = False
        with pytest.raises(mac.MacLinkError, match="must initially be active, not inactive"):
            migration.preflight()
    else:
        system.pipeline_show_error = True
        with pytest.raises(mac.MacLinkError, match="system command failed safely"):
            migration.preflight()


@pytest.mark.parametrize("failure", ["missing_agent", "broken_curl"])
def test_preflight_validates_both_probe_identities_and_per_user_curl(
    tmp_path: Path, failure: str,
) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    setattr(system, failure, True)
    with pytest.raises(mac.MacLinkError, match="system command failed safely"):
        migration.preflight()


def test_unexpected_probe_exit_is_operational_error_not_denial(tmp_path: Path) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    migration.install()
    system.probe_returncode = 1
    with pytest.raises(mac.MacLinkError, match="operational error: curl exit 1"):
        migration.finalize()
    probe = next(command for command in system.commands if "--connect-timeout" in command)
    assert probe[probe.index("/usr/bin/curl") + 1] == "-q"
    assert system.phase == "legacy"


def test_preflight_rejects_preexisting_routes_on_dedicated_interface(tmp_path: Path) -> None:
    migration, system, _layout = isolated_migration(tmp_path)
    system.preexisting_dedicated_route = True
    with pytest.raises(mac.MacLinkError, match="still has routes"):
        migration.preflight()


def test_rollback_requires_pipeline_stop_before_changing_policy_or_state(tmp_path: Path) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    migration.install()
    system.fail_pipeline_stop = True
    with pytest.raises(mac.MacLinkError, match="system command failed safely"):
        migration.rollback()
    assert system.phase == "staged"
    assert layout.state.exists()
    assert layout.pipeline_dropin.exists()
    system.fail_pipeline_stop = False
    migration.rollback()
    assert not layout.state.exists()


@pytest.mark.parametrize("failure", ["fail_link_stop", "link_remains_up", "profile_remains_active"])
def test_rollback_requires_verified_link_and_profile_shutdown_before_cleanup(
    tmp_path: Path, failure: str,
) -> None:
    migration, system, layout = isolated_migration(tmp_path)
    migration.install()
    setattr(system, failure, True)
    with pytest.raises(mac.MacLinkError):
        migration.rollback()
    assert system.phase == "legacy"
    assert layout.state.exists()
    assert layout.pipeline_dropin.exists()
    assert layout.nm_profile.exists()


def test_cli_offline_check_and_render_do_not_call_system_commands(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_text(), encoding="utf-8")
    assert mac.main(["check", "--config", str(config_path)]) == 0
    output = tmp_path / "rendered"
    assert mac.main(["render", "--config", str(config_path), "--pipeline-uid", str(UID),
                     "--output-dir", str(output)]) == 0
    assert {path.name for path in output.iterdir()} == {
        "nmconnection", "link_unit", "pipeline_dropin", "mac_plist", "legacy_nft", "staged_nft", "final_nft",
    }
