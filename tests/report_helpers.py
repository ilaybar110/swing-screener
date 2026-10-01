"""Builders for report / brief tests: a throwaway schema-initialised DB populated with a
handful of live recommendations (the committed fixture.db has no recommendations)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src.config import Config, load_config
from src.contracts import Recommendation
from src.db import init_db

AS_OF = date(2026, 9, 30)
PREV = date(2026, 9, 29)

GOOD_BRIEF = {
    "ticker": "AAA",
    "summary": "Reported quarterly results and raised guidance.",
    "upcoming_catalysts": [{"event": "Investor day", "date": "2026-11-10"}, {"event": "Product launch", "date": None}],
    "red_flags": ["Filed a $300M at-the-market equity offering (8-K 0000000001-26-000001)"],
    "recent_positive_events": ["Raised full-year revenue guidance"],
    "sources": ["0000000001-26-000001", "https://news.example.com/aaa"],
}


def make_config(tmp_path: Path) -> Config:
    """Real config.yaml with every path rooted in tmp_path."""
    return load_config(base_dir=tmp_path)


def make_rec(ticker: str, signal: date = AS_OF, rank: Optional[int] = 1, *, in_report: bool = True,
             regime: str = "Favorable", status: str = "pending", current_r: Optional[float] = None,
             llm_brief: Optional[dict[str, Any]] = None, modules: Optional[list[str]] = None,
             industry: str = "Software", earnings_in_window: bool = False,
             guardrail: str = "pass", entry: float = 100.0) -> Recommendation:
    stop, target = round(entry * 0.95, 2), round(entry * 1.10, 2)
    return Recommendation(
        id=f"{signal.isoformat()}_{ticker}", source="live", signal_date=signal, ticker=ticker,
        modules=modules or ["a_momentum_pullback"], primary_module=(modules or ["a_momentum_pullback"])[0],
        setup_score=80.0, rs_pct=90.0, overlap_score=0.0, track_score=0.0, total_score=77.7, rank=rank,
        in_report=in_report, entry=entry, stop=stop, target=target, valid_until=date(2026, 10, 7),
        regime=regime, sector="Technology", industry=industry,
        earnings_date=date(2026, 10, 20), earnings_in_window=earnings_in_window,
        guardrail_status=guardrail, guardrail_reasons=[] if guardrail == "pass" else ["Missing fundamentals"],
        valuation_info={"ev_fcf_sector_pct": 35.0, "ev_fcf": 18.2},
        rationale=f"{ticker} pulled back to the 20-day SMA on light volume and broke the prior day's high.",
        details={"stop_pct": 0.05, "target_pct": 0.10, "target_r_multiple": 2.0},
        llm_brief=llm_brief, status=status, current_r=current_r, last_updated=signal.isoformat(),
    )


def insert_rec(conn: sqlite3.Connection, rec: Recommendation) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO recommendations (id, source, signal_date, ticker, modules, primary_module, "
        "setup_score, rs_pct, overlap_score, track_score, total_score, rank, in_report, entry, stop, target, "
        "valid_until, regime, sector, industry, earnings_date, earnings_in_window, guardrail_status, "
        "guardrail_reasons, valuation_info, rationale, details, llm_brief, status, current_r, last_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec.id, rec.source, rec.signal_date.isoformat(), rec.ticker, json.dumps(rec.modules),
         rec.primary_module, rec.setup_score, rec.rs_pct, rec.overlap_score, rec.track_score,
         rec.total_score, rec.rank, int(rec.in_report), rec.entry, rec.stop, rec.target,
         rec.valid_until.isoformat(), rec.regime, rec.sector, rec.industry,
         rec.earnings_date.isoformat() if rec.earnings_date else None, int(rec.earnings_in_window),
         rec.guardrail_status, json.dumps(rec.guardrail_reasons), json.dumps(rec.valuation_info),
         rec.rationale, json.dumps(rec.details),
         json.dumps(rec.llm_brief) if isinstance(rec.llm_brief, (dict, list)) else rec.llm_brief,
         rec.status, rec.current_r, rec.last_updated),
    )


def make_db(tmp_path: Path, *, regime: str = "Favorable", as_of: date = AS_OF) -> sqlite3.Connection:
    """Empty-but-initialised DB with tickers and a regime row for ``as_of``."""
    conn = init_db(tmp_path / "report_test.db")
    for t, name in (("AAA", "Alpha Corp"), ("BBB", "Beta Inc"), ("CCC", "Gamma Holdings"), ("OLD", "Old Co")):
        conn.execute("INSERT INTO tickers (ticker, name, sector, industry) VALUES (?,?,?,?)",
                     (t, name, "Technology", "Software"))
    spy, sma, breadth = {
        "Favorable": (450.0, 420.0, 62.0),
        "Caution": (450.0, 420.0, 41.0),
        "Unfavorable": (400.0, 420.0, 30.0),
    }[regime]
    conn.execute("INSERT INTO regime_log (date, spy_close, spy_sma200, breadth_pct, regime) VALUES (?,?,?,?,?)",
                 (as_of.isoformat(), spy, sma, breadth, regime))
    conn.commit()
    return conn


def add_result(conn: sqlite3.Connection, rec: Recommendation, final_status: str, exit_date: date, r: float,
               entry_date: Optional[date] = None) -> None:
    insert_rec(conn, rec)
    conn.execute(
        "INSERT INTO recommendation_results (rec_id, final_status, entry_date, entry_fill, exit_date, "
        "avg_exit_price, exit_reason, r_multiple, pct_return, days_held, mae_r, mfe_r, spy_return, "
        "sector_etf_return, excess_vs_spy, excess_vs_sector) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec.id, final_status, (entry_date or rec.signal_date).isoformat(), rec.entry, exit_date.isoformat(),
         rec.entry * (1 + r * 0.05), "stop" if final_status == "stopped" else "breakeven", r, r * 0.05, 5,
         -0.5, 1.0, 0.01, 0.01, r * 0.05 - 0.01, r * 0.05 - 0.01))
    conn.execute("UPDATE recommendations SET status = ? WHERE id = ?", (final_status, rec.id))
    conn.commit()
