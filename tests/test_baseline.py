"""Tests for src/baseline.py against the synthetic fixture DB."""

from __future__ import annotations

from datetime import date

import pytest

from src.baseline import create_for
from src.config import load_config
from src.tracker import row_to_rec, update_all

CFG = load_config()
SIGNAL = date(2024, 6, 14)


def _insert_rec(conn, ticker="MOMA1", signal=SIGNAL, source="live"):
    px = conn.execute(
        "SELECT close FROM prices WHERE ticker=? AND date=?", (ticker, signal.isoformat())
    ).fetchone()["close"]
    rid = f"{signal.isoformat()}_{ticker}"
    conn.execute(
        "INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,"
        "entry,stop,target,valid_until,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, source, signal.isoformat(), ticker, '["a"]', "a", px, px * 0.95,
         px * 1.10, signal.isoformat(), "pending"),
    )
    conn.commit()
    return row_to_rec(conn.execute("SELECT * FROM recommendations WHERE id=?", (rid,)).fetchone())


def _tickers(conn):
    return [r["ticker"] for r in conn.execute("SELECT ticker FROM baselines ORDER BY id")]


def test_draws_three_distinct_tickers_excluding_the_rec(writable_fixture_conn):
    rec = _insert_rec(writable_fixture_conn)
    n = create_for(writable_fixture_conn, [rec], SIGNAL, CFG)
    ts = _tickers(writable_fixture_conn)
    assert n == 3 and len(ts) == len(set(ts)) == 3
    assert rec.ticker not in ts
    uni = {r["ticker"] for r in writable_fixture_conn.execute(
        "SELECT ticker FROM universe_snapshots WHERE snapshot_date = "
        "(SELECT MAX(snapshot_date) FROM universe_snapshots WHERE snapshot_date <= ?)",
        (SIGNAL.isoformat(),))}
    assert set(ts) <= uni


def test_stop_distance_copied_in_percent(writable_fixture_conn):
    rec = _insert_rec(writable_fixture_conn)
    create_for(writable_fixture_conn, [rec], SIGNAL, CFG)
    pcts = [r["stop_pct"] for r in writable_fixture_conn.execute("SELECT stop_pct FROM baselines")]
    assert len(pcts) == 3 and all(p == pytest.approx(0.05) for p in pcts)


def test_draw_is_deterministic_and_idempotent(writable_fixture_conn):
    rec = _insert_rec(writable_fixture_conn)
    create_for(writable_fixture_conn, [rec], SIGNAL, CFG)
    first = _tickers(writable_fixture_conn)
    assert create_for(writable_fixture_conn, [rec], SIGNAL, CFG) == 0
    assert _tickers(writable_fixture_conn) == first
    writable_fixture_conn.execute("DELETE FROM baselines")
    create_for(writable_fixture_conn, [rec], SIGNAL, CFG)
    assert _tickers(writable_fixture_conn) == first


def test_different_recs_get_different_draws(writable_fixture_conn):
    a = _insert_rec(writable_fixture_conn, "MOMA1")
    b = _insert_rec(writable_fixture_conn, "MOMB1")
    create_for(writable_fixture_conn, [a, b], SIGNAL, CFG)
    rows = writable_fixture_conn.execute("SELECT parent_rec_id, ticker FROM baselines").fetchall()
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_rec_id"], set()).add(r["ticker"])
    assert len(by_parent) == 2
    assert "MOMA1" not in by_parent[a.id] and "MOMB1" not in by_parent[b.id]
    assert by_parent[a.id] != by_parent[b.id]


def test_future_signals_are_skipped(writable_fixture_conn):
    rec = _insert_rec(writable_fixture_conn)
    assert create_for(writable_fixture_conn, [rec], date(2024, 6, 1), CFG) == 0


def test_update_all_advances_and_finalises_baselines(writable_fixture_conn):
    conn = writable_fixture_conn
    rec = _insert_rec(conn)
    create_for(conn, [rec], SIGNAL, CFG)
    update_all(conn, date(2024, 6, 14), "live", CFG)  # no next session yet
    assert {r["status"] for r in conn.execute("SELECT status FROM baselines")} == {"pending"}

    update_all(conn, date(2024, 12, 31), "live", CFG)  # > 20 sessions later
    rows = conn.execute("SELECT * FROM baselines").fetchall()
    assert all(r["status"] in ("stopped", "target_hit", "time_stop") for r in rows)
    assert all(r["r_multiple"] is not None and r["exit_date"] for r in rows)
    snap = [tuple(r) for r in rows]
    update_all(conn, date(2024, 12, 31), "live", CFG)
    assert [tuple(r) for r in conn.execute("SELECT * FROM baselines")] == snap
