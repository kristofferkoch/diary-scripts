"""Import the console against exactly the helpers the installer deploys."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HELPERS = (
    "__init__.py",
    "agent_merge_console.py",
    "agent_merge_pii.py",
    "agent_merge_queue.py",
    "agent_merge_review.py",
    "diary_audit.py",
    "diary_lock_console_ui.py",
    "diary_lock_ui.py",
    "diary_merge_console_service.py",
    "diary_production_baseline.py",
)

SOURCE = Path(__file__).resolve().parent


def test_console_and_executor_import_from_the_deployed_helper_set(tmp_path: Path) -> None:
    """The installed copy has no mail stack, venv, or third-party packages.

    The installer deploys a fixed helper list into
    /usr/local/lib/diary-lock-ui/scripts and runs it with the system Python, so
    a module-level import outside that set fails at service start. Reproduce the
    layout with -S (no site-packages) and import both entry points.
    """
    package = tmp_path / "scripts"
    package.mkdir()
    for name in HELPERS:
        shutil.copy(SOURCE / name, package / name)

    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONNOUSERSITE": "1"}
    proc = subprocess.run(
        [sys.executable, "-S", "-c",
         "import scripts.diary_lock_console_ui, scripts.diary_merge_console_service"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 0, proc.stderr
