"""Tests for src/guardrail.py against docs/PLAN.md section 8."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src import guardrail
from src.data_access import DataAccess

AS_OF = date(2024, 12, 31)


def _insert_ticker(conn: sqlite3.Connection, ticker: str, cik: str, sector: str = "Technology") -> None:
    conn.execute(
        "INSERT INTO tickers (ticker, cik, name, exchange, sector, industry, sic, "
        "sector_etf, is_active, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
        (ticker, cik, ticker, "NASDAQ", sector, f"{sector} Industry", "7372", "XLK", AS_OF.isoformat()),
    )


def _insert_fundamental(
    conn: sqlite3.Connection, cik: str, ticker: str, metric: str, period_end: str,
    value: float, filed_date: str, period_type: str = "duration", period_start: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO fundamentals (cik, ticker, metric, period_start, period_end, "
        "period_type, value, form, filed_date, accession, tag_used, is_proxy) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
        (cik, ticker, metric, period_start, period_end, period_type, value, "10-Q", filed_date, f"acc-{metric}-{period_end}", "TestTag"),
    )


def _healthy_quarters(conn, cik, ticker, metric, base_value, ends):
    for i, end in enumerate(ends):
        _insert_fundamental(conn, cik, ticker, metric, end, base_value, end)


@pytest.fixture()
def conn(writable_fixture_conn) -> sqlite3.Connection:
    return writable_fixture_conn


def _seed_healthy_company(conn, ticker="HLTHY", cik="0000000099", sector="Technology"):
    _insert_ticker(conn, ticker, cik, sector)
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2023-12-31", 1_000_000, "2023-12-31", period_type="instant")
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2024-12-31", 1_020_000, "2024-12-31", period_type="instant")
    _insert_fundamental(conn, cik, ticker, "total_debt", "2024-12-31", 20.0, "2024-12-31", period_type="instant")
    conn.commit()
    return ticker


def test_pass_when_all_metrics_healthy(conn):
    ticker = _seed_healthy_company(conn)
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status == "pass"
    assert result.reasons == []


def test_fail_when_op_income_and_fcf_both_negative(conn):
    ticker = "BADCO"
    cik = "0000000098"
    _insert_ticker(conn, ticker, cik)
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, -10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, -5.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 2.0, end)  # fcf = -5-2 = -7, negative
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status == "fail"
    assert any("operating income" in r.lower() and "free cash flow" in r.lower() for r in result.reasons)


def test_pass_when_op_income_negative_but_fcf_positive(conn):
    """Only fails when BOTH are negative."""
    ticker = "MIXEDCO"
    cik = "0000000097"
    _insert_ticker(conn, ticker, cik)
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, -10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)  # fcf = 12, positive
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status != "fail"


def test_fail_when_shares_outstanding_grew_more_than_10pct_yoy(conn):
    ticker = "DILUTED"
    cik = "0000000096"
    _insert_ticker(conn, ticker, cik)
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2023-12-31", 1_000_000, "2023-12-31", period_type="instant")
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2024-12-31", 1_150_000, "2024-12-31", period_type="instant")
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status == "fail"
    assert any("shares outstanding" in r.lower() for r in result.reasons)
    assert "+15" in result.reasons[0] or "15" in result.reasons[0]


def test_fail_when_debt_exceeds_5x_operating_income(conn):
    ticker = "LEVERED"
    cik = "0000000095"
    _insert_ticker(conn, ticker, cik, sector="Technology")
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "total_debt", "2024-12-31", 250.0, "2024-12-31", period_type="instant")  # 250/40=6.25x
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status == "fail"
    assert any("debt" in r.lower() for r in result.reasons)


def test_debt_test_skipped_for_financials_sector(conn):
    ticker = "BANKCO"
    cik = "0000000094"
    _insert_ticker(conn, ticker, cik, sector="Financials")
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "total_debt", "2024-12-31", 999.0, "2024-12-31", period_type="instant")
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status != "fail"


def test_debt_test_skipped_when_operating_income_not_positive(conn):
    ticker = "ZEROCO"
    cik = "0000000093"
    _insert_ticker(conn, ticker, cik, sector="Technology")
    quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    for end in quarter_ends:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 0.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "total_debt", "2024-12-31", 999.0, "2024-12-31", period_type="instant")
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.status != "fail"


def test_unknown_when_no_fundamentals_and_no_fetch_fn(conn):
    ticker = "NODATA"
    cik = "0000000092"
    _insert_ticker(conn, ticker, cik)
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn), fetch_fn=lambda c, cik: None)
    assert result.status == "unknown"
    assert result.reasons


def test_calls_injected_fetch_fn_when_ticker_missing_from_db(conn):
    ticker = "FETCHME"
    cik = "0000000091"
    _insert_ticker(conn, ticker, cik)
    conn.commit()

    called = {}

    def fake_fetch(c, cik_arg):
        called["cik"] = cik_arg
        quarter_ends = ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
        for end in quarter_ends:
            _insert_fundamental(c, cik_arg, ticker, "operating_income", end, 10.0, end)
            _insert_fundamental(c, cik_arg, ticker, "operating_cash_flow", end, 15.0, end)
            _insert_fundamental(c, cik_arg, ticker, "capex", end, 3.0, end)
        c.commit()

    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn), fetch_fn=fake_fetch)
    assert called["cik"] == cik
    assert result.status == "pass"


def test_backtest_never_fetches_when_fetch_fn_is_noop(conn):
    """Backtests must be able to pass a no-op fetch_fn so point-in-time replay
    never makes a live network call."""
    ticker = "BACKTESTONLY"
    cik = "0000000090"
    _insert_ticker(conn, ticker, cik)
    conn.commit()

    calls = []
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn), fetch_fn=lambda c, cik: calls.append(cik))
    assert calls == [cik]  # was called (ticker had no data), but never hit the network
    assert result.status == "unknown"  # fake fetch_fn didn't actually insert data


def test_valuation_info_populated_from_fundamentals_summary(conn):
    ticker = _seed_healthy_company(conn, ticker="VALCO", cik="0000000089")
    conn.execute(
        "INSERT INTO fundamentals_summary (as_of, ticker, revenue_ttm, op_income_ttm, "
        "fcf_ttm, total_debt, cash, shares, ev_fcf, ev_fcf_sector_pct) VALUES "
        "(?,?,500,40,60,20,10,1000000,12.5,35.0)",
        (AS_OF.isoformat(), ticker),
    )
    conn.commit()
    result = guardrail.evaluate(ticker, AS_OF, DataAccess(conn))
    assert result.valuation_info.get("ev_fcf") == pytest.approx(12.5)
    assert result.valuation_info.get("ev_fcf_sector_pct") == pytest.approx(35.0)
