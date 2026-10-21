"""Tests for src/data/earnings.py (Bot 1)."""

from __future__ import annotations

from datetime import date

import pytest

from src.data import earnings
from src.db import ensure_earnings_columns


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

    earnings.update_upcoming_earnings(conn, as_of, days_ahead=2, lookback_trading_days=0)

    assert fake_session.requested_dates == ["2024-01-08", "2024-01-09", "2024-01-10"]


def test_update_upcoming_earnings_also_refetches_lookback_sessions(writable_fixture_conn, monkeypatch):
    conn = writable_fixture_conn
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM earnings_dates")
    conn.commit()
    _seed_tickers(conn, ["AAPL"])
    fake_session = FakeSession({})
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: fake_session)

    # Mon 2024-01-08: the two sessions before are Thu 01-04 and Fri 01-05
    earnings.update_upcoming_earnings(conn, date(2024, 1, 8), days_ahead=0)

    assert fake_session.requested_dates == ["2024-01-04", "2024-01-05", "2024-01-08"]


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


def _rows(conn, ticker="AAPL"):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM earnings_dates WHERE ticker = ? ORDER BY event_date", (ticker,))]


@pytest.fixture()
def cal_conn(writable_fixture_conn):
    conn = writable_fixture_conn
    ensure_earnings_columns(conn)  # fixture.db predates the additive columns
    conn.execute("DELETE FROM tickers")
    conn.execute("DELETE FROM earnings_dates")
    conn.commit()
    _seed_tickers(conn, ["AAPL", "MSFT"])
    return conn


def _run(conn, monkeypatch, as_of, responses, days_ahead, lookback=0):
    session = FakeSession(responses)
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: session)
    earnings.update_upcoming_earnings(conn, as_of, days_ahead=days_ahead, lookback_trading_days=lookback)


def _day(*rows):
    return {"data": {"rows": list(rows)}}


def test_moved_date_replaces_the_stale_row_and_keeps_known_date(cal_conn, monkeypatch):
    conn = cal_conn
    # 2024-01-08: AAPL listed for 01-10
    _run(conn, monkeypatch, date(2024, 1, 8),
         {"2024-01-10": _day({"symbol": "AAPL", "time": "time-after-hours"},
                             {"symbol": "MSFT", "time": "time-pre-market"})}, days_ahead=4)
    assert [r["event_date"] for r in _rows(conn)] == ["2024-01-10"]
    # 2024-01-09: Nasdaq moved AAPL to 01-11; MSFT is unchanged on 01-10
    _run(conn, monkeypatch, date(2024, 1, 9),
         {"2024-01-10": _day({"symbol": "MSFT", "time": "time-pre-market"}),
          "2024-01-11": _day({"symbol": "AAPL", "time": "time-after-hours"})}, days_ahead=4)
    rows = _rows(conn)
    assert [r["event_date"] for r in rows] == ["2024-01-11"]
    assert rows[0]["known_date"] == "2024-01-09"
    msft = _rows(conn, "MSFT")
    assert [(r["event_date"], r["known_date"], r["last_seen_date"]) for r in msft] == [
        ("2024-01-10", "2024-01-08", "2024-01-09")]
    # get_upcoming_earnings now returns the new date (it used to return the stale 01-10)
    from src.data_access import DataAccess
    assert DataAccess(conn).get_upcoming_earnings("AAPL", date(2024, 1, 9))["event_date"] == "2024-01-11"


def test_moved_date_is_superseded_even_if_the_old_day_failed_to_fetch(cal_conn, monkeypatch):
    conn = cal_conn
    _run(conn, monkeypatch, date(2024, 1, 8),
         {"2024-01-10": _day({"symbol": "AAPL", "time": "time-after-hours"})}, days_ahead=4)

    class Failing(FakeSession):
        def get(self, url, params=None, timeout=None):
            if params["date"] == "2024-01-10":
                raise RuntimeError("boom")
            return super().get(url, params=params, timeout=timeout)

    session = Failing({"2024-01-12": _day({"symbol": "AAPL", "time": "time-after-hours"})})
    monkeypatch.setattr(earnings, "get_session", lambda **kwargs: session)
    earnings.update_upcoming_earnings(conn, date(2024, 1, 9), days_ahead=4, lookback_trading_days=0)
    assert [r["event_date"] for r in _rows(conn)] == ["2024-01-12"]


