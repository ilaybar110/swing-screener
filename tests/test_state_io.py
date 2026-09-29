"""Round-trip test: import_state(export_state(X)) must reproduce X byte-for-byte
in the exported CSVs, and export_state(import_state(csvs)) must reproduce the CSVs.
"""

from __future__ import annotations

import filecmp
from datetime import date
from pathlib import Path

from src.db import init_db
from src.state_io import export_state, import_state


def _all_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_export_import_export_is_stable(tmp_path: Path, fixture_state_dir: Path):
    db_path = tmp_path / "run.db"
    conn = init_db(db_path)
    import_state(conn, fixture_state_dir)

    first_export = tmp_path / "export1"
    export_state(conn, first_export, as_of=date(2024, 12, 31))

    conn2 = init_db(tmp_path / "run2.db")
    import_state(conn2, first_export)

    second_export = tmp_path / "export2"
    export_state(conn2, second_export, as_of=date(2024, 12, 31))

    files1 = _all_files(first_export)
    files2 = _all_files(second_export)
    assert files1 == files2

    mismatches = []
    for rel in sorted(files1):
        if not filecmp.cmp(first_export / rel, second_export / rel, shallow=False):
            mismatches.append(rel)
    assert not mismatches, f"non-deterministic export for: {mismatches}"


def test_export_matches_committed_fixture_state(tmp_path: Path, fixture_db_path: Path, fixture_state_dir: Path):
    """Re-exporting the committed fixture.db should reproduce the committed
    fixture/state/ directory exactly -- guards against fixture.db and its state/
    export drifting apart."""
    import shutil
    import sqlite3

    db_copy = tmp_path / "fixture_copy.db"
    shutil.copy(fixture_db_path, db_copy)
    conn = sqlite3.connect(db_copy)
    conn.row_factory = sqlite3.Row

    out_dir = tmp_path / "reexport"
    export_state(conn, out_dir, as_of=date(2024, 12, 31))

    files_committed = _all_files(fixture_state_dir)
    files_reexport = _all_files(out_dir)
    assert files_committed == files_reexport

    mismatches = []
    for rel in sorted(files_committed):
        if not filecmp.cmp(fixture_state_dir / rel, out_dir / rel, shallow=False):
            mismatches.append(rel)
    assert not mismatches, f"fixture/state/ is stale for: {mismatches}"
