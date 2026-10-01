"""Technical indicators shared by the strategy modules (owned by Bot 4).

Every function here is *causal*: the value at row ``t`` depends only on rows ``<= t``.
That property is what makes the modules point-in-time safe and what lets
``scan_history`` (computed once over a whole range) agree exactly with ``scan``
(computed on data truncated at ``as_of``). Rolling windows use ``min_periods`` equal to
the window so a value is either fully supported by history or NaN; ATR uses a simple
rolling mean of true range (not Wilder smoothing) because Wilder's recursion depends on
where the series starts, which would make results depend on the download window.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from src.utils.logging import get_logger

log = get_logger(__name__)

# Calendar-day buffer loaded before the first evaluation date. ~600 calendar days is
# ~410 sessions, comfortably more than the longest lookback used (252-session 52w high).
LOOKBACK_CALENDAR_DAYS = 600
TRADING_DAYS_52W = 252
BENCHMARK = "SPY"


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average over n sessions (NaN until n observations exist)."""
    return s.rolling(n, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    parts = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return parts.max(axis=1, skipna=False)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average true range: rolling mean of true range over n sessions."""
    return true_range(df).rolling(n, min_periods=n).mean()


def high_52w(df: pd.DataFrame, n: int = TRADING_DAYS_52W) -> pd.Series:
    """Rolling 52-week (n-session) high of daily highs, including the current session."""
    return df["high"].rolling(n, min_periods=n).max()


def avg_volume(df: pd.DataFrame, n: int) -> pd.Series:
    """Average daily volume over the n sessions ending on each row (inclusive)."""
    return df["volume"].rolling(n, min_periods=n).mean()


def rs_raw(close: pd.Series, spy_close: pd.Series, lookback: int = 126, skip: int = 21) -> pd.Series:
    """Relative strength vs SPY, skipping the most recent ``skip`` sessions:
    ``(close[t-skip]/close[t-lookback]) / (spy[t-skip]/spy[t-lookback]) - 1``."""
    spy = spy_close.reindex(close.index)
    stock_ret = close.shift(skip) / close.shift(lookback)
    spy_ret = spy.shift(skip) / spy.shift(lookback)
    return stock_ret / spy_ret - 1.0


def rs_percentile(rs: pd.DataFrame, member: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cross-sectional percentile (0-100, top = 100) of ``rs`` (dates x tickers) within
    each date's universe. ``member`` (same shape, bool) restricts the ranking to the
    tickers in that date's universe; NaN RS values are excluded from the ranking."""
    masked = rs if member is None else rs.where(member)
    return masked.rank(axis=1, pct=True, method="average") * 100.0


def sliding(a: np.ndarray, window: int) -> np.ndarray:
    """Rolling windows of ``a`` as a (len(a)-window+1, window) view; row i covers
    ``a[i:i+window]``."""
    return np.lib.stride_tricks.sliding_window_view(a, window)


# ---------------------------------------------------------------------------
# Price loading
# ---------------------------------------------------------------------------


def load_prices(data, ticker: str, start: date, end: date) -> pd.DataFrame:
    """OHLCV for one ticker indexed by ``datetime.date``-valued DatetimeIndex, only
    rows with a complete OHLCV bar, never beyond ``end``."""
    df = data.get_prices(ticker, start, end)
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.dropna()


def load_panel(
    data, tickers: Iterable[str], start: date, end: date, buffer_days: int = LOOKBACK_CALENDAR_DAYS
) -> dict[str, pd.DataFrame]:
    """Price frames for ``tickers`` over [start - buffer, end], each reindexed to the
    benchmark's session calendar so that positional shifts mean the same thing for
    every ticker. Missing sessions become NaN rows (and therefore NaN indicators)."""
    lo = start - timedelta(days=buffer_days)
    spy = load_prices(data, BENCHMARK, lo, end)
    out: dict[str, pd.DataFrame] = {}
    if spy.empty:
        return out
    cal = spy.index
    out[BENCHMARK] = spy
    for t in tickers:
        if t == BENCHMARK:
            continue
        df = load_prices(data, t, lo, end)
        if df.empty:
            continue
        out[t] = df.reindex(cal)
    return out


def universe_membership(
    data, dates: pd.DatetimeIndex
) -> dict[pd.Timestamp, frozenset[str]]:
    """Universe (set of tickers) in effect on each date, via ``get_universe``.
    Snapshots are weekly, so only distinct snapshots are queried."""
    if len(dates) == 0:
        return {}
    snaps = [
        r[0]
        for r in data.conn.execute(
            "SELECT DISTINCT snapshot_date FROM universe_snapshots "
            "WHERE snapshot_date <= ? ORDER BY snapshot_date",
            (dates[-1].date().isoformat(),),
        ).fetchall()
    ]
    snap_sets: dict[str, frozenset[str]] = {}
    out: dict[pd.Timestamp, frozenset[str]] = {}
    for d in dates:
        eff = [s for s in snaps if s <= d.date().isoformat()]
        if not eff:
            out[d] = frozenset()
            continue
        key = eff[-1]
        if key not in snap_sets:
            u = data.get_universe(date.fromisoformat(key))
            snap_sets[key] = frozenset(u["ticker"].tolist())
        out[d] = snap_sets[key]
    return out


def membership_frame(
    member_by_date: dict[pd.Timestamp, frozenset[str]], index: pd.DatetimeIndex, tickers: list[str]
) -> pd.DataFrame:
    """Boolean dates x tickers frame of universe membership."""
    m = pd.DataFrame(False, index=index, columns=tickers)
    for d in index:
        s = member_by_date.get(d, frozenset())
        cols = [t for t in tickers if t in s]
        if cols:
            m.loc[d, cols] = True
    return m


def rs_percentile_panel(
    panel: dict[str, pd.DataFrame],
    member: pd.DataFrame,
    lookback: int = 126,
    skip: int = 21,
) -> pd.DataFrame:
    """RS percentile (0-100) for every ticker in ``panel`` on every date, ranked within
    that date's universe (``member``)."""
    spy = panel[BENCHMARK]["close"]
    raw = pd.DataFrame(
        {t: rs_raw(df["close"], spy, lookback, skip) for t, df in panel.items() if t != BENCHMARK}
    )
    raw = raw.reindex(columns=member.columns)
    return rs_percentile(raw, member)


def stop_distance_ok(entry: float, stop: float, lo: float, hi: float) -> bool:
    """Common rule: skip when the stop is not below entry or its distance is outside
    [lo, hi] of entry."""
    if not (np.isfinite(entry) and np.isfinite(stop)) or stop >= entry or entry <= 0:
        return False
    d = (entry - stop) / entry
    return lo <= d <= hi


def clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))