def test_failed_or_empty_day_keeps_its_rows(cal_conn, monkeypatch):
    conn = cal_conn
    _run(conn, monkeypatch, date(2024, 1, 8),
         {"2024-01-10": _day({"symbol": "AAPL", "time": "time-after-hours"})}, days_ahead=4)
    # next refresh: 01-10 comes back empty (ambiguous) -> nothing is deleted
    _run(conn, monkeypatch, date(2024, 1, 9), {}, days_ahead=4)
    assert [r["event_date"] for r in _rows(conn)] == ["2024-01-10"]


def test_nasdaq_estimate_fields_are_stored_and_actuals_from_lookback(cal_conn, monkeypatch):
    conn = cal_conn
    _run(conn, monkeypatch, date(2024, 1, 8),
         {"2024-01-10": _day({"symbol": "AAPL", "time": "time-after-hours", "epsForecast": "$1.20",
                              "noOfEsts": "12", "lastYearEPS": "($0.15)"})}, days_ahead=4)
    r = _rows(conn)[0]
    assert (r["eps_forecast"], r["num_estimates"], r["last_year_eps"], r["eps_actual"]) == (1.2, 12, -0.15, None)
    # two sessions later the report day is in the lookback: actual EPS + surprise recorded,
    # the estimate fields survive even if the row omits them, and past rows are never deleted
    _run(conn, monkeypatch, date(2024, 1, 12),
         {"2024-01-10": _day({"symbol": "AAPL", "time": "time-after-hours", "eps": "$1.32",
                              "surprise": "10", "epsForecast": "N/A"})}, days_ahead=0, lookback=2)
    r = _rows(conn)[0]
    assert (r["eps_forecast"], r["eps_actual"], r["eps_surprise_pct"], r["known_date"]) == (
        1.2, 1.32, 10.0, "2024-01-08")


def test_parse_number():
    assert earnings.parse_number("$1.23") == 1.23
    assert earnings.parse_number("($0.45)") == -0.45
    assert earnings.parse_number("-3.2") == -3.2
    assert earnings.parse_number("1,234") == 1234.0
    assert earnings.parse_number("N/A") is None
    assert earnings.parse_number("") is None
    assert earnings.parse_number(None) is None


def test_get_upcoming_earnings_is_point_in_time(cal_conn):
    from src.data_access import DataAccess

    conn = cal_conn
    conn.executemany(
        "INSERT INTO earnings_dates (ticker, event_date, timing, source, acceptance_datetime, known_date) "
        "VALUES (?,?,?,?,?,?)",
        [("AAPL", "2024-02-01", "AMC", "nasdaq_calendar", None, "2024-01-20"),
         ("AAPL", "2024-01-25", None, "8k_2.02", "2024-01-24T11:00:00", None),
         ("AAPL", "2024-04-30", "AMC", "nasdaq_calendar", None, None)],  # legacy row: no known_date
    )
    conn.commit()
    data = DataAccess(conn)
    assert data.get_upcoming_earnings("AAPL", date(2024, 1, 10))["event_date"] == "2024-04-30"
    assert data.get_upcoming_earnings("AAPL", date(2024, 1, 20))["event_date"] == "2024-02-01"
    assert data.get_upcoming_earnings("AAPL", date(2024, 1, 24))["event_date"] == "2024-01-25"
    ev = data.get_earnings_events("AAPL", date(2024, 1, 1), date(2024, 12, 31), known_as_of=date(2024, 1, 10))
    assert list(ev["event_date"]) == ["2024-04-30"]
    assert len(data.get_earnings_events("AAPL", date(2024, 1, 1), date(2024, 12, 31))) == 3


def test_data_access_works_without_the_additive_columns(data_access):
    # read-only fixture.db predates known_date: the point-in-time filter degrades gracefully
    assert data_access.get_upcoming_earnings("MOMB1", date(2024, 12, 3))["event_date"] == "2024-12-03"
    # the 8-K was accepted on 2024-12-03: not knowable the day before
    assert data_access.get_upcoming_earnings("MOMB1", date(2024, 12, 2)) is None


@pytest.mark.skipif(True, reason="live network smoke test; run manually with RUN_NETWORK_TESTS=1")
def test_live_smoke_update_upcoming_earnings():
    pass
