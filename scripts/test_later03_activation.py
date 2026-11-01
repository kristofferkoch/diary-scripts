"""Negative tests for root-owned LATER-03 activation and integration evidence."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import later03_activation as activation

ACT01_DIARY = "1" * 40
CTL02_DIARY = "2" * 40
ACT01_SCRIPTS = "3" * 40
CTL02_SCRIPTS = "4" * 40
INTEGRATED_DIARY = "a" * 40
INTEGRATED_SCRIPTS = "b" * 40
POLICY_SET = "inet:diary_agent_policy:agent_egress"


def values() -> dict[str, str]:
    return {
        "ACT01_ACCEPTED": "yes",
        "ACT01_DIARY_OID": ACT01_DIARY,
        "ACT01_SCRIPTS_OID": ACT01_SCRIPTS,
        "CTL02_LIVE_VERIFIED": "yes",
        "CTL02_DIARY_OID": CTL02_DIARY,
        "CTL02_SCRIPTS_OID": CTL02_SCRIPTS,
        "INTEGRATED_DIARY_OID": INTEGRATED_DIARY,
        "INTEGRATED_SCRIPTS_OID": INTEGRATED_SCRIPTS,
        "ENFORCEMENT_SETS": POLICY_SET,
        "SCOPE_PREFIXES": "dsh-turn-,kimi-turn-",
    }


def write_evidence(path: Path, **changes: str) -> None:
    content = values()
    content.update(changes)
    path.write_text("".join(f"{key}={value}\n" for key, value in content.items()), encoding="ascii")
    path.chmod(0o600)


def layout(root: Path) -> tuple[Path, Path, Path]:
    diary = root / "diary-approved"
    scripts = root / "diary-scripts-approved"
    for relative in (
        "systemd/units/agent.slice",
        "systemd/units/diary-lockd.service",
        "dsh/dsh-web.service",
        "kimi/kimi-web.service",
    ):
        path = diary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (diary / "systemd/units/agent.slice").write_text(
        f"NFTSet=cgroup:{activation.AUDIT_SET.replace(':', ':', 1)}\n"
        f"NFTSet=cgroup:{POLICY_SET}\n"
    )
    (diary / "systemd/units/diary-lockd.service").write_text(
        f"ExecStart=/usr/local/sbin/diary-lockd --cgroup-set {activation.AUDIT_SET} "
        f"--cgroup-set {POLICY_SET}\n"
    )
    for relative in ("dsh/dsh-web.service", "kimi/kimi-web.service"):
        (diary / relative).write_text("Slice=agent.slice\n")
    source = scripts / "scripts/diary_lock.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def _turn_scopes(): pass\n"
        "def verify_scope(): pass\n"
        "self.identity.verify()\n"
        "dsh-turn-\n"
        "kimi-turn-\n"
    )
    evidence = root / "later03-activation.conf"
    write_evidence(evidence)
    return evidence, diary, scripts


class FakeGit:
    def __init__(self, diary: Path, scripts: Path) -> None:
        self.heads = {diary: INTEGRATED_DIARY, scripts: INTEGRATED_SCRIPTS}
        self.ancestors = {
            diary: {ACT01_DIARY, CTL02_DIARY},
            scripts: {ACT01_SCRIPTS, CTL02_SCRIPTS},
        }

    def __call__(self, argv: list[str], **kwargs):
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert "shell" not in kwargs
        repo = Path(argv[4])
        operation = argv[5]
        if operation == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=self.heads[repo] + "\n")
        if operation == "merge-base":
            return SimpleNamespace(returncode=0 if argv[7] in self.ancestors[repo] else 1, stdout="")
        raise AssertionError(argv)


def validate(evidence: Path, diary: Path, scripts: Path, git: FakeGit):
    return activation.validate_activation(
        evidence, diary, scripts, expected_uid=os.geteuid(), runner=git,
    )


def test_root_owned_acceptance_and_exact_integrated_oids_pass(tmp_path: Path) -> None:
    evidence, diary, scripts = layout(tmp_path)
    result = validate(evidence, diary, scripts, FakeGit(diary, scripts))
    assert result["enforcement_sets"] == (POLICY_SET,)


@pytest.mark.parametrize("flag", ["ACT01_ACCEPTED", "CTL02_LIVE_VERIFIED"])
def test_acceptance_and_live_verification_are_mandatory(tmp_path: Path, flag: str) -> None:
    evidence, diary, scripts = layout(tmp_path)
    write_evidence(evidence, **{flag: "no"})
    with pytest.raises(activation.ActivationError, match=f"does not prove {flag}"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


def test_unsafe_evidence_file_is_rejected(tmp_path: Path) -> None:
    evidence, diary, scripts = layout(tmp_path)
    evidence.chmod(0o644)
    with pytest.raises(activation.ActivationError, match="0600"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))
    evidence.chmod(0o600)
    target = tmp_path / "target"
    target.write_text(evidence.read_text())
    evidence.unlink()
    evidence.symlink_to(target)
    with pytest.raises(activation.ActivationError, match="non-symlink"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


def test_exact_approved_head_and_prerequisite_ancestry_are_enforced(tmp_path: Path) -> None:
    evidence, diary, scripts = layout(tmp_path)
    git = FakeGit(diary, scripts)
    git.heads[diary] = "f" * 40
    with pytest.raises(activation.ActivationError, match="approved HEAD"):
        validate(evidence, diary, scripts, git)
    git.heads[diary] = INTEGRATED_DIARY
    git.ancestors[scripts].remove(CTL02_SCRIPTS)
    with pytest.raises(activation.ActivationError, match="CTL02_SCRIPTS_OID is not an ancestor"):
        validate(evidence, diary, scripts, git)


def test_every_enforcement_set_must_be_in_slice_and_controller(tmp_path: Path) -> None:
    evidence, diary, scripts = layout(tmp_path)
    slice_path = diary / "systemd/units/agent.slice"
    slice_path.write_text(f"NFTSet=cgroup:{activation.AUDIT_SET}\n")
    with pytest.raises(activation.ActivationError, match="agent.slice set collection mismatch"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))
    slice_path.write_text(f"NFTSet=cgroup:{activation.AUDIT_SET}\nNFTSet=cgroup:{POLICY_SET}\n")
    unit = diary / "systemd/units/diary-lockd.service"
    unit.write_text(f"ExecStart=/usr/local/sbin/diary-lockd --cgroup-set {activation.AUDIT_SET}\n")
    with pytest.raises(activation.ActivationError, match="diary-lockd set collection mismatch"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


@pytest.mark.parametrize("target", ["slice", "controller"])
def test_extra_set_is_rejected(tmp_path: Path, target: str) -> None:
    evidence, diary, scripts = layout(tmp_path)
    extra = "inet:diary_agent_policy:unsupervised"
    if target == "slice":
        path = diary / "systemd/units/agent.slice"
        path.write_text(path.read_text() + f"NFTSet=cgroup:{extra}\n")
    else:
        path = diary / "systemd/units/diary-lockd.service"
        path.write_text(path.read_text().rstrip() + f" --cgroup-set {extra}\n")
    with pytest.raises(activation.ActivationError, match="extra="):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


@pytest.mark.parametrize("target", ["slice", "controller"])
def test_duplicate_set_is_rejected(tmp_path: Path, target: str) -> None:
    evidence, diary, scripts = layout(tmp_path)
    if target == "slice":
        path = diary / "systemd/units/agent.slice"
        path.write_text(path.read_text() + f"NFTSet=cgroup:{POLICY_SET}\n")
    else:
        path = diary / "systemd/units/diary-lockd.service"
        path.write_text(path.read_text().rstrip() + f" --cgroup-set {POLICY_SET}\n")
    with pytest.raises(activation.ActivationError, match="duplicate cgroup sets"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


def test_integrated_scope_configuration_is_mandatory(tmp_path: Path) -> None:
    evidence, diary, scripts = layout(tmp_path)
    write_evidence(evidence, SCOPE_PREFIXES="dsh-turn-")
    with pytest.raises(activation.ActivationError, match="SCOPE_PREFIXES"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))
    write_evidence(evidence)
    (diary / "dsh/dsh-web.service").write_text("Slice=system.slice\n")
    with pytest.raises(activation.ActivationError, match="not integrated with agent.slice"):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))


@pytest.mark.parametrize("sets", ["", POLICY_SET + "," + POLICY_SET, activation.AUDIT_SET, "bad"])
def test_malformed_or_audit_only_enforcement_sets_are_rejected(tmp_path: Path, sets: str) -> None:
    evidence, diary, scripts = layout(tmp_path)
    write_evidence(evidence, ENFORCEMENT_SETS=sets)
    with pytest.raises(activation.ActivationError):
        validate(evidence, diary, scripts, FakeGit(diary, scripts))
