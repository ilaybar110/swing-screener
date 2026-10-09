"""End-to-end test of backtest/runner.py on the fixture DB, with a deterministic
stand-in strategy module (real modules are covered by Bot 4's tests)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from backtest.runner import SURVIVORSHIP_WARNING, config_snapshot, run
from src.config import load_config
from src.contracts import Candidate

CFG = load_config()
DAYS = [date(2024, 6, 14), date(2024, 6, 21)]


class FakeModule:
    name = "fake_module"

    def scan(self, as_of, data):
        return self.scan_history(as_of, as_of, data)

    def scan_history(self, start, end, data):
        out = []
        for d in DAYS:
            if start <= d <= end:
                # a different ticker on the second day: a repeat of a still-live ticker would be folded (D3)
                tk = "MOMA1" if d == DAYS[0] else "MOMB1"
                px = float(data.get_prices(tk, d, d)["close"].iloc[0])
                out.append(Candidate(tk, self.name, d, px, px * 0.95, 70.0, "fake", {}))
        return out


@pytest.fixture()
def bt(writable_fixture_conn):
    lines: list[str] = []
    rid = run(date(2024, 6, 3), date(2024, 6, 28), "unit test", conn=writable_fixture_conn,
              config=CFG, modules=[FakeModule()], build_universe=False, print_fn=lines.append)
    return writable_fixture_conn, rid, "\n".join(lines)


def test_run_row_has_config_snapshot_and_hash(bt):
    conn, rid, _ = bt
    row = conn.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (rid,)).fetchone()
    snap, h = config_snapshot(CFG)
    assert row["config_hash"] == h and json.loads(row["config_snapshot"]) == json.loads(snap)
    assert row["notes"] == "unit test" and row["start_date"] == "2024-06-03"


def test_recommendations_stored_with_run_source_only(bt):
    conn, rid, _ = bt
    rows = conn.execute("SELECT id, source, ticker, primary_module FROM recommendations").fetchall()
    assert len(rows) == 2
    assert all(r["source"] == rid and r["id"].startswith(rid + "/") for r in rows)
    assert not conn.execute("SELECT 1 FROM recommendations WHERE source='live'").fetchone()


def test_everything_simulated_through_the_same_tracker(bt):
    conn, rid, _ = bt
    st = {r["status"] for r in conn.execute("SELECT status FROM recommendations")}
    assert st <= {"stopped", "target_hit", "time_stop", "expired", "open", "pending"}
    finals = conn.execute("SELECT COUNT(*) FROM recommendation_results").fetchone()[0]
    assert finals == conn.execute(
        "SELECT COUNT(*) FROM recommendations WHERE status IN ('stopped','target_hit','time_stop','expired')"
    ).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM baselines WHERE source=?", (rid,)).fetchone()[0] == 6


def test_report_prints_period_table_and_survivorship_warning(bt):
    _, _, text = bt
    assert SURVIVORSHIP_WARNING in text
    assert "== 2023+ ==" in text and "fake_module" in text
    assert "not enough data yet" in text


def test_two_runs_coexist_and_agree(writable_fixture_conn):
    kw = dict(conn=writable_fixture_conn, config=CFG, modules=[FakeModule()],
              build_universe=False, print_fn=None)
    a = run(date(2024, 6, 3), date(2024, 6, 28), **kw)
    b = run(date(2024, 6, 3), date(2024, 6, 28), **kw)
    assert a != b

    def results(rid):
        return [tuple(r) for r in writable_fixture_conn.execute(
            "SELECT substr(r.id, instr(r.id,'/')+1), x.final_status, x.r_multiple FROM recommendations r "
            "LEFT JOIN recommendation_results x ON x.rec_id = r.id WHERE r.source=? ORDER BY r.id", (rid,))]

    assert results(a) == results(b) and len(results(a)) == 2

    def bases(rid):
        return [tuple(r) for r in writable_fixture_conn.execute(
            "SELECT ticker, status, r_multiple FROM baselines WHERE source=? ORDER BY ticker, sample_no", (rid,))]

    assert bases(a) == bases(b)  # seeded draws: the control group is reproducible
