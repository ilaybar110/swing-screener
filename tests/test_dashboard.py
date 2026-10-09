"""Dashboard tests: pure data helpers plus a smoke run of every view through
Streamlit's AppTest, against a backtest DB built from the fixture (no network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from backtest.runner import run
from dashboard import data as dd
from src.config import load_config
from src.contracts import Candidate
from src.state_io import export_state

APP = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
CFG = load_config()


class FakeModule:
    name = "fake_module"

    def scan(self, as_of, data):
        return self.scan_history(as_of, as_of, data)

    def scan_history(self, start, end, data):
        out = []
        for d in (date(2024, 6, 14), date(2024, 6, 21)):
            if start <= d <= end:
                # a different ticker on the second day: a repeat of a still-live ticker would be folded (D3)
                tk = "MOMA1" if d == date(2024, 6, 14) else "MOMB1"
                px = float(data.get_prices(tk, d, d)["close"].iloc[0])
                out.append(Candidate(tk, self.name, d, px, px * 0.95, 70.0, "fake setup", {}))
        return out


@pytest.fixture()
def populated(writable_fixture_conn, tmp_path):
    conn = writable_fixture_conn
    rid = run(date(2024, 6, 3), date(2024, 6, 28), "dash test", conn=conn, config=CFG,
              modules=[FakeModule()], build_universe=False, print_fn=None)
    return conn, rid, tmp_path


def test_recommendations_table_decodes_json_and_joins_results(populated):
    conn, rid, _ = populated
    df = dd.recommendations_table(conn, rid)
    assert len(df) == 2
    assert df["modules"].iloc[0] == ["fake_module"] and df["modules_str"].iloc[0] == "fake_module"
    assert df["result_r"].notna().all()


def test_source_options_lists_live_then_runs(populated):
    conn, rid, _ = populated
    opts = dd.source_options(conn)
    assert list(opts.values()) == ["live", rid]
    assert dd.source_options(None) == {"Live": "live"}


def test_regime_runs_collapse_contiguous_days():
    h = pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]),
                      "regime": ["Caution", "Caution", "Favorable", "Caution"]})
    r = dd.regime_runs(h)
    assert list(r["regime"]) == ["Caution", "Favorable", "Caution"] and list(r["days"]) == [2, 1, 1]


def test_brief_sections_handles_missing_and_malformed():
    assert dd.brief_sections(None) == {} and dd.brief_sections({"summary": ""}) == {}
    b = dd.brief_sections({"summary": "x", "red_flags": ["a"], "sources": None})
    assert b["summary"] == "x" and b["red_flags"] == ["a"] and b["sources"] == []


def test_live_state_import_roundtrip(populated, tmp_path):
    """connect_live reads exactly what export_state wrote."""
    conn, rid, _ = populated
    conn.execute("UPDATE recommendations SET source='live'")
    conn.commit()
    export_state(conn, tmp_path / "state", date(2024, 12, 31))
    live = dd.connect_live(tmp_path / "state")
    assert len(dd.recommendations_table(live, "live")) == 2


def test_chart_window_bounds():
    row = pd.Series({"signal_date": "2024-06-14", "exit_date": "2024-07-10"})
    start, end = dd.chart_window(row, today=date(2024, 12, 1))
    assert start == date(2024, 4, 30) and end == date(2024, 7, 30)


@pytest.mark.parametrize("view", [
    "Overview", "Modules vs baseline", "Rank buckets", "Breakdowns", "Equity curve",
    "Excess returns", "All recommendations", "Recommendation detail"])
def test_every_view_renders_for_a_backtest_run(populated, monkeypatch, view):
    from streamlit.testing.v1 import AppTest

    conn, rid, tmp = populated
    path = tmp / "research.db"
    import sqlite3
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    monkeypatch.setattr(dd, "research_db_path", lambda: path)
    monkeypatch.setattr(dd, "state_dir", lambda: tmp / "no_state")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    at.sidebar.selectbox[0].select(next(l for l in at.sidebar.selectbox[0].options if rid in l)).run()
    at.sidebar.radio[0].set_value(view).run()
    assert not at.exception, [e.value for e in at.exception]


def test_live_source_with_empty_state_renders(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(dd, "research_db_path", lambda: tmp_path / "missing.db")
    monkeypatch.setattr(dd, "state_dir", lambda: tmp_path / "no_state")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
