"""Shared pytest fixtures.

fixture_db_path / fixture_conn / data_access give every bot's tests read access to
the synthetic fixture built by tests/fixtures/generate_fixtures.py. Per
docs/BOT_RULES.md, tests must never write to data/research.db or state/ -- if a test
needs to mutate a DB, it should copy fixture.db to a tmp_path first (see
`writable_fixture_conn` below).
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from src.data_access import DataAccess

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_DB_PATH = FIXTURES_DIR / "fixture.db"
FIXTURE_STATE_DIR = FIXTURES_DIR / "state"


@pytest.fixture(scope="session")
def fixture_db_path() -> Path:
    if not FIXTURE_DB_PATH.exists():
        raise RuntimeError(
            "tests/fixtures/fixture.db is missing -- run "
            "`python -m tests.fixtures.generate_fixtures` first."
        )
    return FIXTURE_DB_PATH


@pytest.fixture()
def fixture_conn(fixture_db_path: Path) -> sqlite3.Connection:
    """Read-only connection to the committed fixture DB."""
    uri = f"file:{fixture_db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def writable_fixture_conn(fixture_db_path: Path, tmp_path: Path) -> sqlite3.Connection:
    """A private tmp_path copy of fixture.db, safe to write to in a test."""
    copy_path = tmp_path / "fixture_copy.db"
    shutil.copy(fixture_db_path, copy_path)
    conn = sqlite3.connect(copy_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def data_access(fixture_conn: sqlite3.Connection) -> DataAccess:
    return DataAccess(fixture_conn)


@pytest.fixture(scope="session")
def fixture_state_dir() -> Path:
    return FIXTURE_STATE_DIR
