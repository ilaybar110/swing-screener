"""Reliability behaviour of the daily/weekly entrypoints on fixtures (network stubbed):
job_log persistence, partial-day retries, EDGAR index backfill, missed-run alerts, stale
prices."""

from __future__ import annotations

import json
from datetime import date

import pytest

import run_daily
import run_weekly
from src import universe
from src.data import edgar_daily, fundamentals, prices
from tests.e2e.conftest import now_after, seed_last_day
from tests.e2e.test_daily_flow import read_csv, run_full, write_briefs
from tests.e2e.test_failures_and_weekly import _module_class, _screener_frame

TARGET = date(2024, 12, 31)


def _jobs(env) -> list[dict]:
    return read_csv(env.state / "job_log.csv")


def _row(env, job: str, day: date) -> dict | None:
    rows = [j for j in _jobs(env) if j["job"] == job and j["trading_date"] == day.isoformat()]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _break_module_d(monkeypatch):
    import src.modules.d_base_breakout as d_mod

    def scan_boom(self, as_of, data):
        raise ValueError("module blew up")

    monkeypatch.setattr(_module_class(d_mod), "scan", scan_boom)
    return _module_class(d_mod)


# ---------------------------------------------------------------------------
# job_log persistence
# ---------------------------------------------------------------------------


def test_telegram_step_reaches_job_log_csv(env):
    run_full(env)
    row = _row(env, "telegram", TARGET)
    assert row is not None and row["status"] == "ok" and row["message"] == "sent"


def test_invalid_brief_reasons_are_logged(env):
    seed_last_day(env.state, date(2024, 12, 30))
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    tickers = write_briefs(env.work)
    assert tickers
    (env.work / "brief_outputs" / f"{tickers[0]}.json").write_text("{not json", encoding="utf-8")
    assert run_daily.finalize(env.cfg, now=now) == 0
    msg = json.loads(_row(env, "merge_briefs", TARGET)["message"])
    assert msg["invalid"] == 1 and msg["merged"] == len(tickers) - 1
    reason = msg["invalid_reasons"][tickers[0]]
    assert reason.startswith("unreadable JSON") and len(reason) <= run_daily.MAX_REASON_CHARS


def test_weekly_rows_survive_a_daily_run_for_the_same_day(env, monkeypatch):
    frame = _screener_frame(env)
    monkeypatch.setattr(universe, "_fetch_universe_source", lambda cfg: (frame, "nasdaq"))
    monkeypatch.setattr(fundamentals, "update_summary", lambda conn, as_of, client=None: {
        "universe_size": 30, "requests_made": 0, "resolved": {}})
    seed_last_day(env.state, date(2024, 12, 30))
    assert run_weekly.run(env.cfg, now=now_after(TARGET)) == 0
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 0
    assert _row(env, "weekly:prices", TARGET) is not None
    assert _row(env, "weekly:earnings_calendar", TARGET) is not None
    assert _row(env, "prices", TARGET) is not None and _row(env, "earnings_calendar", TARGET) is not None


# ---------------------------------------------------------------------------
# partial days
# ---------------------------------------------------------------------------


def test_failed_step_marks_day_partial_and_next_run_retries_it(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 23))
    cls = _module_class(__import__("src.modules.d_base_breakout", fromlist=["x"]))
    working = cls.scan
    _break_module_d(monkeypatch)
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 24))) == 0
    row = _row(env, "day", date(2024, 12, 24))
    assert row["status"] == "partial" and "scan:base_breakout" in row["message"] and "attempt 1/3" in row["message"]
    assert not any("MOMD1" == r["ticker"] for f in (env.state / "recommendations").glob("*.csv")
                   for r in read_csv(f) if r["signal_date"] == "2024-12-24")

    monkeypatch.setattr(cls, "scan", working)  # fixed: the next run (new session) retries 12-24
    env.calls.edgar.clear()
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 26))) == 0
    assert env.calls.edgar == [date(2024, 12, 24), date(2024, 12, 26)]
    assert _row(env, "day", date(2024, 12, 24))["status"] == "ok"
    assert "attempt 2/3" in _row(env, "day", date(2024, 12, 24))["message"]
    assert _row(env, "day", date(2024, 12, 26))["status"] == "ok"
    recs = [r for f in (env.state / "recommendations").glob("*.csv") for r in read_csv(f)]
    assert any(r["ticker"] == "MOMD1" and r["signal_date"] == "2024-12-24" for r in recs), \
        "the retried day must pick up the module that failed the first time"
    assert len({r["id"] for r in recs}) == len(recs)


@pytest.mark.slow
def test_always_failing_step_gives_up_after_three_runs(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 20))
    _break_module_d(monkeypatch)
    for day in (date(2024, 12, 23), date(2024, 12, 24), date(2024, 12, 26)):
        assert run_daily.prepare(env.cfg, now=now_after(day)) == 0
    first = _row(env, "day", date(2024, 12, 23))
    assert first["status"] == "failed" and "attempt 3/3" in first["message"]
    assert len(env.calls.alert) == 1 and "2024-12-23" in env.calls.alert[0]
    assert _row(env, "day", date(2024, 12, 24))["status"] == "partial"

    env.calls.edgar.clear()  # a failed day is never retried again
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 27))) == 0
    assert date(2024, 12, 23) not in env.calls.edgar
    assert _row(env, "day", date(2024, 12, 23))["status"] == "failed"


def test_partial_retries_respect_the_catch_up_limit(env, monkeypatch):
    env.cfg.daily.max_catchup_days = 1
    seed_last_day(env.state, date(2024, 12, 23))
    _break_module_d(monkeypatch)
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 24))) == 0
    env.calls.edgar.clear()
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 26))) == 0
    assert env.calls.edgar == [date(2024, 12, 26)]  # no budget left for the retry


