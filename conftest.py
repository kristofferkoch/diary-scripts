"""Make the test suite hermetic with respect to runtime config.

The code reads real deployment values from a private config file
($DIARY_CONFIG or ~/.config/diary/config.toml) — see mail_reader/config.py.
Tests must NOT depend on whether that file happens to exist on the machine
running them: they assert against the placeholder values and fixtures shipped
in this repo. So neutralise any ambient config before test modules (and the
import-time constants in priority.py / kindle_dashboard/data.py) are imported.

This runs at conftest import, i.e. before test collection. Individual tests
that need a config (mail_reader/test_config.py) set DIARY_CONFIG themselves
via monkeypatch + config.reload().
"""
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

os.environ.pop("DIARY_CONFIG", None)
os.environ["XDG_CONFIG_HOME"] = "/nonexistent-diary-xdg-for-tests"
# Pin the workspace root to this repo dir so workspace_root() is deterministic
# in tests and never autodetects a real CALENDAR.md on the dev machine.
os.environ.setdefault("DIARY_ROOT", str(Path(__file__).resolve().parent))

# Public host endpoints must also resolve before doctests import embed_mail or
# embed_images. Pin fixed config paths to sanitized fixtures, never real
# deployment files. Tests for missing/malformed configs monkeypatch these paths.
from mail_reader import config as diary_config
diary_config.HOSTS_CONFIG_PATH = Path(__file__).resolve().parent / "fixtures" / "hosts.toml"
diary_config.TRUSTED_CONFIG_PATH = Path("/nonexistent-diary-trusted-for-tests.toml")
diary_config.reload()


@pytest.fixture(scope="session")
def test_pg_dsn() -> Iterator[str]:
    """A migrated, disposable schema; never the application's tables."""
    base_dsn = os.environ.get("DIARY_TEST_PG_DSN")
    if not base_dsn:
        pytest.skip("set DIARY_TEST_PG_DSN to run PostgreSQL integration tests")

    schema = f"diary_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    test_dsn = make_conninfo(base_dsn, options=f"-csearch_path={schema},public")
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(test_dsn) as conn:
            for migration in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                conn.execute(migration.read_text())
            conn.commit()
        yield test_dsn
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def db_conn(test_pg_dsn: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(test_pg_dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.execute("TRUNCATE messages, entities, themes, agenda_dismissed CASCADE")
        conn.commit()
        conn.close()
