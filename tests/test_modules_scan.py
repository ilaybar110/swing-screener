"""Strategy-module tests against tests/fixtures/fixture.db (Bot 4).

Module B's engineered fixture ticker (MOMB1) has a close-to-close gap but an *open*
within 0.3% of the prior close, so it cannot satisfy the spec'd "opened >= 5% above prior
close" test. Tests that need B therefore run on a private copy of the DB where the
gap-day open of MOMB1 (+7%) / NEGB1 (+3%) is set to match the documented scenario (see
docs/CHANGE_REQUESTS.md, Bot 4 entry).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import numpy as np
import pytest

from src.config import load_config
from src.data_access import DataAccess
from src.modules import MODULE_CLASSES, get_enabled_modules
from src.utils.calendar import trading_days

CFG = load_config()
GAP_DATE = "2024-12-03"
TAIL_START = date(2024, 11, 1)
END = date(2024, 12, 31)


def _patch_gap_opens(conn: sqlite3.Connection) -> None:
    for ticker, gap in (("MOMB1", 1.07), ("NEGB1", 1.03)):
        prev = conn.execute(
            "SELECT close FROM prices WHERE ticker=? AND date<? ORDER BY date DESC LIMIT 1",
            (ticker, GAP_DATE),
        ).fetchone()[0]
        conn.execute(
            "UPDATE prices SET open=? WHERE ticker=? AND date=?", (prev * gap, ticker, GAP_DATE)
        )
    conn.commit()


@pytest.fixture()
def pdata(writable_fixture_conn) -> DataAccess:
    """DataAccess over a private copy of the fixture with the module-B gap opens fixed."""
    _patch_gap_opens(writable_fixture_conn)
    return DataAccess(writable_fixture_conn)


def _module(name: str):
    return next(m for m in get_enabled_modules(CFG) if m.name == name)


def _hits(name: str, data, start=TAIL_START, end=END):
    return _module(name).scan_history(start, end, data)


def _by_ticker(cands):
    out: dict[str, list] = {}
    for c in cands:
        out.setdefault(c.ticker, []).append(c)
    return out


# ---------------------------------------------------------------------------
# Designated fixture tickers
# ---------------------------------------------------------------------------


def test_module_a_triggers_on_moma1_not_nega1(pdata):
    hits = _by_ticker(_hits("momentum_pullback", pdata))
    assert [c.signal_date for c in hits["MOMA1"]] == [date(2024, 12, 31)]
    assert "NEGA1" not in hits


def test_module_b_triggers_on_momb1_not_negb1(pdata):
    hits = _by_ticker(_hits("earnings_gap_drift", pdata))
    assert [c.signal_date for c in hits["MOMB1"]] == [date(2024, 12, 10)]
    assert hits["MOMB1"][0].details["gap_date"] == GAP_DATE
    assert "NEGB1" not in hits


def test_module_b_needs_a_real_opening_gap(data_access):
    """On the unpatched fixture the +7% is close-to-close only, so B must stay quiet."""
    assert "MOMB1" not in _by_ticker(_hits("earnings_gap_drift", data_access))


def test_module_c_triggers_on_momc1_not_negc1(pdata):
    hits = _by_ticker(_hits("insider_cluster", pdata))
    assert [c.signal_date for c in hits["MOMC1"]] == [date(2024, 11, 18)]
    assert hits["MOMC1"][0].details["insider_count"] == 2
    assert "NEGC1" not in hits


def test_module_d_triggers_on_momd1_not_negd1(pdata):
    hits = _by_ticker(_hits("base_breakout", pdata))
    assert [c.signal_date for c in hits["MOMD1"]] == [date(2024, 12, 24)]
    assert "NEGD1" not in hits


def test_no_module_fires_on_negatives_in_engineered_window(pdata):
    for m in get_enabled_modules(CFG):
        got = {c.ticker for c in m.scan_history(TAIL_START, END, pdata)}
        assert not got & {"NEGA1", "NEGB1", "NEGC1", "NEGD1"}, m.name


def test_candidate_levels_are_valid(pdata):
    lo, hi = CFG.modules.stop_distance_pct_min, CFG.modules.stop_distance_pct_max
    for m in get_enabled_modules(CFG):
        for c in m.scan_history(date(2023, 6, 1), END, pdata):
            assert 0 <= c.setup_score <= 100
            dist = (c.entry - c.stop) / c.entry
            assert lo <= dist <= hi, (m.name, c.ticker, c.signal_date, dist)
            bar = pdata.get_prices(c.ticker, c.signal_date, c.signal_date).iloc[0]
            assert c.entry == pytest.approx(bar["high"], abs=1e-3)  # buy-stop at trigger high


def test_get_enabled_modules_respects_config():
    cfg = load_config()
    assert [m.name for m in get_enabled_modules(cfg)] == [
        "momentum_pullback", "earnings_gap_drift", "insider_cluster", "base_breakout"
    ]
    cfg.modules.c_insider_cluster.enabled = False
    cfg.modules.a_momentum_pullback.enabled = False
    assert [m.name for m in get_enabled_modules(cfg)] == ["earnings_gap_drift", "base_breakout"]
    assert set(MODULE_CLASSES) == {
        "a_momentum_pullback", "b_earnings_gap_drift", "c_insider_cluster", "d_base_breakout"
    }


# ---------------------------------------------------------------------------
# scan() == scan_history() day by day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "momentum_pullback", "earnings_gap_drift", "insider_cluster", "base_breakout"
])
def test_scan_matches_scan_history_day_by_day(pdata, name):
    start, end = date(2024, 11, 12), END
    mod = _module(name)
    hist = mod.scan_history(start, end, pdata)
    daily = []
    for d in trading_days(start, end):
        daily.extend(mod.scan(d, pdata))
    assert hist == daily
    assert len(hist) > 0 or name == "base_breakout"


def test_scan_matches_scan_history_over_a_long_range(pdata):
    """Whole-history agreement for A on a window with many signals."""
    mod = _module("momentum_pullback")
    start, end = date(2023, 3, 1), date(2023, 5, 31)
    hist = mod.scan_history(start, end, pdata)
    daily = [c for d in trading_days(start, end) for c in mod.scan(d, pdata)]
    assert len(hist) > 5
    assert hist == daily


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("as_of", [date(2024, 11, 18), date(2024, 12, 10), date(2024, 12, 13), date(2024, 12, 24)])
def test_scan_ignores_future_prices_and_filings(pdata, as_of):
    mods = get_enabled_modules(CFG)
    before = {m.name: m.scan(as_of, pdata) for m in mods}
    assert any(before.values())

    conn = pdata.conn
    rng = np.random.default_rng(7)
    rows = conn.execute(
        "SELECT ticker, date FROM prices WHERE date > ?", (as_of.isoformat(),)
    ).fetchall()
    for ticker, d in rows:
        k = float(rng.uniform(0.3, 3.0))
        conn.execute(
            "UPDATE prices SET open=open*?, high=high*?, low=low*?, close=close*?, "
            "volume=volume*? WHERE ticker=? AND date=?",
            (k, k, k, k, 5, ticker, d),
        )
    # future filings: an insider cluster for NEGC1 filed after as_of, and a future 8-K gap
    fut = "2024-12-30"
    for j in range(3):
        conn.execute(
            "INSERT INTO insider_trades (accession, row_num, ticker, insider_cik, insider_name, "
            "is_officer, is_director, is_ten_pct_owner, officer_title, trans_code, trans_date, "
            "shares, price, value, acquired_disposed, filed_date) VALUES "
            "(?,1,'NEGC1',?,?,1,0,0,'CEO','P',?,1,1,500000,'A',?)",
            (f"FUT-{j}", f"X{j}", f"Future {j}", "2024-12-27", fut),
        )
    conn.commit()

    after = {m.name: m.scan(as_of, pdata) for m in mods}
    assert after == before


def test_insider_cluster_not_visible_before_filing(pdata):
    """Trades are invisible until filed: scanning before MOMC1's second filing finds
    nothing even though the trade dates are earlier."""
    mod = _module("insider_cluster")
    filed = [r[0] for r in pdata.conn.execute(
        "SELECT filed_date FROM insider_trades WHERE ticker='MOMC1' ORDER BY filed_date"
    )]
    assert len(filed) == 2
    # the day before the later filing the cluster cannot exist, however long we look
    prev = date.fromisoformat(filed[1]).toordinal() - 1
    assert not [c for c in mod.scan_history(date(2024, 10, 1), date.fromordinal(prev), pdata)
                if c.ticker == "MOMC1"]


def test_ten_percent_owners_do_not_count(pdata):
    pdata.conn.execute("UPDATE insider_trades SET is_ten_pct_owner=1 WHERE ticker='MOMC1'")
    pdata.conn.commit()
    assert "MOMC1" not in _by_ticker(_hits("insider_cluster", pdata))


def test_small_trades_do_not_count(pdata):
    pdata.conn.execute("UPDATE insider_trades SET value=40000 WHERE ticker='MOMC1'")
    pdata.conn.commit()
    assert "MOMC1" not in _by_ticker(_hits("insider_cluster", pdata))
