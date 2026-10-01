"""Trade plan construction (owned by Bot 4) -- docs/PLAN.md section 10.

``build`` merges one ticker's same-day candidates into a single ``Recommendation``
(status PENDING). The cross-bot stub in ``src/contracts.py`` names a dict-returning
``build(candidate, data, config)``; that shape is available as ``plan_levels`` and is
what ``build`` uses internally.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

from src.contracts import Candidate, Recommendation
from src.utils.calendar import add_trading_days
from src.utils.logging import get_logger

log = get_logger(__name__)

EARNINGS_LOOKAHEAD_CALENDAR_DAYS = 120


def plan_levels(candidate: Candidate, data, config) -> dict[str, Any]:
    """Entry/stop/target/valid_until for one candidate (the contract-stub shape)."""
    tp = config.trade_plan
    risk = candidate.entry - candidate.stop
    return {
        "entry": candidate.entry,
        "stop": candidate.stop,
        "target": round(candidate.entry + tp.target_r_multiple * risk, 4),
        "valid_until": add_trading_days(candidate.signal_date, tp.entry_validity_trading_days),
    }


def _sector_industry(data, ticker: str) -> tuple[Optional[str], Optional[str]]:
    row = data.conn.execute(
        "SELECT sector, industry FROM tickers WHERE ticker = ?", (ticker,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def next_earnings(data, ticker: str, as_of: date) -> Optional[date]:
    """Next known earnings date on/after ``as_of``, honoring point-in-time: an event
    row whose acceptance timestamp is after ``as_of`` was not yet knowable."""
    events = data.get_earnings_events(
        ticker, as_of, as_of + timedelta(days=EARNINGS_LOOKAHEAD_CALENDAR_DAYS)
    )
    for _, ev in events.iterrows():
        acc = ev.get("acceptance_datetime")
        if pd.notna(acc) and acc and str(acc)[:10] > as_of.isoformat():
            continue
        return date.fromisoformat(str(ev["event_date"])[:10])
    return None


def build(
    candidates: list[Candidate],
    data,
    config,
    rs_pct: float = 0.0,
    regime: Optional[str] = None,
    source: str = "live",
    now: Optional[datetime] = None,
) -> Recommendation:
    """Merge one ticker's candidates for one day into a PENDING ``Recommendation``.

    The candidate with the highest ``setup_score`` is primary (ties: module name) and
    supplies entry/stop. Ranking fields (track/total/rank/in_report) and guardrail
    fields are left at neutral defaults for ``ranking.rank`` to fill in.
    """
    if not candidates:
        raise ValueError("build() needs at least one candidate")
    tickers = {c.ticker for c in candidates}
    days = {c.signal_date for c in candidates}
    if len(tickers) != 1 or len(days) != 1:
        raise ValueError("build() merges candidates of a single ticker on a single day")
    ticker, signal_date = tickers.pop(), days.pop()

    ordered = sorted(candidates, key=lambda c: (-c.setup_score, c.module))
    primary = ordered[0]
    levels = plan_levels(primary, data, config)
    risk = primary.entry - primary.stop
    modules = [c.module for c in ordered]

    sector, industry = _sector_industry(data, ticker)
    if regime is None:
        row = data.get_regime(signal_date)
        regime = row["regime"] if row else "Unknown"

    earnings = next_earnings(data, ticker, signal_date)
    window_end = add_trading_days(signal_date, config.ranking.earnings_window_trading_days)
    in_window = earnings is not None and earnings <= window_end

    rationale = primary.rationale
    if len(ordered) > 1:
        rationale += " Also signaled by: " + ", ".join(modules[1:]) + "."

    details: dict[str, Any] = {
        "entry": levels["entry"],
        "stop": levels["stop"],
        "target": levels["target"],
        "risk_per_share": round(risk, 4),
        "stop_pct": round(risk / primary.entry, 4),
        "target_pct": round((levels["target"] - primary.entry) / primary.entry, 4),
        "target_r_multiple": config.trade_plan.target_r_multiple,
        "primary": primary.details,
        "by_module": {
            c.module: {
                "entry": c.entry,
                "stop": c.stop,
                "setup_score": c.setup_score,
                "rationale": c.rationale,
                "details": c.details,
            }
            for c in ordered
        },
    }
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    return Recommendation(
        id=f"{signal_date.isoformat()}_{ticker}",
        source=source,
        signal_date=signal_date,
        ticker=ticker,
        modules=modules,
        primary_module=primary.module,
        setup_score=primary.setup_score,
        rs_pct=float(rs_pct),
        overlap_score=0.0,
        track_score=0.0,
        total_score=0.0,
        rank=None,
        in_report=False,
        entry=levels["entry"],
        stop=levels["stop"],
        target=levels["target"],
        valid_until=levels["valid_until"],
        regime=regime,
        sector=sector,
        industry=industry,
        earnings_date=earnings,
        earnings_in_window=bool(in_window),
        guardrail_status="unknown",
        guardrail_reasons=[],
        valuation_info={},
        rationale=rationale,
        details=details,
        llm_brief=None,
        status="PENDING",
        current_r=None,
        last_updated=stamp,
    )
