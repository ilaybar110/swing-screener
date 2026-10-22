"""Module quality filters and the insider-cluster completion rule (Bot 4).

* Module A: volume dry-up (< max_pullback_volume_ratio), pullback depth cap
  (<= max_pullback_depth_atr) and trigger-day close location (>= min_close_location),
  on textbook synthetic series plus a property check against the fixture.
* Module D: trend template (close > SMA150 and SMA200, SMA200 rising) and base depth cap.
* Module C: a cluster completion is not suppressed by an old cluster, continuation filings
  do not re-fire, and a live scan with only ~120 days of filings agrees with scan_history.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.data_access import DataAccess
from src.modules.a_momentum_pullback import MomentumPullback
from src.modules.base import ScanContext
from src.modules.c_insider_cluster import InsiderCluster
from src.modules.d_base_breakout import BaseBreakout
from src.utils.calendar import trading_days
from tests.test_modules_scan import _patch_gap_opens


@pytest.fixture()
def pdata(writable_fixture_conn) -> DataAccess:
    _patch_gap_opens(writable_fixture_conn)
    return DataAccess(writable_fixture_conn)


def _ctx(df: pd.DataFrame, rs: float = 95.0) -> ScanContext:
    last = df.index[-1]
    return ScanContext(data=None, eval_dates=df.index[-1:], rs_pct=pd.DataFrame({"X": rs}, index=df.index),
                       start=last.date(), end=last.date())


# ---------------------------------------------------------------------------
# Module A
# ---------------------------------------------------------------------------


def _textbook_pullback(n=300, pull_vol=0.6, pull_days=4, pull_pct=0.012, close_loc=0.9) -> pd.DataFrame:
    """Steady 0.4%/day leader, a ``pull_days`` pullback of ``pull_pct`` per day on
    ``pull_vol`` x normal volume, then a trigger day above the prior high whose close sits
    at ``close_loc`` of its range."""
    idx = pd.bdate_range("2023-01-02", periods=n)
    close = 50 * np.cumprod(np.full(n, 1.004))
    vol = np.full(n, 1e6)
    t = n - 1
    for k in range(pull_days):
        close[t - pull_days + k] = close[t - pull_days - 1] * (1 - pull_pct * (k + 1))
        vol[t - pull_days + k] = 1e6 * pull_vol
    high, low = close * 1.01, close * 0.99
    lo_t, hi_t = high[t - 1] * 0.995, high[t - 1] * 1.02
    close[t] = lo_t + close_loc * (hi_t - lo_t)
    high[t], low[t] = hi_t, lo_t
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def _a(**overrides) -> MomentumPullback:
    cfg = load_config()
    for k, v in overrides.items():
        setattr(cfg.modules.a_momentum_pullback, k, v)
    return MomentumPullback(cfg)


def test_module_a_fires_on_a_textbook_pullback():
    df = _textbook_pullback()
    (c,) = _a().scan_ticker("X", df, _ctx(df))
    d = c.details
    assert c.signal_date == df.index[-1].date()
    assert d["pullback_volume_ratio"] < 0.8
    assert d["pullback_depth_atr"] <= 3.5
    assert d["close_location"] == pytest.approx(0.9, abs=1e-3)
    # ranking's RS-free quality: the same dry-up/depth components without the RS term
    assert d["setup_score_ex_rs"] == pytest.approx((c.setup_score - 40 * d["rs_pct"] / 100) / 0.6, abs=0.05)


@pytest.mark.parametrize("shape,relax", [
    ({"pull_vol": 0.9}, {"max_pullback_volume_ratio": 1.0}),        # volume barely below average
    ({"pull_pct": 0.02}, {"max_pullback_depth_atr": float("inf")}),  # ~4.3 ATR: too deep
    ({"close_loc": 0.4}, {"min_close_location": 0.0}),               # weak close on the trigger day
])
def test_module_a_quality_rules_reject_and_only_that_rule_does(shape, relax):
    df = _textbook_pullback(**shape)
    assert _a().scan_ticker("X", df, _ctx(df)) == []
    assert len(_a(**relax).scan_ticker("X", df, _ctx(df))) == 1


def test_module_a_rules_are_exactly_the_documented_thresholds(pdata):
    """On the fixture, shipped candidates == relaxed candidates that satisfy the three rules."""
    start, end = date(2023, 1, 1), date(2024, 12, 31)
    c = load_config().modules.a_momentum_pullback
    relaxed = _a(max_pullback_volume_ratio=1.0, max_pullback_depth_atr=float("inf"),
                 min_close_location=0.0).scan_history(start, end, pdata)
    assert len(relaxed) > 20
    expected = [
        (x.ticker, x.signal_date) for x in relaxed
        if x.details["pullback_volume_ratio"] < c.max_pullback_volume_ratio
        and x.details["pullback_depth_atr"] <= c.max_pullback_depth_atr
        and x.details["close_location"] >= c.min_close_location
    ]
    assert [(x.ticker, x.signal_date) for x in _a().scan_history(start, end, pdata)] == expected


# ---------------------------------------------------------------------------
# Module D
# ---------------------------------------------------------------------------


def _d(**overrides) -> BaseBreakout:
    cfg = load_config()
    for k, v in overrides.items():
        setattr(cfg.modules.d_base_breakout, k, v)
    return BaseBreakout(cfg)


def test_module_d_trend_template_and_base_depth_on_the_fixture(pdata):
    start, end = date(2022, 6, 1), date(2024, 12, 31)
    loose = _d(trend_template=False, max_base_depth_pct=1.0).scan_history(start, end, pdata)
    shipped = _d().scan_history(start, end, pdata)
    assert any(x.ticker == "MOMD1" and x.signal_date == date(2024, 12, 24) for x in shipped)
    for x in shipped:
        d = x.details
        assert d["trigger_close"] > d["sma150"] and d["trigger_close"] > d["sma200"]
        assert d["sma200"] > d["sma200_slope_ref"]
        assert d["base_depth_pct"] <= 0.25
    assert {(x.ticker, x.signal_date) for x in shipped} <= {(x.ticker, x.signal_date) for x in loose}


def _base_then_breakout(n=320, trend=0.002, base_len=30, base_depth=0.05) -> pd.DataFrame:
    """Uptrend (``trend``/day, 3% ranges), then a ``base_len`` base whose ranges contract
    and whose lows sit ``base_depth`` under its high, then a 2x-volume breakout."""
    idx = pd.bdate_range("2022-01-03", periods=n)
    close = 40 * np.cumprod(np.full(n, 1 + trend))
    rng = np.full(n, 0.03)
    vol = np.full(n, 1e6)
    t = n - 1
    b0 = t - base_len
    top = close[b0 - 1]
    close[b0:t] = top * 0.98
    rng[b0:t] = np.linspace(0.03, 0.006, base_len)
    high, low = close * (1 + rng / 2), close * (1 - rng / 2)
    high[b0:t] = np.minimum(high[b0:t], top)
    # the base's low, inside every candidate base length (>= 15) yet outside the ATR14
    # window that measures the contraction
    low[t - 15] = top * (1 - base_depth)
    close[t] = top * 1.03
    high[t], low[t] = close[t] * 1.005, top * 0.995
    vol[t] = 2.5e6
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def _dctx(df):
    last = df.index[-1]
    return ScanContext(data=None, eval_dates=df.index[-1:], rs_pct=pd.DataFrame(index=df.index),
                       start=last.date(), end=last.date())


def test_module_d_fires_on_a_textbook_base_and_rejects_a_deep_one():
    ok = _base_then_breakout()
    (c,) = _d().scan_ticker("X", ok, _dctx(ok))
    assert c.details["base_depth_pct"] <= 0.25
    deep = _base_then_breakout(base_depth=0.30)
    assert _d().scan_ticker("X", deep, _dctx(deep)) == []
    assert len(_d(max_base_depth_pct=1.0).scan_ticker("X", deep, _dctx(deep))) == 1


def test_module_d_rejects_a_breakout_in_a_downtrend():
    down = _base_then_breakout(trend=-0.002)  # below a falling SMA200
    assert _d().scan_ticker("X", down, _dctx(down)) == []
    # without the trend template the same chart is a (52-week-high-limited) candidate or not,
    # but never because of the trend: check the template is what rejects it when the rest passes
    flat_hi = down.copy()
    hi = flat_hi["high"].iloc[-1]
    flat_hi.loc[flat_hi.index[:-1], ["high"]] = np.minimum(flat_hi["high"].iloc[:-1], hi * 0.99)
    flat_hi["open"] = flat_hi[["open", "high"]].min(axis=1)
    flat_hi["close"] = flat_hi[["close", "high"]].min(axis=1)
    flat_hi["low"] = flat_hi[["low", "close", "open"]].min(axis=1)
    assert _d().scan_ticker("X", flat_hi, _dctx(flat_hi)) == []
    assert len(_d(trend_template=False).scan_ticker("X", flat_hi, _dctx(flat_hi))) == 1


def test_module_d_needs_the_long_sma_history():
    short = _base_then_breakout(n=200)  # < 200 + 20 sessions: SMA200 slope unknown
    assert _d().scan_ticker("X", short, _dctx(short)) == []


# ---------------------------------------------------------------------------
# Module C
# ---------------------------------------------------------------------------


def _insert_trade(conn, ticker, insider, trans, filed, value=150_000.0, tag=""):
    conn.execute(
        "INSERT INTO insider_trades (accession, row_num, ticker, insider_cik, insider_name, is_officer, "
        "is_director, is_ten_pct_owner, officer_title, trans_code, trans_date, shares, price, value, "
        "acquired_disposed, filed_date) VALUES (?,1,?,?,?,1,0,0,'Chief Financial Officer','P',?,10000,15,?,'A',?)",
        (f"T-{ticker}-{insider}-{trans}{tag}", ticker, f"CIK_{insider}", f"Officer {insider} of {ticker}",
         trans, value, filed),
    )
    conn.commit()


def _completions(data, ticker, as_of=date(2024, 12, 31)) -> list[date]:
    mod = InsiderCluster(load_config())
    days = pd.DatetimeIndex([pd.Timestamp(d) for d in trading_days(date(2022, 6, 1), as_of)])
    mod.prepare([ticker], ScanContext(data=data, eval_dates=days, rs_pct=pd.DataFrame(),
                                      start=days[0].date(), end=as_of))
    return [F.date() for F, _ in mod._completions(ticker)]


def _c_hits(data, ticker, start=date(2023, 6, 1), end=date(2024, 12, 31)):
    return [c.signal_date for c in InsiderCluster(load_config()).scan_history(start, end, data) if c.ticker == ticker]


def test_two_clusters_a_year_apart_both_fire(pdata):
    """Regression: an older qualifying cluster used to block every later one forever."""
    conn = pdata.conn
    _insert_trade(conn, "MOMC1", "A", "2023-11-06", "2023-11-08")
    _insert_trade(conn, "MOMC1", "B", "2023-11-08", "2023-11-10")
    assert _completions(pdata, "MOMC1") == [date(2023, 11, 10), date(2024, 11, 16)]
    hits = _c_hits(pdata, "MOMC1")
    assert date(2024, 11, 18) in hits
    assert len(hits) == 2 and hits[0] < date(2024, 1, 1)


def test_continuation_filings_do_not_refire(pdata):
    conn = pdata.conn
    _insert_trade(conn, "MOMC1", "C", "2024-11-20", "2024-11-22")    # tops up the cluster
    _insert_trade(conn, "MOMC1", "D", "2024-12-10", "2024-12-12")    # a second pair 4 weeks on
    _insert_trade(conn, "MOMC1", "E", "2024-12-12", "2024-12-16")
    assert _completions(pdata, "MOMC1") == [date(2024, 11, 16)]
    assert _c_hits(pdata, "MOMC1") == [date(2024, 11, 18)]
    # without the November cluster the December pair is a cluster of its own
    conn.execute("DELETE FROM insider_trades WHERE ticker='MOMC1' AND trans_date < '2024-11-20'")
    conn.commit()
    assert _completions(pdata, "MOMC1") == [date(2024, 12, 12)]  # C (11-20) + D (12-10)


def test_cluster_after_the_cooldown_fires_again(pdata):
    conn = pdata.conn
    cool = load_config().modules.c_insider_cluster.cooldown_days
    first = date(2024, 11, 11)  # MOMC1's first trade
    a = first + timedelta(days=cool + 1)
    _insert_trade(conn, "MOMC1", "F", a.isoformat(), (a + timedelta(days=2)).isoformat())
    _insert_trade(conn, "MOMC1", "G", (a + timedelta(days=1)).isoformat(), (a + timedelta(days=3)).isoformat())
    assert _completions(pdata, "MOMC1", as_of=date(2025, 6, 30)) == [date(2024, 11, 16), a + timedelta(days=3)]


def test_live_scan_with_120_days_of_filings_matches_history(pdata, tmp_path):
    """Live runs only carry ~120 days of insider filings in state/; scan_history over a long
    range must still agree with them on every trigger day."""
    import shutil
    import sqlite3

    conn = pdata.conn
    _insert_trade(conn, "MOMC1", "A", "2023-11-06", "2023-11-08")
    _insert_trade(conn, "MOMC1", "B", "2023-11-08", "2023-11-10")
    _insert_trade(conn, "MOMC1", "C", "2024-08-01", "2024-08-05")  # lone buy inside the cooldown span
    hist = InsiderCluster(load_config()).scan_history(date(2023, 6, 1), date(2024, 12, 31), pdata)
    days = sorted({c.signal_date for c in hist} | {date(2024, 11, 18), date(2024, 11, 19)})
    assert date(2024, 11, 18) in {c.signal_date for c in hist}
    for d in days:
        live_path = tmp_path / f"live_{d}.db"
        shutil.copy(pdata.conn.execute("PRAGMA database_list").fetchone()[2], live_path)
        live = sqlite3.connect(live_path)
        live.row_factory = sqlite3.Row
        live.execute("DELETE FROM insider_trades WHERE filed_date < ?", ((d - timedelta(days=120)).isoformat(),))
        live.commit()
        got = InsiderCluster(load_config()).scan(d, DataAccess(live))
        live.close()
        assert got == [c for c in hist if c.signal_date == d], d
