"""Smoke tests for the foundation pieces owned by Bot 0: config, schema, calendar,
and DataAccess against the fixture DB.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date

import pytest

from src.config import load_config
from src.data_access import DataAccess
from src.db import init_db
from src.utils.calendar import add_trading_days, is_trading_day, trading_days


def test_load_config_reads_all_sections():
    os.environ.setdefault("SEC_EMAIL", "test@example.com")
    cfg = load_config()
    assert cfg.modules.a_momentum_pullback.enabled is True
    assert cfg.tracking.sector_etfs["Technology"] == "XLK"
    assert cfg.secrets.sec_email == os.environ["SEC_EMAIL"]


def test_init_db_creates_all_tables(tmp_path):
    conn = init_db(tmp_path / "test.db")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for expected in [
        "tickers", "prices", "splits", "universe_snapshots", "regime_log",
        "fundamentals", "fundamentals_summary", "filings", "insider_trades",
        "earnings_dates", "filing_texts", "recommendations",
        "recommendation_results", "baselines", "backtest_runs", "job_log",
    ]:
        assert expected in tables


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    conn1 = init_db(path)
    conn1.close()
    conn2 = init_db(path)  # should not raise on re-migration
    conn2.close()


def test_calendar_helpers():
    assert is_trading_day(date(2024, 1, 2))  # Tuesday
    assert not is_trading_day(date(2024, 1, 1))  # New Year's Day
    days = trading_days(date(2024, 1, 1), date(2024, 1, 10))
    assert date(2024, 1, 1) not in days
    assert add_trading_days(date(2024, 1, 2), 1) == date(2024, 1, 3)


def test_data_access_get_prices(data_access: DataAccess):
    df = data_access.get_prices("MOMA1", date(2022, 1, 1), date(2024, 12, 31))
    assert not df.empty
    assert list(df.columns) == [
        "date", "open", "high", "low", "close", "adj_close", "volume", "price_source",
    ]
    assert df["date"].is_monotonic_increasing


def test_data_access_get_universe(data_access: DataAccess):
    df = data_access.get_universe(date(2024, 12, 31))
    assert "MOMA1" in set(df["ticker"])


def test_data_access_get_regime(data_access: DataAccess):
    regime = data_access.get_regime(date(2024, 12, 31))
    assert regime is not None
    assert regime["regime"] in {"Favorable", "Caution", "Unfavorable"}


def test_data_access_point_in_time_insider_trades(data_access: DataAccess):
    # MOMC1's insider cluster is filed a few days after the trade dates; a
    # filed_as_of before the filing date must not see it.
    early = data_access.get_insider_trades(
        date(2022, 1, 1), date(2024, 12, 31), filed_as_of=date(2022, 1, 2)
    )
    assert early.empty
    late = data_access.get_insider_trades(
        date(2022, 1, 1), date(2024, 12, 31), filed_as_of=date(2024, 12, 31)
    )
    assert not late.empty
