"""Indicator unit tests (Bot 4)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind


def _df(n=40):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(np.arange(100.0, 100.0 + n), index=idx)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0}
    )


def test_sma_and_nan_warmup():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ind.sma(s, 3)
    assert out.isna().tolist() == [True, True, False, False, False]
    assert out.iloc[-1] == 4.0


def test_atr_constant_range():
    df = _df()
    a = ind.atr(df, 14)
    assert a.iloc[:13].isna().all()
    assert a.iloc[-1] == 2.0  # high-low = 2, gaps vs prior close <= 2


def test_atr_is_independent_of_series_start():
    df = _df(80)
    assert ind.atr(df, 14).iloc[-1] == ind.atr(df.iloc[30:], 14).iloc[-1]


def test_high_52w_includes_today():
    df = _df(300)
    assert ind.high_52w(df).iloc[-1] == df["high"].iloc[-252:].max()


def test_rs_raw_formula():
    idx = pd.date_range("2024-01-01", periods=200, freq="B")
    stock = pd.Series(np.linspace(100, 200, 200), index=idx)
    spy = pd.Series(np.linspace(100, 120, 200), index=idx)
    rs = ind.rs_raw(stock, spy, 126, 21)
    t = 199
    expected = (stock.iloc[t - 21] / stock.iloc[t - 126]) / (spy.iloc[t - 21] / spy.iloc[t - 126]) - 1
    assert rs.iloc[t] == expected
    assert rs.iloc[:126].isna().all()


def test_rs_percentile_within_membership():
    idx = pd.date_range("2024-01-01", periods=1, freq="B")
    rs = pd.DataFrame({"A": [0.1], "B": [0.2], "C": [0.3], "D": [0.4], "OUT": [9.0]}, index=idx)
    member = pd.DataFrame({"A": [True], "B": [True], "C": [True], "D": [True], "OUT": [False]}, index=idx)
    pct = ind.rs_percentile(rs, member).iloc[0]
    assert pct["D"] == 100.0 and pct["A"] == 25.0
    assert np.isnan(pct["OUT"])


def test_stop_distance_rule():
    assert ind.stop_distance_ok(100, 95, 0.02, 0.12)
    assert not ind.stop_distance_ok(100, 99, 0.02, 0.12)   # < 2%
    assert not ind.stop_distance_ok(100, 85, 0.02, 0.12)   # > 12%
    assert not ind.stop_distance_ok(100, 101, 0.02, 0.12)  # stop above entry
