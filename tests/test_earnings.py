"""Tests for src/data/earnings.py (Bot 1)."""

from __future__ import annotations

from datetime import date

import pytest

from src.data import earnings


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
    def __init__(self, responses_by_date):
        self._responses_by_date = responses_by_date
        self.requested_dates: list[str] = []

    def get(self, url, params=None, timeout=None):
        d = params["date"]
        self.requested_dates.append(d)
        payload = self._responses_by_date.get(d, {"data": {"rows": []}})
        return FakeResponse(payload)


def _seed_tickers(conn, tickers):
    now = date.today().isoformat()
    conn.executemany(
        "INSERT INTO tickers (ticker, name, updated_at) VALUES (?, ?, ?)",
        [(t, t, now) for t in tickers],
    )
    conn.commit()


def test_update_upcoming_earnings_parses_timing_and_filters_known_tickers(
    writable_fixture_conn, monkeypatch
):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM earnings_dates")
    conn.commit()
    _seed_tickers(conn, ["AAPL", "MSFT"])

    # Monday 2024-01-08 is a trading day.
    as_of = date(2024, 1, 8)
    responses = {
        "2024-01-08": {
            "data": {
                "rows": [
                    {"symbol": "AAPL", "time": "time-pre-market"},
                    {"symbol": "MSFT", "time": "time-after-hours"},
                    {"symbol": "UNKNOWNCO", "time": "time-pre-market"},
                ]
            }
        }
    }
    fake_session = FakeSession(responses)
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: fake_session)

    earnings.update_upcoming_earnings(conn, as_of, days_ahead=0)

    rows = conn.execute(
        "SELECT ticker, event_date, timing, source FROM earnings_dates ORDER BY ticker"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"ticker": "AAPL", "event_date": "2024-01-08", "timing": "BMO", "source": "nasdaq_calendar"},
        {"ticker": "MSFT", "event_date": "2024-01-08", "timing": "AMC", "source": "nasdaq_calendar"},
    ]


def test_update_upcoming_earnings_requests_one_call_per_trading_day(
    writable_fixture_conn, monkeypatch
):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM earnings_dates")
    conn.commit()
    _seed_tickers(conn, ["AAPL"])

    # 2024-01-08 (Mon) through 2024-01-10 (Wed) -> 3 trading days.
    as_of = date(2024, 1, 8)
    fake_session = FakeSession({})
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: fake_session)

    earnings.update_upcoming_earnings(conn, as_of, days_ahead=2)

    assert fake_session.requested_dates == ["2024-01-08", "2024-01-09", "2024-01-10"]


def test_update_upcoming_earnings_missing_timing_stored_as_none(
    writable_fixture_conn, monkeypatch
):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM earnings_dates")
    conn.commit()
    _seed_tickers(conn, ["AAPL"])

    as_of = date(2024, 1, 8)
    responses = {
        "2024-01-08": {"data": {"rows": [{"symbol": "AAPL", "time": "time-not-supplied"}]}}
    }
    fake_session = FakeSession(responses)
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: fake_session)

    earnings.update_upcoming_earnings(conn, as_of, days_ahead=0)

    row = conn.execute(
        "SELECT timing FROM earnings_dates WHERE ticker = 'AAPL'"
    ).fetchone()
    assert row["timing"] is None


@pytest.mark.skipif(True, reason="live network smoke test; run manually with RUN_NETWORK_TESTS=1")
def test_live_smoke_update_upcoming_earnings():
    pass
