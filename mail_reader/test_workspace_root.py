"""Tests for workspace_root() — the fix for the `Path(__file__)` smell.

The bug: modules located the diary data root with
``Path(__file__).resolve().parent.parent``. That answer depends on *where the
module file sits*, so relocating the code (e.g. mounting it as a git submodule
one level deeper) silently pointed every data path at the wrong directory.

These tests pin the property that broke — the resolved root must NOT depend on
the caller's location — plus an architectural guard so the smell can't return.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import mail_reader
import scripts
from mail_reader import config


# ---- behaviour: config / env drive the root, not __file__ ------------------

def test_config_paths_project_wins(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(f'[paths]\nproject = "{tmp_path / "ws"}"\n')
    monkeypatch.setenv("DIARY_CONFIG", str(cfgfile))
    config.reload()
    assert config.workspace_root() == tmp_path / "ws"
    config.reload()


def test_diary_root_env_used_when_no_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DIARY_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "noxdg"))
    monkeypatch.setenv("DIARY_ROOT", str(tmp_path / "ws"))
    config.reload()
    assert config.workspace_root() == tmp_path / "ws"
    config.reload()


# ---- the creative one: location-independence (move simulation) -------------

_PROBE_SRC = "from mail_reader.config import workspace_root\nROOT = workspace_root()\n"


def _load_probe(path: Path):
    """Write a tiny module that asks for workspace_root() and import it from
    `path`, so we can compare the answer from two different filesystem depths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PROBE_SRC)
    spec = importlib.util.spec_from_file_location(f"probe_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ROOT


def test_workspace_root_is_independent_of_caller_depth(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.delenv("DIARY_CONFIG", raising=False)
    monkeypatch.setenv("DIARY_ROOT", str(ws))
    config.reload()

    shallow = _load_probe(tmp_path / "shallow.py")
    deep = _load_probe(tmp_path / "a" / "b" / "c" / "deep.py")

    # The OLD Path(__file__).parent.parent approach would give two DIFFERENT
    # answers here (tmp_path vs tmp_path/a/b); the config-driven resolver gives
    # the same one regardless of where the calling module lives.
    assert shallow == deep == ws
    config.reload()


# ---- architectural guard: the smell must not come back ---------------------

# Two-or-more-levels-up __file__ walking is how modules used to find the
# workspace; a single `.parent` (a code-relative sibling/static file) is fine.
_SMELL = re.compile(r"__file__\)[^\n]*?(?:\.parent\.parent|\.parents\[)")


def _package_py_files():
    for pkg in (scripts, mail_reader):
        root = Path(pkg.__path__[0])
        for p in root.rglob("*.py"):
            if p.name.startswith("test_") or p.name == "conftest.py":
                continue
            yield p


def test_no_module_rederives_workspace_root_from_file():
    offenders = []
    for p in _package_py_files():
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if _SMELL.search(line):
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, (
        "Modules must use mail_reader.config.workspace_root() instead of "
        "walking __file__ to find the workspace:\n  " + "\n  ".join(offenders)
    )
