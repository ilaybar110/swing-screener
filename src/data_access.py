"""DataAccess: thin, read-only SQLite query layer shared by every bot.

Point an instance at either data/run.db or data/research.db -- the queries are
identical either way. This file contains no business logic (no scoring, no
strategy rules, no ranking); it only fetches rows. All as_of / filed_as_of
parameters enforce point-in-time correctness: nothing dated/filed after that date
is ever returned.

See src/contracts.py for the StrategyModule interface and the dataclasses that
flow between modules; see docs/CONTRACTS.md for narrative documentation.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Optional

import pandas as pd


class DataAccess:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_prices(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Daily OHLCV + adj_close + price_source for one ticker, inclusive range,
        sorted by date ascending."""
        query = (
            "SELECT date, open, high, low, close, adj_close, volume, price_source "
            "FROM prices WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date"
        )
        return pd.read_sql_query(
            query, self.conn, params=(ticker, start.isoformat(), end.isoformat())
        )

    def get_splits(self, ticker: str) -> pd.DataFrame:
        """All recorded splits for one ticker, sorted by date ascending."""
        query = "SELECT date, ratio FROM splits WHERE ticker = ? ORDER BY date"
        return pd.read_sql_query(query, self.conn, params=(ticker,))

    def get_universe(self, as_of: date) -> pd.DataFrame:
        """The universe snapshot in effect on as_of (most recent snapshot with
        snapshot_date <= as_of)."""
        query = (
            "SELECT snapshot_date, ticker, price, market_cap, adv20 FROM universe_snapshots "
            "WHERE snapshot_date = ("
            "  SELECT MAX(snapshot_date) FROM universe_snapshots WHERE snapshot_date <= ?"
            ")"
        )
        return pd.read_sql_query(query, self.conn, params=(as_of.isoformat(),))

    def get_sector_etf(self, sector: str) -> Optional[str]:
        """Map a GICS sector name to its tracking ETF ticker (config-driven)."""
        from src.config import load_config

        cfg = load_config()
        return cfg.tracking.sector_etfs.get(sector)

    def get_regime(self, as_of: date) -> Optional[dict[str, Any]]:
        """Regime row in effect on as_of (most recent regime_log row <= as_of)."""
        query = (
            "SELECT date, spy_close, spy_sma200, breadth_pct, regime FROM regime_log "
            "WHERE date <= ? ORDER BY date DESC LIMIT 1"
        )
        row = self.conn.execute(query, (as_of.isoformat(),)).fetchone()
        return dict(row) if row else None

    def get_fundamentals(self, ticker: str, as_of: date) -> pd.DataFrame:
        """All fundamentals rows for ticker with filed_date <= as_of, one row per
        (metric, period_end) keeping only the latest filed_date (i.e. the most
        recently amended value known as of as_of)."""
        # Rows are stored under one ticker per CIK, so a second share class (GOOGL vs
        # GOOG) is matched through the shared CIK.
        query = (
            "SELECT * FROM fundamentals WHERE (ticker = ? OR cik = "
            "(SELECT cik FROM tickers WHERE ticker = ?)) AND filed_date <= ? "
            "ORDER BY metric, period_end, filed_date"
        )
        df = pd.read_sql_query(query, self.conn, params=(ticker, ticker, as_of.isoformat()))
        if df.empty:
            return df
        return df.groupby(["metric", "period_end"], as_index=False).last()

    def get_fundamentals_summary(self, ticker: str, as_of: date) -> Optional[dict[str, Any]]:
        """Most recent fundamentals_summary row for ticker with as_of column <= as_of."""
        query = (
            "SELECT * FROM fundamentals_summary WHERE ticker = ? AND as_of <= ? "
            "ORDER BY as_of DESC LIMIT 1"
        )
        row = self.conn.execute(query, (ticker, as_of.isoformat())).fetchone()
        return dict(row) if row else None

    def get_insider_trades(self, start: date, end: date, filed_as_of: date) -> pd.DataFrame:
        """Insider transactions with trans_date in [start, end] and filed_date <=
        filed_as_of (point-in-time: a trade isn't visible until its Form 4 is
        filed)."""
        query = (
            "SELECT * FROM insider_trades WHERE trans_date >= ? AND trans_date <= ? "
            "AND filed_date <= ? ORDER BY ticker, trans_date"
        )
        return pd.read_sql_query(
            query,
            self.conn,
            params=(start.isoformat(), end.isoformat(), filed_as_of.isoformat()),
        )

    def get_earnings_events(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Known earnings events (past, from 8-K 2.02 acceptance) for ticker in
        [start, end]."""
        query = (
            "SELECT * FROM earnings_dates WHERE ticker = ? AND event_date >= ? "
            "AND event_date <= ? ORDER BY event_date"
        )
        return pd.read_sql_query(
            query, self.conn, params=(ticker, start.isoformat(), end.isoformat())
        )

    def get_upcoming_earnings(self, ticker: str, as_of: date) -> Optional[dict[str, Any]]:
        """Next known earnings event for ticker with event_date >= as_of, if any."""
        query = (
            "SELECT * FROM earnings_dates WHERE ticker = ? AND event_date >= ? "
            "ORDER BY event_date ASC LIMIT 1"
        )
        row = self.conn.execute(query, (ticker, as_of.isoformat())).fetchone()
        return dict(row) if row else None
