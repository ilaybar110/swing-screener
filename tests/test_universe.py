"""Tests for src/data/universe_source.py and src/universe.py (Bot 1)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from src.data import universe_source
from src import universe


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc
        self.urls_called: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.urls_called.append(url)
        if self._raise_exc:
            raise self._raise_exc
        return FakeResponse(self._payload)


NASDAQ_PAYLOAD = {
    "data": {
        "rows": [
            {
                "symbol": "AAA",
                "name": "Alpha Corp Common Stock",
                "sector": "Technology",
                "industry": "Software",
                "marketCap": "2500000000",
                "lastsale": "$45.20",
                "country": "United States",
            },
            {
                "symbol": "BBB",
                "name": "Beta Acquisition Corp",
                "sector": "",
                "industry": "",
                "marketCap": "800000000",
                "lastsale": "$10.10",
                "country": "United States",
            },
        ]
    }
}


class FakeEdgarClient:
    def __init__(self, cik_map=None):
        self._cik_map = cik_map or {"AAA": "0000000001", "BBB": "0000000002"}

    def get_cik_map(self):
        return dict(self._cik_map)

    def get_json(self, url, use_cache=True):
        return {
            "data": {
                "AAA": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Corp"},
                "BBB": {"cik_str": 2, "ticker": "BBB", "title": "Beta Acquisition Corp"},
            }
        }


def test_fetch_nasdaq_screener_parses_rows(monkeypatch, tmp_path):
    fake_session = FakeSession(payload=NASDAQ_PAYLOAD)
    monkeypatch.setattr(universe_source, "get_session", lambda **kw: fake_session)

    df = universe_source.fetch_nasdaq_screener(tmp_path)

    assert list(df["ticker"]) == ["AAA", "BBB"]
    assert df.loc[df["ticker"] == "AAA", "market_cap"].iloc[0] == pytest.approx(2_500_000_000)
    assert df.loc[df["ticker"] == "AAA", "price"].iloc[0] == pytest.approx(45.20)
    assert any("screener" in u for u in fake_session.urls_called)


def test_get_universe_source_falls_back_to_sec_on_nasdaq_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        universe_source,
        "fetch_nasdaq_screener",
        lambda raw_cache_dir: (_ for _ in ()).throw(RuntimeError("nasdaq down")),
    )
    sec_df = pd.DataFrame(
        [{"ticker": "AAA", "name": "Alpha Corp", "exchange": "NASDAQ", "cik": "0000000001",
          "sector": None, "industry": None, "sic": None, "market_cap": None, "price": None}]
    )
    monkeypatch.setattr(universe_source, "fetch_sec_fallback", lambda edgar_client: sec_df)

    df, source = universe_source.get_universe_source(tmp_path, FakeEdgarClient())

    assert source == "sec"
    assert list(df["ticker"]) == ["AAA"]


def test_upsert_tickers_maps_sector_etf_and_cik(writable_fixture_conn):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.commit()

    df = pd.DataFrame(
        [
            {"ticker": "AAA", "name": "Alpha Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 2.5e9, "price": 45.2},
        ]
    )
    report = universe_source.upsert_tickers(conn, df, FakeEdgarClient())

    row = conn.execute(
        "SELECT ticker, cik, sector, industry, sector_etf FROM tickers WHERE ticker = 'AAA'"
    ).fetchone()
    assert row["cik"] == "0000000001"
    assert row["sector"] == "Technology"
    assert row["sector_etf"] == "XLK"
    assert report["count"] == 1


def test_upsert_tickers_normalizes_nasdaq_sector_labels(writable_fixture_conn):
    """Regression: Nasdaq says 'Finance'/'Basic Materials'/'Telecommunications', but the
    sector-ETF map and the guardrail's Financials exception use GICS-style names."""
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.commit()
    names = {"FIN": "Finance", "MAT": "Basic Materials", "COM": "Telecommunications",
             "MIS": "Miscellaneous", "NAN": float("nan")}
    df = pd.DataFrame([
        {"ticker": t, "name": t, "exchange": "NYSE", "sector": s, "industry": "x", "sic": None,
         "market_cap": 2e9, "price": 10.0} for t, s in names.items()
    ])
    universe_source.upsert_tickers(conn, df, FakeEdgarClient())
    got = {r["ticker"]: (r["sector"], r["sector_etf"])
           for r in conn.execute("SELECT ticker, sector, sector_etf FROM tickers")}
    assert got["FIN"] == ("Financials", "XLF")
    assert got["MAT"] == ("Materials", "XLB")
    assert got["COM"] == ("Communication Services", "XLC")
    assert got["MIS"] == ("Miscellaneous", None)
    assert got["NAN"] == (None, None)


