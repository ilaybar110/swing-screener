"""Tests for src/regime.py (Bot 1)."""

from __future__ import annotations

from datetime import date

from src import regime


def test_compute_regime_favorable_when_spy_and_breadth_both_strong(writable_fixture_conn):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM regime_log")
    conn.execute("DELETE FROM prices WHERE ticker = 'SPY'")
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers WHERE ticker NOT IN ('AAA', 'BBB')")
    conn.commit()

    # Two tickers, both trading above their own flat 50-day SMA; SPY trending up
    # and above its 200-day SMA. 60 trading days of history is enough for both
    # SMA windows.
    dates = [date(2024, 1, 2 + i) for i in range(0, 1)]  # placeholder, built below
    from src.utils.calendar import trading_days

    days = trading_days(date(2023, 10, 1), date(2024, 2, 1))[-70:]
    conn.executemany(
        "INSERT INTO tickers (ticker, updated_at) VALUES (?, '2024-01-01')",
        [("AAA",), ("BBB",)],
    )
    price_rows = []
    for i, d in enumerate(days):
        spy_close = 400.0 + i * 1.0  # steadily rising -> above its own SMA200
        price_rows.append(("SPY", d.isoformat(), spy_close, spy_close, spy_close, spy_close, spy_close, 1000, "yfinance"))
        for t in ("AAA", "BBB"):
            close = 50.0 + i * 0.5  # steadily rising -> above its own SMA50
            price_rows.append((t, d.isoformat(), close, close, close, close, close, 1000, "yfinance"))
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, price_source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        price_rows,
    )
    as_of = days[-1]
    conn.execute(
        "INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) VALUES (?,?,?,?,?)",
        (as_of.isoformat(), "AAA", 60.0, 2e9, 3e7),
    )
    conn.execute(
        "INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) VALUES (?,?,?,?,?)",
        (as_of.isoformat(), "BBB", 60.0, 2e9, 3e7),
    )
    conn.commit()

    result = regime.compute_regime(conn, as_of)

    assert result["regime"] == "Favorable"
    assert result["breadth_pct"] == 100.0
    assert result["date"] == as_of.isoformat()

    stored = conn.execute(
        "SELECT regime, breadth_pct FROM regime_log WHERE date = ?", (as_of.isoformat(),)
    ).fetchone()
    assert stored["regime"] == "Favorable"


def test_compute_regime_unfavorable_when_spy_and_breadth_both_weak(writable_fixture_conn):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM regime_log")
    conn.execute("DELETE FROM prices WHERE ticker = 'SPY'")
    conn.execute("DELETE FROM universe_snapshots")
    conn.execute("DELETE FROM tickers WHERE ticker NOT IN ('AAA', 'BBB')")
    conn.commit()

    from src.utils.calendar import trading_days

    days = trading_days(date(2023, 10, 1), date(2024, 2, 1))[-70:]
    conn.executemany(
        "INSERT INTO tickers (ticker, updated_at) VALUES (?, '2024-01-01')",
        [("AAA",), ("BBB",)],
    )
    price_rows = []
    for i, d in enumerate(days):
        spy_close = 400.0 - i * 1.0  # steadily falling -> below its own SMA200
        price_rows.append(("SPY", d.isoformat(), spy_close, spy_close, spy_close, spy_close, spy_close, 1000, "yfinance"))
        for t in ("AAA", "BBB"):
            close = 50.0 - i * 0.3  # steadily falling -> below its own SMA50
            price_rows.append((t, d.isoformat(), close, close, close, close, close, 1000, "yfinance"))
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, price_source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        price_rows,
    )
    as_of = days[-1]
    for t in ("AAA", "BBB"):
        conn.execute(
            "INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) VALUES (?,?,?,?,?)",
            (as_of.isoformat(), t, 30.0, 2e9, 3e7),
        )
    conn.commit()

    result = regime.compute_regime(conn, as_of)

    assert result["regime"] == "Unfavorable"
    assert result["breadth_pct"] == 0.0


def test_backfill_regime_writes_one_row_per_trading_day(writable_fixture_conn):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM regime_log")
    conn.commit()

    from datetime import date as date_
    start = date_(2023, 6, 1)
    end = date_(2023, 6, 9)

    rows = regime.backfill_regime(conn, start, end)

    from src.utils.calendar import trading_days
    expected_days = trading_days(start, end)
    assert len(rows) == len(expected_days)

    stored_count = conn.execute(
        "SELECT COUNT(*) FROM regime_log WHERE date >= ? AND date <= ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    assert stored_count == len(expected_days)
