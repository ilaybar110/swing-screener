"""Universe construction (docs/PLAN.md section 5).

refresh_universe() is the live, weekly job: pulls current listing/sector/market-cap
data via src/data/universe_source.py, applies the price/market-cap/ADV/history
filters and SPAC/fund/preferred/warrant exclusions using our own point-in-time price
history, and writes one row per surviving ticker into `universe_snapshots`.

build_historical_universe() is the backtest-only approximation of the same filters
using only what's in the DB already (no network calls) -- see its docstring for the
survivorship-bias caveat.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from src.config import Config, load_config
from src.data import universe_source
from src.data.edgar_client import EdgarClient
from src.data_access import DataAccess
from src.utils.logging import get_logger

log = get_logger(__name__)


def _is_excluded(name: Optional[str], sic: Optional[str], cfg: Config) -> bool:
    if sic is not None:
        try:
            if int(sic) in cfg.universe.exclude_sic_codes:
                return True
        except (TypeError, ValueError):
            pass
    if name:
        lowered = name.lower()
        for pattern in cfg.universe.exclude_name_patterns:
            if pattern.lower() in lowered:
                return True
    return False


def _fetch_universe_source(cfg: Config) -> tuple[pd.DataFrame, str]:
    edgar_client = EdgarClient(
        cfg.paths.raw_cache_dir,
        cfg.secrets.sec_email,
        cfg.edgar.max_requests_per_second,
    )
    return universe_source.get_universe_source(cfg.paths.raw_cache_dir, edgar_client)


def refresh_universe(conn: sqlite3.Connection, as_of: date) -> None:
    """Rebuild the universe snapshot for `as_of` per docs/PLAN.md section 5."""
    cfg = load_config()
    df, source = _fetch_universe_source(cfg)

    edgar_client = EdgarClient(
        cfg.paths.raw_cache_dir,
        cfg.secrets.sec_email,
        cfg.edgar.max_requests_per_second,
    )
    universe_source.upsert_tickers(conn, df, edgar_client)

    data = DataAccess(conn)
    lookback_start = as_of - timedelta(days=int(cfg.universe.min_history_days * 2.5) + 30)

    rows: list[tuple] = []
    for _, r in df.iterrows():
        ticker = str(r["ticker"]).upper()
        if _is_excluded(r.get("name"), r.get("sic"), cfg):
            continue

        prices = data.get_prices(ticker, lookback_start, as_of)
        if len(prices) < cfg.universe.min_history_days:
            continue

        price = float(prices["close"].iloc[-1])
        if price < cfg.universe.min_price:
            continue

        market_cap = r.get("market_cap")
        if market_cap is None or pd.isna(market_cap) or market_cap < cfg.universe.min_market_cap:
            continue

        adv20 = float((prices["close"] * prices["volume"]).tail(20).mean())
        if adv20 < cfg.universe.min_adv20_dollars:
            continue

        rows.append((as_of.isoformat(), ticker, price, float(market_cap), adv20))

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO universe_snapshots "
            "(snapshot_date, ticker, price, market_cap, adv20) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()

    log.info(
        "refresh_universe: as_of=%s source=%s candidates=%d survivors=%d",
        as_of, source, len(df), len(rows),
    )


def build_historical_universe(data: DataAccess, as_of: date) -> pd.DataFrame:
    """Point-in-time approximation of the universe as of `as_of`, for backtesting.

    Market cap is `shares outstanding (filed as of as_of) x price`, using the
    weekly fundamentals_summary snapshot in effect on as_of. When no summary is
    available yet for a ticker, falls back to that ticker's most recent known
    market cap (from any universe_snapshots row, regardless of date) and flags the
    row with market_cap_is_approx=True, since that fallback is not point-in-time.

    SURVIVORSHIP BIAS: this only considers tickers present in today's `tickers`
    table. Any ticker that was delisted, acquired, or otherwise dropped before the
    backtest was run never appears here, even if it would have qualified for the
    universe on `as_of`. Backtest results built on this approximation are therefore
    an upper bound on what a point-in-time universe would have produced -- see
    docs/PLAN.md section 15.
    """
    cfg = load_config()
    conn = data.conn
    lookback_start = as_of - timedelta(days=int(cfg.universe.min_history_days * 2.5) + 30)

    tickers = [row[0] for row in conn.execute("SELECT ticker FROM tickers").fetchall()]

    records: list[dict[str, Any]] = []
    for ticker in tickers:
        prices = data.get_prices(ticker, lookback_start, as_of)
        if len(prices) < cfg.universe.min_history_days:
            continue

        price = float(prices["close"].iloc[-1])
        if price < cfg.universe.min_price:
            continue

        adv20 = float((prices["close"] * prices["volume"]).tail(20).mean())
        if adv20 < cfg.universe.min_adv20_dollars:
            continue

        summary = data.get_fundamentals_summary(ticker, as_of)
        market_cap: Optional[float] = None
        market_cap_is_approx = False
        if summary and summary.get("shares"):
            market_cap = float(summary["shares"]) * price
        else:
            fallback = conn.execute(
                "SELECT market_cap FROM universe_snapshots WHERE ticker = ? "
                "AND market_cap IS NOT NULL ORDER BY snapshot_date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if fallback:
                market_cap = float(fallback["market_cap"])
                market_cap_is_approx = True

        if market_cap is None or market_cap < cfg.universe.min_market_cap:
            continue

        ticker_row = conn.execute(
            "SELECT name, sic FROM tickers WHERE ticker = ?", (ticker,)
        ).fetchone()
        name = ticker_row["name"] if ticker_row else None
        sic = ticker_row["sic"] if ticker_row else None
        if _is_excluded(name, sic, cfg):
            continue

        records.append({
            "ticker": ticker,
            "as_of": as_of.isoformat(),
            "price": price,
            "market_cap": market_cap,
            "market_cap_is_approx": market_cap_is_approx,
            "adv20": adv20,
        })

    return pd.DataFrame(
        records,
        columns=["ticker", "as_of", "price", "market_cap", "market_cap_is_approx", "adv20"],
    )
