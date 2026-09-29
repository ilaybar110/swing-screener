"""SQLite schema and connection management.

Works identically against data/run.db (ephemeral, cloud) and data/research.db
(permanent, local) -- callers just point get_conn() / init_db() at a different path.

Migrations are plain numbered SQL blocks applied in order and tracked in the
schema_migrations table, so init_db() is safe to call every run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

StrPath = Union[str, Path]

_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS tickers (
        ticker TEXT PRIMARY KEY,
        cik TEXT,
        name TEXT,
        exchange TEXT,
        sector TEXT,
        industry TEXT,
        sic TEXT,
        sector_etf TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS prices (
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        adj_close REAL,
        volume INTEGER,
        price_source TEXT,
        PRIMARY KEY (ticker, date)
    );
    CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

    CREATE TABLE IF NOT EXISTS splits (
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        ratio REAL NOT NULL,
        PRIMARY KEY (ticker, date)
    );

    CREATE TABLE IF NOT EXISTS universe_snapshots (
        snapshot_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        price REAL,
        market_cap REAL,
        adv20 REAL,
        PRIMARY KEY (snapshot_date, ticker)
    );
    CREATE INDEX IF NOT EXISTS idx_universe_snapshots_date ON universe_snapshots(snapshot_date);

    CREATE TABLE IF NOT EXISTS regime_log (
        date TEXT PRIMARY KEY,
        spy_close REAL,
        spy_sma200 REAL,
        breadth_pct REAL,
        regime TEXT
    );

    CREATE TABLE IF NOT EXISTS fundamentals (
        cik TEXT NOT NULL,
        ticker TEXT,
        metric TEXT NOT NULL,
        period_start TEXT,
        period_end TEXT NOT NULL,
        period_type TEXT,
        value REAL,
        form TEXT,
        filed_date TEXT NOT NULL,
        accession TEXT,
        tag_used TEXT,
        is_proxy INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_fundamentals_lookup
        ON fundamentals(ticker, metric, filed_date);

    CREATE TABLE IF NOT EXISTS fundamentals_summary (
        as_of TEXT NOT NULL,
        ticker TEXT NOT NULL,
        revenue_ttm REAL,
        op_income_ttm REAL,
        fcf_ttm REAL,
        total_debt REAL,
        cash REAL,
        shares REAL,
        ev_fcf REAL,
        ev_fcf_sector_pct REAL,
        PRIMARY KEY (as_of, ticker)
    );

    CREATE TABLE IF NOT EXISTS filings (
        accession TEXT PRIMARY KEY,
        cik TEXT NOT NULL,
        ticker TEXT,
        form TEXT NOT NULL,
        filed_date TEXT NOT NULL,
        acceptance_datetime TEXT,
        items TEXT,
        primary_doc_url TEXT,
        exhibit_991_url TEXT,
        processed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_filings_ticker_date ON filings(ticker, filed_date);

    CREATE TABLE IF NOT EXISTS insider_trades (
        accession TEXT NOT NULL,
        row_num INTEGER NOT NULL,
        cik TEXT,
        ticker TEXT,
        insider_cik TEXT,
        insider_name TEXT,
        is_officer INTEGER,
        is_director INTEGER,
        is_ten_pct_owner INTEGER,
        officer_title TEXT,
        trans_code TEXT,
        trans_date TEXT,
        shares REAL,
        price REAL,
        value REAL,
        acquired_disposed TEXT,
        filed_date TEXT,
        PRIMARY KEY (accession, row_num)
    );
    CREATE INDEX IF NOT EXISTS idx_insider_trades_ticker_date
        ON insider_trades(ticker, filed_date);

    CREATE TABLE IF NOT EXISTS earnings_dates (
        ticker TEXT NOT NULL,
        event_date TEXT NOT NULL,
        timing TEXT,
        source TEXT NOT NULL,
        acceptance_datetime TEXT,
        PRIMARY KEY (ticker, event_date, source)
    );

    CREATE TABLE IF NOT EXISTS filing_texts (
        accession TEXT NOT NULL,
        section TEXT NOT NULL,
        text TEXT,
        PRIMARY KEY (accession, section)
    );

    CREATE TABLE IF NOT EXISTS recommendations (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        modules TEXT,
        primary_module TEXT,
        setup_score REAL,
        rs_pct REAL,
        overlap_score REAL,
        track_score REAL,
        total_score REAL,
        rank INTEGER,
        in_report INTEGER NOT NULL DEFAULT 0,
        entry REAL,
        stop REAL,
        target REAL,
        valid_until TEXT,
        regime TEXT,
        sector TEXT,
        industry TEXT,
        earnings_date TEXT,
        earnings_in_window INTEGER,
        guardrail_status TEXT,
        guardrail_reasons TEXT,
        valuation_info TEXT,
        rationale TEXT,
        details TEXT,
        llm_brief TEXT,
        status TEXT,
        current_r REAL,
        last_updated TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_recommendations_source_date
        ON recommendations(source, signal_date);
    CREATE INDEX IF NOT EXISTS idx_recommendations_ticker
        ON recommendations(ticker);

    CREATE TABLE IF NOT EXISTS recommendation_results (
        rec_id TEXT PRIMARY KEY,
        final_status TEXT,
        entry_date TEXT,
        entry_fill REAL,
        exit_date TEXT,
        avg_exit_price REAL,
        exit_reason TEXT,
        r_multiple REAL,
        pct_return REAL,
        days_held INTEGER,
        mae_r REAL,
        mfe_r REAL,
        spy_return REAL,
        sector_etf_return REAL,
        excess_vs_spy REAL,
        excess_vs_sector REAL,
        FOREIGN KEY (rec_id) REFERENCES recommendations(id)
    );

    CREATE TABLE IF NOT EXISTS baselines (
        id TEXT PRIMARY KEY,
        parent_rec_id TEXT NOT NULL,
        sample_no INTEGER NOT NULL,
        source TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        stop_pct REAL,
        status TEXT,
        r_multiple REAL,
        pct_return REAL,
        exit_date TEXT,
        exit_reason TEXT,
        FOREIGN KEY (parent_rec_id) REFERENCES recommendations(id)
    );
    CREATE INDEX IF NOT EXISTS idx_baselines_parent ON baselines(parent_rec_id);

    CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        config_hash TEXT,
        config_snapshot TEXT,
        notes TEXT
    );

    CREATE TABLE IF NOT EXISTS job_log (
        run_id TEXT NOT NULL,
        job TEXT NOT NULL,
        trading_date TEXT,
        started_at TEXT,
        finished_at TEXT,
        status TEXT,
        message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_job_log_run ON job_log(run_id, job);
    """),
]


def get_conn(path: StrPath) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults (foreign keys on, WAL off for
    portability across the ephemeral cloud filesystem)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return row["v"] or 0
    except sqlite3.OperationalError:
        return 0


def init_db(path: StrPath) -> sqlite3.Connection:
    """Create/upgrade the schema at path, returning an open connection.

    Safe to call every run: only migrations newer than the recorded version are
    applied.
    """
    conn = get_conn(path)
    current = _current_version(conn)
    for version, sql in _MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
        )
        conn.commit()
    return conn
