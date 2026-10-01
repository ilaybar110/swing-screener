"""Shared e2e harness: a sandboxed repo root (state/, reports/, data/, work/ in tmp_path),
seeded from tests/fixtures, with every network-facing call replaced by a stand-in that
serves the same fixture data. Everything else -- scans, ranking, tracking, reports,
state export -- is the real code."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src import jobs, state_io
from src.config import load_config

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXED_STAMP = datetime(2025, 1, 2, 5, 0, tzinfo=timezone.utc)


def now_after(day: date) -> datetime:
    """A morning-after-the-close moment for ``day`` (so ``day`` is the latest finished session)."""
    return datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).replace(hour=5)


def seed_last_day(state_dir: Path, day: date) -> None:
    """Pretend ``day`` is the last trading day a previous run completed."""
    path = state_dir / "job_log.csv"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["run_id", "job", "trading_date", "started_at", "finished_at", "status", "message"])
        w.writerow(["seed", jobs.DAY_JOB, day.isoformat(), "", "", "ok", "seeded"])


def seed_recommendation(state_dir: Path, **values: Any) -> None:
    """Append one recommendation row to its month shard in state/recommendations/."""
    row: dict[str, Any] = {k: "" for k in state_io._RECOMMENDATIONS_FIELDS}
    row.update({
        "source": "live", "modules": json.dumps(["a_momentum_pullback"]),
        "primary_module": "a_momentum_pullback", "setup_score": 70, "rs_pct": 80,
        "overlap_score": 0, "track_score": 0, "total_score": 60, "rank": 1, "in_report": 0,
        "regime": "Favorable", "earnings_in_window": 0, "guardrail_status": "unknown",
        "guardrail_reasons": "[]", "valuation_info": "{}", "details": "{}",
        "rationale": "seeded", "status": "pending",
    })
    row.update(values)
    shard = state_dir / "recommendations" / f"{str(row['signal_date'])[:7]}.csv"
    shard.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if shard.exists():
        with open(shard, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    rows.append({k: str(v) for k, v in row.items()})
    with open(shard, "w", encoding="utf-8", newline="\n") as f:
        w = csv.DictWriter(f, fieldnames=state_io._RECOMMENDATIONS_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def tree_bytes(*roots: Path) -> dict[str, bytes]:
    """Every file under the roots -> its bytes, keyed relative to the roots' parent
    (for 'nothing changed' assertions)."""
    out: dict[str, bytes] = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[p.relative_to(root.parent).as_posix()] = p.read_bytes()
    return out


def _as_yahoo_adjusted(fix, ticker: str, rows, end: date) -> list[tuple]:
    """The fixture stores SPLIT1 unadjusted (and its split-day bar keeps the pre-split open and
    high). Live Yahoo history is split-adjusted, so serve it that way: divide every bar before a
    split that has already happened by its ratio (volume multiplied), and repair the split bar."""
    out = [list(r) for r in rows]
    for sdate, ratio in fix.execute("SELECT date, ratio FROM splits WHERE ticker = ?", (ticker,)).fetchall():
        if sdate > end.isoformat():
            continue
        for r in out:
            if r[1] < sdate:
                r[2:7] = [v / ratio for v in r[2:7]]
                r[7] = int(r[7] * ratio)
            elif r[1] == sdate:
                if r[2] > 1.5 * r[5]:
                    r[2] /= ratio
                if r[3] > 1.5 * r[5]:
                    r[3] = max(r[3] / ratio, r[2], r[5])
    return [tuple(r) for r in out]


def install_stubs(monkeypatch) -> SimpleNamespace:
    """Replace every network-facing call with a stand-in serving fixture data."""
    fix = sqlite3.connect(f"file:{(FIXTURES / 'fixture.db').as_posix()}?mode=ro", uri=True)
    fix.row_factory = sqlite3.Row
    calls = SimpleNamespace(prices=[], edgar=[], splits=[], earnings=[], report=[], alert=[], fix=fix)

    def fake_update_prices(conn, tickers, start, end, **kw):
        calls.prices.append((sorted(tickers), start, end))
        found: set[str] = set()
        for t in sorted(set(tickers)):
            rows = fix.execute(
                "SELECT ticker, date, open, high, low, close, adj_close, volume, 'yfinance' "
                "FROM prices WHERE ticker = ? AND date >= ? AND date <= ?",
                (t, start.isoformat(), end.isoformat())).fetchall()
            if rows:
                conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?,?)",
                                 [tuple(r) for r in _as_yahoo_adjusted(fix, t, rows, end)])
                found.add(t)
        conn.commit()
        return {"successes": sorted(found), "retried": [], "nasdaq_filled": [],
                "missing": sorted(set(tickers) - found)}

    def fake_update_splits(conn, tickers):
        calls.splits.append(sorted(tickers))
        n = 0
        for t in tickers:
            for r in fix.execute("SELECT ticker, date, ratio FROM splits WHERE ticker = ?", (t,)):
                conn.execute("INSERT OR REPLACE INTO splits VALUES (?,?,?)", tuple(r))
                n += 1
        conn.commit()
        return n

    def fake_process_day(conn, day, client=None, lookback_days=10):
        calls.edgar.append(day)
        return {"requested_date": day.isoformat(), "index_date": day.isoformat(),
                "index_is_requested_day": True, "form4_processed": 0, "eightk_processed": 0,
                "earnings_events": 0, "errors": []}

    from notify import telegram
    from src.data import earnings, edgar_daily, fundamentals, news, prices, texts

    monkeypatch.setattr(prices, "update_prices", fake_update_prices)
    monkeypatch.setattr(prices, "update_splits", fake_update_splits)
    monkeypatch.setattr(edgar_daily, "process_day", fake_process_day)
    monkeypatch.setattr(earnings, "update_upcoming_earnings",
                        lambda conn, as_of, days_ahead: calls.earnings.append((as_of, days_ahead)))
    monkeypatch.setattr(fundamentals, "fetch_company", lambda conn, cik, client=None: 0)
    monkeypatch.setattr(news, "get_news", lambda *a, **k: [])
    monkeypatch.setattr(texts, "get_8k_texts", lambda *a, **k: [])
    monkeypatch.setattr(telegram, "send_report",
                        lambda html, text, **k: calls.report.append((html, text)) or True)
    monkeypatch.setattr(telegram, "send_alert", lambda text, **k: calls.alert.append(text) or True)
    monkeypatch.setattr(jobs, "utc_now", lambda: FIXED_STAMP)
    return calls


def make_root(root: Path):
    """state/ seeded from the fixtures, an empty reports/, and a sandboxed config."""
    shutil.copytree(FIXTURES / "state", root / "state")
    (root / "reports").mkdir(exist_ok=True)
    return load_config(base_dir=root)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = make_root(tmp_path)
    calls = install_stubs(monkeypatch)
    yield SimpleNamespace(cfg=cfg, root=tmp_path, state=tmp_path / "state", reports=tmp_path / "reports",
                          work=tmp_path / "work", calls=calls)
    calls.fix.close()
