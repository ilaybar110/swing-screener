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

    def _earnings_has(self, column: str) -> bool:
        """Whether earnings_dates carries ``column`` (the additive columns of
        src/db.py EARNINGS_EXTRA_COLUMNS are missing from DBs built before them)."""
        cols = getattr(self, "_earnings_cols", None)
        if cols is None:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(earnings_dates)").fetchall()}
            self._earnings_cols = cols
        return column in cols

    def _earnings_known_clause(self, known_as_of: Optional[date]) -> tuple[str, tuple]:
        """SQL filter keeping only earnings rows knowable on ``known_as_of``: an 8-K row
        from its acceptance date, a calendar row from its ``known_date`` (rows without
        either, e.g. legacy imports, count as known)."""
        if known_as_of is None:
            return "", ()
        iso = known_as_of.isoformat()
        clause = " AND (acceptance_datetime IS NULL OR substr(acceptance_datetime, 1, 10) <= ?)"
        params: tuple = (iso,)
        if self._earnings_has("known_date"):
            clause += " AND (known_date IS NULL OR known_date <= ?)"
            params += (iso,)
        return clause, params

    def get_earnings_events(
        self, ticker: str, start: date, end: date, known_as_of: Optional[date] = None
    ) -> pd.DataFrame:
        """Known earnings events for ticker with event_date in [start, end] (8-K 2.02
        rows and calendar rows). With ``known_as_of``, only rows already knowable on
        that date are returned (see ``_earnings_known_clause``)."""
        clause, extra = self._earnings_known_clause(known_as_of)
        query = (
            "SELECT * FROM earnings_dates WHERE ticker = ? AND event_date >= ? "
            "AND event_date <= ?" + clause + " ORDER BY event_date"
        )
        return pd.read_sql_query(
            query, self.conn, params=(ticker, start.isoformat(), end.isoformat()) + extra
        )

    def get_upcoming_earnings(self, ticker: str, as_of: date) -> Optional[dict[str, Any]]:
        """Next earnings event for ticker with event_date >= as_of that was already
        known on as_of (point-in-time: a calendar row from its ``known_date``, an 8-K row
        from its acceptance date)."""
        clause, extra = self._earnings_known_clause(as_of)
        query = (
            "SELECT * FROM earnings_dates WHERE ticker = ? AND event_date >= ?"
            + clause + " ORDER BY event_date ASC LIMIT 1"
        )
        row = self.conn.execute(query, (ticker, as_of.isoformat()) + extra).fetchone()
        return dict(row) if row else None
