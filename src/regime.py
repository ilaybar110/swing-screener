"""Market regime computation (docs/PLAN.md section 6).

Regime is derived daily from two independent conditions:
  - SPY trading above its own `regime.spy_sma_days`-day SMA.
  - Breadth: the percentage of the current universe trading above its own
    `regime.breadth_sma_days`-day SMA, with an "ok" threshold of ``regime.breadth_threshold_pct`` (config.yaml, 50).

Favorable = both conditions ok, Unfavorable = both fail, Caution = exactly one.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from src.config import load_config
from src.data_access import DataAccess
from src.utils.calendar import trading_days
from src.utils.logging import get_logger

log = get_logger(__name__)


def _lookback_start(as_of: date, sma_days: int) -> date:
    # Generous calendar-day lookback so the SMA window always has sma_days of
    # trading history available (weekends/holidays inflate calendar vs trading days).
    return as_of - timedelta(days=int(sma_days * 2.5) + 30)


def compute_regime(conn: sqlite3.Connection, as_of: date) -> dict[str, Any]:
    """Compute regime for `as_of`, upsert into `regime_log`, and return the row."""
    cfg = load_config()
    data = DataAccess(conn)

    spy_sma_days = cfg.regime.spy_sma_days
    breadth_sma_days = cfg.regime.breadth_sma_days

    spy_prices = data.get_prices("SPY", _lookback_start(as_of, spy_sma_days), as_of)
    if spy_prices.empty:
        raise ValueError(f"no SPY price history available as of {as_of}")

    spy_close = float(spy_prices["close"].iloc[-1])
    spy_sma200 = float(spy_prices["close"].tail(spy_sma_days).mean())
    spy_ok = spy_close > spy_sma200

    universe = data.get_universe(as_of)
    tickers = universe["ticker"].tolist() if not universe.empty else []

    above = 0
    counted = 0
    for ticker in tickers:
        prices = data.get_prices(ticker, _lookback_start(as_of, breadth_sma_days), as_of)
        if prices.empty:
            continue
        counted += 1
        close = float(prices["close"].iloc[-1])
        sma = float(prices["close"].tail(breadth_sma_days).mean())
        if close > sma:
            above += 1
    breadth_pct = 100.0 * above / counted if counted else 0.0
    breadth_ok = breadth_pct >= cfg.regime.breadth_threshold_pct

    if spy_ok and breadth_ok:
        regime_label = "Favorable"
    elif spy_ok or breadth_ok:
        regime_label = "Caution"
    else:
        regime_label = "Unfavorable"

    row = {
        "date": as_of.isoformat(),
        "spy_close": spy_close,
        "spy_sma200": spy_sma200,
        "breadth_pct": breadth_pct,
        "regime": regime_label,
    }
    conn.execute(
        "INSERT OR REPLACE INTO regime_log (date, spy_close, spy_sma200, breadth_pct, regime) "
        "VALUES (:date, :spy_close, :spy_sma200, :breadth_pct, :regime)",
        row,
    )
    conn.commit()
    log.info(
        "compute_regime: as_of=%s spy_close=%.2f spy_sma%s=%.2f breadth=%.1f%% (%d/%d) -> %s",
        as_of, spy_close, spy_sma_days, spy_sma200, breadth_pct, above, counted, regime_label,
    )
    return row


def backfill_regime(conn: sqlite3.Connection, start: date, end: date) -> list[dict[str, Any]]:
    """Compute and store regime for every NYSE trading day in [start, end]."""
    rows = [compute_regime(conn, day) for day in trading_days(start, end)]
    return rows
