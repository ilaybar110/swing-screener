"""Tests for src/forward_returns.py: the pure horizon computation on hand-built bars,
the DB update (point-in-time, write-once) on the synthetic fixture, and the
state/forward_returns round trip through src/state_io.py."""

from __future__ import annotations

import filecmp
from datetime import date

import pandas as pd
import pytest

from src import forward_returns as fr
from src.baseline import create_for
from src.config import load_config
from src.db import init_db
from src.state_io import export_state, import_state
from src.tracker import _Bars, row_to_rec, update_all

CFG = load_config()
SIGNAL = date(2024, 6, 14)


def _bars(closes, start="2024-01-02", opens=None):
    ds = pd.bdate_range(start, periods=len(closes))
    opens = opens or closes
    return _Bars(pd.DataFrame({
        "date": [d.date().isoformat() for d in ds], "open": opens, "high": closes,
        "low": closes, "close": closes,
    }))


# ---- pure computation --------------------------------------------------------


def test_horizon_runs_from_next_open_to_close_of_hth_bar():
    closes = [100.0] * 3 + [float(110 + i) for i in range(10)]
    opens = [100.0] * 3 + [105.0] + [float(110 + i) for i in range(1, 10)]
    stock = _bars(closes, opens=opens)
    spy = _bars([200.0] * 3 + [200.0 + i for i in range(10)])
    sig = stock.dates[2]
    out = fr.compute(stock, sig, [1, 5, 20], spy)
    assert set(out) == {1, 5}  # 20 bars not yet available
    assert out[5]["start_date"] == stock.dates[3].isoformat()
    assert out[5]["end_date"] == stock.dates[7].isoformat()
    assert out[5]["ret"] == pytest.approx(114 / 105 - 1)
    assert out[1]["ret"] == pytest.approx(110 / 105 - 1)
    assert out[5]["spy_return"] == pytest.approx(204 / 200 - 1)
    assert out[5]["excess_vs_spy"] == pytest.approx(out[5]["ret"] - out[5]["spy_return"])
    assert out[5]["sector_etf_return"] is None and out[5]["excess_vs_sector"] is None


def test_incomplete_benchmark_defers_the_horizon():
    stock = _bars([100.0 + i for i in range(12)])
    spy = _bars([200.0] * 5)  # SPY data ends before the 5-day end date
    out = fr.compute(stock, stock.dates[0], [2, 5], spy)
    assert set(out) == {2}
    assert fr.compute(stock, stock.dates[0], [5], None) == {}


def test_no_bar_after_signal_is_empty():
    stock = _bars([100.0] * 3)
    assert fr.compute(stock, stock.dates[-1], [5], stock) == {}


# ---- DB layer ----------------------------------------------------------------


def _insert_rec(conn, ticker="MOMA1", signal=SIGNAL, sector="Technology"):
    px = conn.execute("SELECT close FROM prices WHERE ticker=? AND date=?",
                      (ticker, signal.isoformat())).fetchone()["close"]
    rid = f"{signal.isoformat()}_{ticker}"
    conn.execute(
        "INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,"
        "entry,stop,target,valid_until,status,sector) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, "live", signal.isoformat(), ticker, '["a"]', "a", px, px * 0.95, px * 1.10,
         signal.isoformat(), "pending", sector),
    )
    conn.commit()
    return row_to_rec(conn.execute("SELECT * FROM recommendations WHERE id=?", (rid,)).fetchone())


def _rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM forward_returns ORDER BY subject_id, horizon")]


def test_update_is_point_in_time_write_once_and_idempotent(writable_fixture_conn):
    conn = writable_fixture_conn
    rec = _insert_rec(conn)
    create_for(conn, [rec], SIGNAL, CFG)
    days = [r["date"] for r in conn.execute(
        "SELECT date FROM prices WHERE ticker='SPY' AND date > ? ORDER BY date", (SIGNAL.isoformat(),))]

    # as of the 7th session after the signal only the 5-day horizon is complete
    as_of = date.fromisoformat(days[6])
    n = fr.update_forward_returns(conn, as_of, "live", CFG)
    rows = _rows(conn)
    assert n == 4 and {r["horizon"] for r in rows} == {5}
    assert {r["kind"] for r in rows} == {"rec", "baseline"}
    assert all(r["end_date"] <= as_of.isoformat() for r in rows)
    rec_row = next(r for r in rows if r["kind"] == "rec")
    o = conn.execute("SELECT open FROM prices WHERE ticker='MOMA1' AND date=?", (days[0],)).fetchone()[0]
    c = conn.execute("SELECT close FROM prices WHERE ticker='MOMA1' AND date=?", (days[4],)).fetchone()[0]
    assert rec_row["start_date"] == days[0] and rec_row["end_date"] == days[4]
    assert rec_row["ret"] == pytest.approx(c / o - 1)
    assert rec_row["sector_etf"] == "XLK" and rec_row["sector_etf_return"] is not None
    assert rec_row["parent_rec_id"] == rec.id
    assert all(r["parent_rec_id"] == rec.id for r in rows)
    assert fr.update_forward_returns(conn, as_of, "live", CFG) == 0

    # complete rows are never rewritten, even if prices later change
    first = _rows(conn)
    conn.execute("UPDATE prices SET close = close * 2 WHERE ticker='MOMA1'")
    conn.commit()
    fr.update_forward_returns(conn, date(2024, 12, 31), "live", CFG)
    later = _rows(conn)
    assert [r for r in later if r["horizon"] == 5] == first
    assert {r["horizon"] for r in later} == {5, 10, 20, 40, 60}
    assert len(later) == 4 * 5


def test_update_all_fills_forward_returns(writable_fixture_conn):
    conn = writable_fixture_conn
    rec = _insert_rec(conn)
    create_for(conn, [rec], SIGNAL, CFG)
    counts = update_all(conn, date(2024, 12, 31), "live", CFG)
    assert counts["forward_returns"] == 20
    assert counts["simulated"] == 1


def test_other_sources_untouched(writable_fixture_conn):
    conn = writable_fixture_conn
    _insert_rec(conn)
    assert fr.update_forward_returns(conn, date(2024, 12, 31), "bt1", CFG) == 0
    assert _rows(conn) == []


def test_load_without_table_is_empty(tmp_path):
    conn = init_db(tmp_path / "x.db")
    df = fr.load(conn, "live")
    assert df.empty and list(df.columns) == fr.FIELDS
    assert not fr.table_exists(conn)


def test_state_round_trip_is_deterministic(writable_fixture_conn, tmp_path):
    conn = writable_fixture_conn
    rec = _insert_rec(conn)
    create_for(conn, [rec], SIGNAL, CFG)
    update_all(conn, date(2024, 12, 31), "live", CFG)
    out1 = tmp_path / "e1"
    export_state(conn, out1, as_of=date(2024, 12, 31))
    shard = out1 / "forward_returns" / "2024-06.csv"
    assert shard.exists()
    assert shard.read_text(encoding="utf-8").splitlines()[0] == ",".join(fr.FIELDS)

    conn2 = init_db(tmp_path / "r2.db")
    import_state(conn2, out1)
    out2 = tmp_path / "e2"
    export_state(conn2, out2, as_of=date(2024, 12, 31))
    assert filecmp.cmp(shard, out2 / "forward_returns" / "2024-06.csv", shallow=False)
    assert fr.update_forward_returns(conn2, date(2024, 12, 31), "live", CFG) == 0
