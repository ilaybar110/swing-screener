"""Data side of the recommendation-detail chart (no Streamlit imports: unit-testable).

* ``module_details``      the per-module ``details`` stored on a recommendation.
* ``ensure_annotations``  returns the details needed to draw a module's annotations: the
                          stored ones when the recommendation already carries them,
                          otherwise recomputed by running the module's own ``scan_ticker``
                          on prices up to ``signal_date`` only (point-in-time).
* ``outcome_events``      entry fill / 2R partial / final exit / expiry markers, replayed
                          with the real ``tracker.simulate`` on the fetched prices.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from src import indicators as ind
from src.config import Config
from src.data_access import DataAccess
from src.modules import MODULE_CLASSES
from src.modules.base import ScanContext
from src.tracker import row_to_rec, simulate

MODULE_KEYS = {cls.name: key for key, cls in MODULE_CLASSES.items()}
# One field per module that only annotation-aware details carry.
MARKER_FIELD = {
    "momentum_pullback": "swing_high_date",
    "earnings_gap_drift": "consolidation_start",
    "insider_cluster": "trades",
    "base_breakout": "base_start",
}
HISTORY_DAYS = 620  # calendar days of history before the signal (52w high needs ~252 sessions)


def module_details(row: pd.Series, module: str) -> dict[str, Any]:
    det = row.get("details") or {}
    by = (det.get("by_module") or {}).get(module) or {}
    found = by.get("details")
    if found is None and module == row.get("primary_module"):
        found = det.get("primary")
    return dict(found or {})


def has_annotations(module: str, details: dict[str, Any]) -> bool:
    return MARKER_FIELD[module] in details


def fetch_ohlcv(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Split-adjusted OHLCV from yfinance indexed by session date. Raises on failure."""
    import yfinance as yf

    df = yf.Ticker(ticker).history(
        start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"no price data returned for {ticker}")
    df = df.rename(columns=str.lower)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df[["open", "high", "low", "close", "volume"]].astype(float).dropna()


def recompute_details(
    module: str, ticker: str, signal: date, prices: pd.DataFrame, spy: pd.DataFrame,
    stored: dict[str, Any], data: DataAccess, config: Config,
) -> Optional[dict[str, Any]]:
    """Run ``module.scan_ticker`` on ``prices`` truncated at ``signal`` (never later) and
    return the details of the candidate it emits for ``signal``; None if it does not."""
    cutoff = pd.Timestamp(signal)
    df = prices[prices.index <= cutoff]
    bench = spy[spy.index <= cutoff] if len(spy) else spy
    if df.empty or df.index[-1] != cutoff:
        return None
    mod = MODULE_CLASSES[MODULE_KEYS[module]](config)
    rs = pd.DataFrame(index=df.index)
    if mod.needs_rs:
        rs[ticker] = float("nan")
        rs.loc[cutoff, ticker] = float(stored.get("rs_pct", float("nan")))
    ctx = ScanContext(
        data=data, eval_dates=pd.DatetimeIndex([cutoff]), rs_pct=rs, start=signal, end=signal,
        spy=bench,
    )
    mod.prepare([ticker], ctx)
    for cand in mod.scan_ticker(ticker, df, ctx):
        if cand.signal_date == signal and cand.ticker == ticker:
            return dict(cand.details)
    return None


def ensure_annotations(
    module: str, row: pd.Series, prices: pd.DataFrame, spy: pd.DataFrame, data: DataAccess,
    config: Config, force_recompute: bool = False,
) -> tuple[dict[str, Any], str]:
    """(details, status) with status "stored", "recomputed" or "unavailable".
    ``force_recompute``: stored price levels are in pre-split units, so when the chart's
    prices are split-adjusted the annotations must be recomputed on those prices."""
    stored = module_details(row, module)
    if has_annotations(module, stored) and not force_recompute:
        return stored, "stored"
    signal = date.fromisoformat(str(row["signal_date"])[:10])
    got = recompute_details(module, row["ticker"], signal, prices, spy, stored, data, config)
    if got is None:
        return ({} if force_recompute else stored), "unavailable"
    return {**stored, **got}, "recomputed"


def outcome_events(row: pd.Series, prices: pd.DataFrame, factor: float, config: Config) -> dict[str, Any]:
    """Replay the trade on ``prices`` with the real simulator. Returns a dict with
    ``status`` plus any of entry/partial/exit/expired markers (date, price, text)."""
    bars = prices.reset_index().rename(columns={prices.index.name or "index": "date"})
    bars = bars[["date", "open", "high", "low", "close"]]
    rec = row_to_rec({**row.to_dict(), "entry": row["entry"] / factor, "stop": row["stop"] / factor,
                      "target": row["target"] / factor})
    res = simulate(rec, bars, None, config)
    out: dict[str, Any] = {"status": res.status, "result": res}
    if res.status == "expired":
        why = {"entry_window_elapsed": "the buy-stop was never triggered within its validity window",
               "setup_broken": "price closed below the stop before the entry triggered"}
        out["expired"] = (pd.Timestamp(res.exit_date), why.get(res.exit_reason, res.exit_reason))
        return out
    if res.entry_date is None:
        return out
    out["entry"] = (pd.Timestamp(res.entry_date), res.entry_fill)
    target = rec.target
    end = pd.Timestamp(res.exit_date) if res.exit_date else bars["date"].max()
    window = bars[(bars["date"] >= pd.Timestamp(res.entry_date)) & (bars["date"] <= end)]
    hit = window[window["high"] >= target]
    partial_ok = res.exit_reason not in ("stop",)
    if partial_ok and not hit.empty:
        out["partial"] = (pd.Timestamp(hit["date"].iloc[0]), target)
    if res.exit_date is not None:
        frac = config.trade_plan.partial_exit_fraction
        px = res.avg_exit_price
        if "partial" in out and frac < 1:
            px = (res.avg_exit_price - frac * target) / (1 - frac)
        out["exit"] = (pd.Timestamp(res.exit_date), px, res.exit_reason)
    return out


def sma_series(prices: pd.DataFrame, n: int) -> pd.Series:
    return ind.sma(prices["close"], n)
