"""Failure handling of the daily job and the weekly job, on fixtures (network stubbed)."""

from __future__ import annotations

import csv
from datetime import date

import pandas as pd

import run_daily
import run_weekly
from src import universe
from src.data import edgar_daily, fundamentals, prices
from tests.e2e.conftest import now_after, seed_last_day, tree_bytes
from tests.e2e.test_daily_flow import all_recs, read_csv

TARGET = date(2024, 12, 31)


def test_no_prices_at_all_is_critical_alerts_and_exports_valid_state(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))
    working = prices.update_prices
    monkeypatch.setattr(prices, "update_prices", lambda conn, tickers, start, end, **kw: {
        "successes": [], "retried": [], "nasdaq_filled": [], "missing": sorted(tickers)})
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 2
    assert len(env.calls.alert) == 1 and "no prices" in env.calls.alert[0].lower()
    # state was still exported, and the failed day was NOT marked complete
    jobs = read_csv(env.state / "job_log.csv")
    assert [j for j in jobs if j["job"] == "day" and j["trading_date"] == "2024-12-31"] == []
    assert any(j["job"] == "day" and j["trading_date"] == "2024-12-30" for j in jobs)
    assert all_recs(env.state) == []
    # the next run (prices are back) retries the same day
    monkeypatch.setattr(prices, "update_prices", working)
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 0
    assert any(r["ticker"] == "MOMA1" for r in all_recs(env.state))


def test_edgar_fully_down_is_critical(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))

    def boom(conn, day, client=None, lookback_days=10):
        raise RuntimeError("No published daily index found")

    monkeypatch.setattr(edgar_daily, "process_day", boom)
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 2
    assert any("EDGAR" in a for a in env.calls.alert)
    jobs = read_csv(env.state / "job_log.csv")
    assert any(j["job"] == "edgar" and j["status"] == "error" for j in jobs)


def test_non_critical_failures_are_logged_and_the_run_continues(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))
    import src.modules.d_base_breakout as d_mod

    def scan_boom(self, as_of, data):
        raise ValueError("module blew up")

    monkeypatch.setattr(_module_class(d_mod), "scan", scan_boom)
    from src.data import news

    monkeypatch.setattr(news, "get_news", lambda *a, **k: (_ for _ in ()).throw(OSError("rss down")))
    monkeypatch.setattr(prices, "update_splits", lambda conn, tickers: (_ for _ in ()).throw(OSError("yf down")))

    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert run_daily.finalize(env.cfg, now=now) == 0
    assert (env.reports / "2024-12-31.html").is_file()
    assert env.calls.alert == []
    jobs = read_csv(env.state / "job_log.csv")
    assert any(j["job"].startswith("scan:") and j["status"] == "error" for j in jobs)
    assert any(r["ticker"] == "MOMA1" for r in all_recs(env.state)), "other modules must still run"


def test_telegram_failure_does_not_fail_the_run(env, monkeypatch):
    from notify import telegram

    seed_last_day(env.state, date(2024, 12, 30))
    monkeypatch.setattr(telegram, "send_report", lambda *a, **k: (_ for _ in ()).throw(OSError("net")))
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert run_daily.finalize(env.cfg, now=now) == 0
    assert (env.reports / "2024-12-31.html").is_file()


def test_prices_job_message_feeds_the_report_data_quality_note(env, monkeypatch):
    seed_last_day(env.state, date(2024, 12, 30))
    real = prices.update_prices

    def partial(conn, tickers, start, end, **kw):
        rep = real(conn, [t for t in tickers if t != "CTRL05"], start, end, **kw)
        rep["missing"] = sorted(set(rep["missing"]) | {"CTRL05"})
        return rep

    monkeypatch.setattr(prices, "update_prices", partial)
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert run_daily.finalize(env.cfg, now=now) == 0
    assert "CTRL05" in (env.reports / "2024-12-31.md").read_text(encoding="utf-8")


def _module_class(mod):
    import inspect

    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if hasattr(obj, "scan_history") and obj.__module__ == mod.__name__:
            return obj
    raise AssertionError("no module class found")


# ---------------------------------------------------------------------------
# weekly
# ---------------------------------------------------------------------------


def _screener_frame(env) -> pd.DataFrame:
    rows = env.calls.fix.execute(
        "SELECT t.ticker, t.name, t.sector, t.industry FROM tickers t WHERE t.ticker LIKE 'MOM%' "
        "OR t.ticker LIKE 'NEG%' OR t.ticker LIKE 'CTRL%' OR t.ticker = 'SPLIT1'").fetchall()
    return pd.DataFrame([{
        "ticker": r["ticker"], "name": r["name"], "exchange": "NASDAQ", "sector": r["sector"],
        "industry": r["industry"], "sic": None, "market_cap": 2e9, "price": 50.0,
        "cik": f"{i + 1:010d}"} for i, r in enumerate(rows)])


def test_weekly_refreshes_universe_summary_and_earnings(env, monkeypatch):
    # start with no universe snapshots in state
    path = env.state / "universe_snapshots.csv"
    path.write_text("snapshot_date,ticker,price,market_cap,adv20\n", encoding="utf-8")
    frame = _screener_frame(env)
    monkeypatch.setattr(universe, "_fetch_universe_source", lambda cfg: (frame, "nasdaq"))
    summary_calls = []
    monkeypatch.setattr(fundamentals, "update_summary", lambda conn, as_of, client=None: (
        summary_calls.append(as_of) or {"universe_size": 30, "requests_made": 0, "resolved": {}}))

    assert run_weekly.run(env.cfg, now=now_after(TARGET)) == 0
    snap = read_csv(env.state / "universe_snapshots.csv")
    assert {r["snapshot_date"] for r in snap} == {"2024-12-31"}
    assert len(snap) == len(frame) == 30
    assert summary_calls == [TARGET]
    (as_of, days_ahead), = env.calls.earnings
    assert as_of == TARGET and 45 <= days_ahead <= 60  # 35 trading days ~ 8 calendar weeks
    jobs = read_csv(env.state / "job_log.csv")
    assert {"prices", "universe", "fundamentals_summary", "earnings_calendar"} <= {j["job"] for j in jobs}
    tickers = {r["ticker"]: r for r in read_csv(env.state / "tickers.csv")}
    assert tickers["MOMA1"]["cik"]


def test_weekly_is_idempotent_and_dry_run_writes_nothing(env, monkeypatch):
    frame = _screener_frame(env)
    monkeypatch.setattr(universe, "_fetch_universe_source", lambda cfg: (frame, "nasdaq"))
    monkeypatch.setattr(fundamentals, "update_summary", lambda conn, as_of, client=None: {
        "universe_size": 30, "requests_made": 0, "resolved": {}})
    before = tree_bytes(env.state)
    assert run_weekly.run(env.cfg, now=now_after(TARGET), dry_run=True) == 0
    assert tree_bytes(env.state) == before
    assert run_weekly.run(env.cfg, now=now_after(TARGET)) == 0
    first = tree_bytes(env.state)
    assert run_weekly.run(env.cfg, now=now_after(TARGET)) == 0
    assert tree_bytes(env.state) == first


def test_weekly_with_no_candidates_is_critical(env, monkeypatch):
    empty = _screener_frame(env).iloc[0:0]
    monkeypatch.setattr(universe, "_fetch_universe_source", lambda cfg: (empty, "nasdaq"))
    assert run_weekly.run(env.cfg, now=now_after(TARGET)) == 2
    assert len(env.calls.alert) == 1