def test_up_to_date_run_does_not_retry_on_its_own(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))
    _break_module_d(monkeypatch)
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 0
    env.calls.edgar.clear()
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 0
    assert env.calls.edgar == []


# ---------------------------------------------------------------------------
# EDGAR backfill
# ---------------------------------------------------------------------------


def test_unpublished_edgar_index_is_backfilled_on_a_later_run(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 26))
    published: set[date] = {date(2024, 12, 26)}

    def lagging_process_day(conn, day, client=None, lookback_days=10):
        env.calls.edgar.append(day)
        idx = max(d for d in published if d <= day)
        return {"requested_date": day.isoformat(), "index_date": idx.isoformat(),
                "index_is_requested_day": idx == day, "form4_processed": 0, "eightk_processed": 0,
                "earnings_events": 0, "errors": []}

    def exact(conn, index_date, client=None):
        env.calls.edgar_index.append(index_date)
        if index_date not in published:
            raise edgar_daily.IndexNotPublished(f"{index_date} not available")
        return {"requested_date": index_date.isoformat(), "index_date": index_date.isoformat(),
                "index_is_requested_day": True, "form4_processed": 2, "eightk_processed": 1,
                "earnings_events": 0, "errors": []}

    monkeypatch.setattr(edgar_daily, "process_day", lagging_process_day)
    monkeypatch.setattr(edgar_daily, "process_index_date", exact)
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 27))) == 0
    assert _row(env, "edgar_index", date(2024, 12, 27))["status"] == "missing"
    assert env.calls.edgar_index == []  # not re-requested in the run that just missed it

    published |= {date(2024, 12, 27), date(2024, 12, 30)}
    assert run_daily.prepare(env.cfg, now=now_after(date(2024, 12, 30))) == 0
    assert env.calls.edgar_index == [date(2024, 12, 27)]
    row = _row(env, "edgar_index", date(2024, 12, 27))
    assert row["status"] == "ok" and row["message"].startswith("backfilled")
    assert json.loads(_row(env, "edgar_backfill", date(2024, 12, 30))["message"])["backfilled"] == ["2024-12-27"]


@pytest.mark.slow
def test_never_published_edgar_index_is_given_up(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 23))

    def fallback(conn, day, client=None, lookback_days=10):
        env.calls.edgar.append(day)
        idx = date(2024, 12, 23) if day == date(2024, 12, 24) else day  # 12-24 never gets an index
        return {"requested_date": day.isoformat(), "index_date": idx.isoformat(), "form4_processed": 0,
                "eightk_processed": 0, "earnings_events": 0, "errors": []}

    def never(conn, index_date, client=None):
        env.calls.edgar_index.append(index_date)
        raise edgar_daily.IndexNotPublished("404")

    monkeypatch.setattr(edgar_daily, "process_day", fallback)
    monkeypatch.setattr(edgar_daily, "process_index_date", never)
    for day in (date(2024, 12, 24), date(2024, 12, 26), date(2024, 12, 27), date(2024, 12, 30), TARGET):
        assert run_daily.prepare(env.cfg, now=now_after(day)) == 0
    assert env.calls.edgar_index == [date(2024, 12, 24)] * 2
    row = _row(env, "edgar_index", date(2024, 12, 24))
    assert row["status"] == "missing" and "attempt 3/3: gave up" in row["message"]
    assert env.calls.alert == []


# ---------------------------------------------------------------------------
# missed runs, stale prices
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_missed_runs_send_one_alert(env):
    run_full(env, target=date(2024, 12, 20), last=date(2024, 12, 19))
    assert env.calls.alert == []
    env.cfg.daily.max_catchup_days = 2
    now = now_after(date(2024, 12, 26))  # 12-23, 12-24, 12-26 were never run
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert len(env.calls.alert) == 1 and "3 session(s) behind" in env.calls.alert[0]
    assert _row(env, "missed_run", date(2024, 12, 26))["status"] == "alert"
    assert run_daily.prepare(env.cfg, now=now) == 0  # the rest of the catch-up: no second alert
    assert len(env.calls.alert) == 1


def test_weekly_alerts_when_daily_reports_lag(env, monkeypatch):
    run_full(env, target=date(2024, 12, 24), last=date(2024, 12, 23))
    frame = _screener_frame(env)
    monkeypatch.setattr(universe, "_fetch_universe_source", lambda cfg: (frame, "nasdaq"))
    monkeypatch.setattr(fundamentals, "update_summary", lambda conn, as_of, client=None: {
        "universe_size": 30, "requests_made": 0, "resolved": {}})
    assert run_weekly.run(env.cfg, now=now_after(date(2024, 12, 27))) == 0
    assert len(env.calls.alert) == 1 and "weekly run" in env.calls.alert[0]
    assert _row(env, "weekly:missed_run", date(2024, 12, 27)) is not None


def test_stale_prices_are_logged_and_shown_in_the_report(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))
    real = prices.update_prices

    def with_stale(conn, tickers, start, end, **kw):
        rep = real(conn, tickers, start, end, **kw)
        conn.execute("DELETE FROM prices WHERE ticker = 'CTRL05' AND date = ?", (end.isoformat(),))
        conn.commit()
        rep["stale"] = ["CTRL05"]
        return rep

    monkeypatch.setattr(prices, "update_prices", with_stale)
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert json.loads(_row(env, "prices", TARGET)["message"])["stale"] == ["CTRL05"]
    assert "CTRL05" in _row(env, "prices_stale", TARGET)["message"]
    assert run_daily.finalize(env.cfg, now=now) == 0
    md = (env.reports / "2024-12-31.md").read_text(encoding="utf-8")
    assert "stale" in md and "CTRL05" in md