def _insert_price_series(conn, ticker, days, price, volume=2_000_000):
    rows = [
        (ticker, d.isoformat(), price, price, price, price, price, volume, "yfinance")
        for d in days
    ]
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, price_source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def test_refresh_universe_applies_price_cap_and_adv_filters(writable_fixture_conn, monkeypatch):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM prices")
    conn.commit()

    from src.utils.calendar import trading_days
    days = trading_days(date(2023, 6, 1), date(2024, 1, 2))[-200:]
    as_of = days[-1]

    # AAA: passes every filter.
    _insert_price_series(conn, "AAA", days, price=50.0, volume=1_000_000)
    # PENNY: price too low.
    _insert_price_series(conn, "PENNY", days, price=2.0, volume=1_000_000)
    # THIN: fails ADV20 (tiny volume).
    _insert_price_series(conn, "THIN", days, price=50.0, volume=10)
    # SHORTHIST: not enough trading days.
    _insert_price_series(conn, "SHORTHIST", days[-50:], price=50.0, volume=1_000_000)

    screener_df = pd.DataFrame(
        [
            {"ticker": "AAA", "name": "Alpha Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 5e9, "price": 50.0},
            {"ticker": "PENNY", "name": "Penny Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 5e9, "price": 2.0},
            {"ticker": "THIN", "name": "Thin Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 5e9, "price": 50.0},
            {"ticker": "SHORTHIST", "name": "Short Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 5e9, "price": 50.0},
        ]
    )
    monkeypatch.setattr(
        universe, "_fetch_universe_source", lambda cfg: (screener_df, "nasdaq")
    )
    monkeypatch.setattr(universe.universe_source, "upsert_tickers", lambda *a, **kw: {"count": 4})

    universe.refresh_universe(conn, as_of)

    survivors = {
        r["ticker"]
        for r in conn.execute(
            "SELECT ticker FROM universe_snapshots WHERE snapshot_date = ?", (as_of.isoformat(),)
        ).fetchall()
    }
    assert survivors == {"AAA"}


def test_refresh_universe_excludes_spac_by_sic_and_name_pattern(writable_fixture_conn, monkeypatch):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM prices")
    conn.commit()

    from src.utils.calendar import trading_days
    days = trading_days(date(2023, 6, 1), date(2024, 1, 2))[-200:]
    as_of = days[-1]

    _insert_price_series(conn, "GOOD", days, price=50.0, volume=1_000_000)
    _insert_price_series(conn, "SPACCO", days, price=50.0, volume=1_000_000)
    _insert_price_series(conn, "SICBAD", days, price=50.0, volume=1_000_000)

    screener_df = pd.DataFrame(
        [
            {"ticker": "GOOD", "name": "Good Corp", "exchange": "NASDAQ", "sector": "Technology",
             "industry": "Software", "sic": None, "market_cap": 5e9, "price": 50.0},
            {"ticker": "SPACCO", "name": "Spacco Acquisition Corp", "exchange": "NASDAQ", "sector": None,
             "industry": None, "sic": None, "market_cap": 5e9, "price": 50.0},
            {"ticker": "SICBAD", "name": "Sicbad Holdings", "exchange": "NASDAQ", "sector": None,
             "industry": None, "sic": "6770", "market_cap": 5e9, "price": 50.0},
        ]
    )
    monkeypatch.setattr(
        universe, "_fetch_universe_source", lambda cfg: (screener_df, "nasdaq")
    )
    monkeypatch.setattr(universe.universe_source, "upsert_tickers", lambda *a, **kw: {"count": 3})

    universe.refresh_universe(conn, as_of)

    survivors = {
        r["ticker"]
        for r in conn.execute(
            "SELECT ticker FROM universe_snapshots WHERE snapshot_date = ?", (as_of.isoformat(),)
        ).fetchall()
    }
    assert survivors == {"GOOD"}


def test_build_historical_universe_uses_shares_times_price(writable_fixture_conn):
    from src.data_access import DataAccess

    conn = writable_fixture_conn
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM fundamentals_summary")
    conn.commit()

    from src.utils.calendar import trading_days
    days = trading_days(date(2023, 6, 1), date(2024, 1, 2))[-200:]
    as_of = days[-1]

    _insert_price_series(conn, "AAA", days, price=100.0, volume=1_000_000)
    conn.execute(
        "INSERT INTO tickers (ticker, name, sic, updated_at) VALUES ('AAA', 'Alpha Corp', NULL, '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO fundamentals_summary (as_of, ticker, shares) VALUES (?, 'AAA', ?)",
        ((as_of - timedelta(days=3)).isoformat(), 2_000_000_000.0),
    )
    conn.commit()

    data = DataAccess(conn)
    df = universe.build_historical_universe(data, as_of)

    row = df[df["ticker"] == "AAA"].iloc[0]
    assert row["market_cap"] == pytest.approx(2_000_000_000.0 * 100.0)
    assert bool(row["market_cap_is_approx"]) is False


def test_build_historical_universe_flags_fallback_market_cap(writable_fixture_conn):
    from src.data_access import DataAccess

    conn = writable_fixture_conn
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM fundamentals_summary")
    conn.commit()

    from src.utils.calendar import trading_days
    days = trading_days(date(2023, 6, 1), date(2024, 1, 2))[-200:]
    as_of = days[-1]

    _insert_price_series(conn, "AAA", days, price=100.0, volume=1_000_000)
    conn.execute(
        "INSERT INTO tickers (ticker, name, sic, updated_at) VALUES ('AAA', 'Alpha Corp', NULL, '2024-01-01')"
    )
    conn.execute(
        "INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
        "VALUES (?, 'AAA', 100.0, 3000000000.0, 1e8)",
        (as_of.isoformat(),),
    )
    conn.commit()

    data = DataAccess(conn)
    df = universe.build_historical_universe(data, as_of)

    row = df[df["ticker"] == "AAA"].iloc[0]
    assert row["market_cap"] == pytest.approx(3_000_000_000.0)
    assert bool(row["market_cap_is_approx"]) is True


def test_upsert_tickers_finds_cik_for_slash_share_classes(writable_fixture_conn):
    """Regression: Nasdaq lists 'BRK/B', SEC's map has 'BRK-B' -> cik was NULL."""
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.commit()
    df = pd.DataFrame([{"ticker": "BRK/B", "name": "Berkshire", "exchange": "NYSE", "sector": "Finance",
                        "industry": "x", "sic": None, "market_cap": 9e11, "price": 450.0}])
    universe_source.upsert_tickers(conn, df, FakeEdgarClient({"BRK-B": "0001067983"}))
    assert conn.execute("SELECT cik FROM tickers WHERE ticker='BRK/B'").fetchone()["cik"] == "0001067983"


def test_build_historical_universe_uses_filed_shares_when_no_summary(writable_fixture_conn):
    """Regression (backtest smoke run): a research DB built by run_backfill has no
    fundamentals_summary history, so the historical universe was always empty and the
    backtest produced no signals."""
    conn = writable_fixture_conn
    conn.execute("DELETE FROM fundamentals_summary")
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM splits WHERE ticker='CTRL03'")
    conn.commit()
    from src.data_access import DataAccess

    data = DataAccess(conn)
    as_of = date(2024, 6, 7)
    assert universe.build_historical_universe(data, as_of).empty  # nothing to compute a cap from
    price = float(conn.execute("SELECT close FROM prices WHERE ticker='CTRL03' AND date<=? ORDER BY date DESC LIMIT 1",
                               (as_of.isoformat(),)).fetchone()[0])
    conn.execute(
        "INSERT INTO fundamentals (cik, ticker, metric, period_start, period_end, period_type, value, form, filed_date, accession, tag_used, is_proxy) "
        "VALUES ('0000000003','CTRL03','shares_outstanding',NULL,'2024-03-31','instant',?, '10-Q','2024-05-01','a','t',0)",
        (3e9 / price,))
    # a filing made after as_of must not be used
    conn.execute(
        "INSERT INTO fundamentals (cik, ticker, metric, period_start, period_end, period_type, value, form, filed_date, accession, tag_used, is_proxy) "
        "VALUES ('0000000003','CTRL03','shares_outstanding',NULL,'2024-06-30','instant',1, '10-Q','2024-08-01','b','t',0)")
    conn.commit()
    df = universe.build_historical_universe(data, as_of)
    row = df[df["ticker"] == "CTRL03"]
    assert len(row) == 1 and float(row["market_cap"].iloc[0]) == pytest.approx(3e9)
    assert not bool(row["market_cap_is_approx"].iloc[0])
    # a later 2:1 split (price history is adjusted for it, the old share count is not)
    conn.execute("INSERT INTO splits (ticker, date, ratio) VALUES ('CTRL03','2024-09-03',2.0)")
    conn.commit()
    row = universe.build_historical_universe(data, as_of)
    assert float(row[row["ticker"] == "CTRL03"]["market_cap"].iloc[0]) == pytest.approx(6e9)
