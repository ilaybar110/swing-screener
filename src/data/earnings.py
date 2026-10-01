"""Upcoming earnings calendar, sourced from Nasdaq's earnings calendar API.

Historical/recent earnings events (derived from 8-K Item 2.02 filings) are owned by
Bot 3 (src/data/edgar_daily.py); this module only handles the forward-looking
calendar (docs/PLAN.md section 4.5).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Optional

from src.utils.calendar import trading_days
from src.utils.http import get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"

_TIMING_MAP = {
    "time-pre-market": "BMO",
    "time-after-hours": "AMC",
}


def _map_timing(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _TIMING_MAP.get(raw)


def update_upcoming_earnings(conn: sqlite3.Connection, as_of: date, days_ahead: int) -> None:
    """Pull Nasdaq's earnings calendar for [as_of, as_of + days_ahead] (one HTTP
    request per NYSE trading day in that range) and upsert into `earnings_dates`
    with source="nasdaq_calendar". Only tickers already present in `tickers` are
    kept, to avoid polluting the DB with symbols outside our universe/watchlist.
    """
    known_tickers = {
        row[0] for row in conn.execute("SELECT ticker FROM tickers").fetchall()
    }
    session = get_session(browser_like=True)

    end = as_of + timedelta(days=days_ahead)
    days = trading_days(as_of, end)

    rows: list[tuple] = []
    for day in days:
        try:
            resp = session.get(
                NASDAQ_EARNINGS_URL, params={"date": day.isoformat()}, timeout=15
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 -- one bad day must not abort the run
            log.warning("failed to fetch Nasdaq earnings calendar for %s: %s", day, exc)
            continue

        entries = ((payload or {}).get("data") or {}).get("rows") or []
        for entry in entries:
            symbol = (entry.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if known_tickers and symbol not in known_tickers:
                continue
            timing = _map_timing(entry.get("time"))
            rows.append((symbol, day.isoformat(), timing, "nasdaq_calendar", None))

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO earnings_dates "
            "(ticker, event_date, timing, source, acceptance_datetime) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    log.info(
        "update_upcoming_earnings: as_of=%s days_ahead=%s -> %d rows across %d trading days",
        as_of, days_ahead, len(rows), len(days),
    )
