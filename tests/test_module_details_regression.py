"""Guards the "details only" contract of the chart-annotation work (Bot 10).

The golden file tests/fixtures/golden_candidates.json was produced by the modules
*before* annotation fields were added to ``details``. Every module is re-run on the same
fixtures and must reproduce every candidate exactly (ticker, date, entry, stop, score,
rationale) with each original ``details`` value unchanged; only new keys may appear.

The modules run with the entry rules added after the golden file was made switched off
(``LEGACY_A``: Module A's pullback-quality rules, which the synthetic MOMA1 fails), so the
golden still pins every field the annotation work must not change. The new rules have
their own tests in tests/test_modules_filters.py.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.data_access import DataAccess
from src.modules import get_enabled_modules
from tests.test_modules_scan import LEGACY_A, _patch_gap_opens

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden_candidates.json"
START, END = date(2023, 6, 1), date(2024, 12, 31)


def collect(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    data = DataAccess(conn)
    out: dict[str, list[dict]] = {}
    for m in get_enabled_modules(LEGACY_A):
        rows = []
        for c in m.scan_history(START, END, data):
            rows.append({
                "ticker": c.ticker, "module": c.module, "signal_date": c.signal_date.isoformat(),
                "entry": c.entry, "stop": c.stop, "setup_score": c.setup_score,
                "rationale": c.rationale, "details": json.loads(json.dumps(c.details)),
            })
        out[m.name] = rows
    return out


def collect_variants(db_path: Path, tmp: Path) -> dict[str, dict]:
    res = {}
    for variant, patch in (("shipped", False), ("gap_patched", True)):
        copy = tmp / f"{variant}.db"
        shutil.copy(db_path, copy)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        if patch:
            _patch_gap_opens(conn)
        res[variant] = collect(conn)
        conn.close()
    return res


@pytest.fixture(scope="module")
def current(fixture_db_path, tmp_path_factory):
    return collect_variants(fixture_db_path, tmp_path_factory.mktemp("golden"))


def test_every_module_reproduces_golden_candidates(current):
    golden = json.loads(GOLDEN.read_text())
    assert set(golden) == set(current)
    for variant, per_module in golden.items():
        assert set(per_module) == set(current[variant])
        for module, old_rows in per_module.items():
            new_rows = current[variant][module]
            assert len(new_rows) == len(old_rows), (variant, module)
            for old, new in zip(old_rows, new_rows):
                where = (variant, module, old["ticker"], old["signal_date"])
                for key in ("ticker", "module", "signal_date", "entry", "stop", "setup_score", "rationale"):
                    assert new[key] == old[key], (where, key)
                for k, v in old["details"].items():
                    assert new["details"].get(k) == v, (where, k)


def test_golden_covers_all_four_modules(current):
    golden = json.loads(GOLDEN.read_text())
    for variant in golden:
        for module in ("momentum_pullback", "earnings_gap_drift", "insider_cluster", "base_breakout"):
            assert golden[variant][module], (variant, module)
