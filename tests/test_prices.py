"""update_prices: yfinance chunk -> retry pass -> Nasdaq fallback -> missing (all mocked)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.data import prices
from src.db import init_db

START, END = date(2024, 1, 2), date(2024, 1, 4)


def _yf_frame(tickers):
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    cols = {}
    for t in tickers:
        for name, base in (("Open", 10), ("High", 11), ("Low", 9), ("Close", 10.5), ("Adj Close", 10.4), ("Volume", 1000)):
            cols[(t, name)] = [base, base, base]
    return pd.DataFrame(cols, index=idx)


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


def _src(conn, t):
    return {r[0] for r in conn.execute("SELECT price_source FROM prices WHERE ticker=?", (t,))}


def test_retry_then_nasdaq_then_missing_are_not_fatal(conn, monkeypatch):
    calls = []

    def fake_dl(tickers, start, end):
        calls.append(list(tickers))
        # first pass: AAA ok, BBB/CCC/DDD absent; retry pass: BBB recovers
        ok = [t for t in tickers if t == "AAA" or (t == "BBB" and len(calls) > 1)]
        return _yf_frame(ok) if ok else pd.DataFrame()

    nasdaq_calls = []

    def fake_nasdaq(t, start, end):
        nasdaq_calls.append(t)
        if t == "CCC":
            return [("CCC", "2024-01-02", 1, 2, 0.5, 1.5, 1.5, 100, "nasdaq")]
        raise RuntimeError("blocked")

    sleeps = []
    monkeypatch.setattr(prices, "_yf_download", fake_dl)
    monkeypatch.setattr(prices, "_nasdaq_history", fake_nasdaq)
    rep = prices.update_prices(conn, ["AAA", "BBB", "CCC", "DDD"], START, END, sleep=sleeps.append)

    assert rep["successes"] == ["AAA"]
    assert rep["retried"] == ["BBB"]
    assert rep["nasdaq_filled"] == ["CCC"]
    assert rep["missing"] == ["DDD"]
    assert len(sleeps) == 1  # one pause before the single retry pass
    assert sorted(nasdaq_calls) == ["CCC", "DDD"]  # only still-missing tickers hit Nasdaq
    assert _src(conn, "CCC") == {"nasdaq"} and _src(conn, "AAA") == {"yfinance"}
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE ticker='DDD'").fetchone()[0] == 0


def test_whole_chunk_exception_goes_to_retry(conn, monkeypatch):
    n = {"i": 0}

    def fake_dl(tickers, start, end):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("yahoo down")
        return _yf_frame(tickers)

    monkeypatch.setattr(prices, "_yf_download", fake_dl)
    monkeypatch.setattr(prices, "_nasdaq_history", lambda *a: [])
    rep = prices.update_prices(conn, ["AAA"], START, END, sleep=lambda s: None)
    assert rep["retried"] == ["AAA"] and rep["missing"] == []


def test_upsert_is_idempotent_and_adj_close_kept(conn, monkeypatch):
    monkeypatch.setattr(prices, "_yf_download", lambda t, s, e: _yf_frame(t))
    for _ in range(2):
        prices.update_prices(conn, ["AAA"], START, END, sleep=lambda s: None)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 3
    row = conn.execute("SELECT close, adj_close FROM prices WHERE ticker='AAA' LIMIT 1").fetchone()
    assert (row[0], row[1]) == (10.5, 10.4)


def test_nasdaq_parser_handles_formatting(monkeypatch):
    class R:
        def raise_for_status(self): ...
        def json(self):
            return {"data": {"tradesTable": {"rows": [
                {"date": "01/03/2024", "close": "$1,234.50", "open": "$1,200.00", "high": "$1,240.00", "low": "$1,190.00", "volume": "1,000"},
                {"date": "bad", "close": "$1"},
                {"date": "01/04/2024", "close": "N/A"},
            ]}}}

    class S:
        def get(self, *a, **k): return R()

    monkeypatch.setattr(prices, "get_session", lambda **k: S())
    rows = prices._nasdaq_history("AAA", START, END)
    assert rows == [("AAA", "2024-01-03", 1200.0, 1240.0, 1190.0, 1234.5, 1234.5, 1000, "nasdaq")]


def test_symbol_mapping_stores_under_our_symbol(conn, monkeypatch):
    seen = []
    monkeypatch.setattr(prices, "_yf_download", lambda t, s, e: (seen.append(t), _yf_frame(["BRK-B"]))[1])
    prices.update_prices(conn, ["BRK/B"], START, END, sleep=lambda s: None)
    assert seen == [["BRK-B"]]
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE ticker='BRK/B'").fetchone()[0] == 3
