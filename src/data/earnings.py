"""Upcoming earnings calendar, sourced from Nasdaq's earnings calendar API.

Historical/recent earnings events (derived from 8-K Item 2.02 filings) are owned by
Bot 3 (src/data/edgar_daily.py); this module only handles the forward-looking
calendar (docs/PLAN.md section 4.5).

Refresh semantics (``update_upcoming_earnings``):
  * Nasdaq moves report dates. A refresh therefore *replaces* the calendar rows of every
    fetched future day (one that returned a non-empty listing) instead of only upserting,
    and a ticker listed on a new date supersedes its other future calendar rows inside
    the fetched window that lie within ``SUPERSEDE_WINDOW_DAYS`` of it (a company reports
    once a quarter). Without this the stale earlier date stayed in the table and
    ``get_upcoming_earnings`` returned it.
  * ``known_date`` is the first ``as_of`` on which a fetch listed the (ticker, date) pair
    and survives refreshes, so point-in-time readers (backtests over research.db) only see
    a calendar date from the day it became known. ``last_seen_date`` is the latest fetch.
  * Nasdaq's ``epsForecast`` / ``noOfEsts`` / ``lastYearEPS`` are stored, plus the reported
    ``eps`` / ``surprise`` that the API returns for days already past (a short lookback of
    ``lookback_trading_days`` sessions is re-fetched to pick those up).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from src.db import ensure_earnings_columns
from src.utils.calendar import add_trading_days, trading_days
from src.utils.http import get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
CALENDAR_SOURCE = "nasdaq_calendar"
# Past sessions re-fetched on each refresh so reported EPS / surprise get recorded.
DEFAULT_LOOKBACK_TRADING_DAYS = 2
# Another future calendar row of the same ticker this close to a newly listed date is the
# same (moved) report, not the next quarter's.
SUPERSEDE_WINDOW_DAYS = 30

_TIMING_MAP = {
    "time-pre-market": "BMO",
    "time-after-hours": "AMC",
}

_COLUMNS = (
    "ticker", "event_date", "timing", "source", "acceptance_datetime", "known_date",
    "last_seen_date", "eps_forecast", "num_estimates", "last_year_eps", "eps_actual",
    "eps_surprise_pct",
)
# Values kept from the stored row when a refresh no longer supplies them.
_STICKY = ("eps_forecast", "num_estimates", "last_year_eps", "eps_actual", "eps_surprise_pct")

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _map_timing(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _TIMING_MAP.get(raw)


def parse_number(raw: Any) -> Optional[float]:
    """Nasdaq number string -> float. Handles ``"$1.23"``, ``"($0.45)"`` (negative),
    ``"-3.2"``, ``"1,234"``; ``"N/A"``/blank/None -> None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    m = _NUM_RE.search(s)
    if not m:
        return None
    value = float(m.group(0))
    return -abs(value) if negative else value


def _parse_entry(entry: dict, day: date, as_of: date) -> Optional[dict[str, Any]]:
    symbol = (entry.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    n_est = parse_number(entry.get("noOfEsts"))
    return {
        "ticker": symbol,
        "event_date": day.isoformat(),
        "timing": _map_timing(entry.get("time")),
        "source": CALENDAR_SOURCE,
        "acceptance_datetime": None,
        "known_date": as_of.isoformat(),
        "last_seen_date": as_of.isoformat(),
        "eps_forecast": parse_number(entry.get("epsForecast")),
        "num_estimates": int(n_est) if n_est is not None else None,
        "last_year_eps": parse_number(entry.get("lastYearEPS")),
        "eps_actual": parse_number(entry.get("eps")),
        "eps_surprise_pct": parse_number(entry.get("surprise")),
    }


def update_upcoming_earnings(
    conn: sqlite3.Connection,
    as_of: date,
    days_ahead: int,
    lookback_trading_days: int = DEFAULT_LOOKBACK_TRADING_DAYS,
) -> None:
    """Pull Nasdaq's earnings calendar for [as_of, as_of + days_ahead] plus the
    ``lookback_trading_days`` sessions before as_of (one HTTP request per NYSE trading
    day) and write it into `earnings_dates` with source="nasdaq_calendar". Only tickers
    already present in `tickers` are kept, to avoid polluting the DB with symbols
    outside our universe/watchlist.

    Future days that returned a listing replace their stored calendar rows (moved dates
    disappear); past (lookback) days only add/refresh rows. ``known_date`` of a row that
    was already stored is preserved. A day whose request fails is left untouched.
    """
    ensure_earnings_columns(conn)
    known_tickers = {
        row[0] for row in conn.execute("SELECT ticker FROM tickers").fetchall()
    }
    session = get_session(browser_like=True)

    end = as_of + timedelta(days=days_ahead)
    future_days = trading_days(as_of, end)
    past_days: list[date] = []
    if lookback_trading_days > 0:
        start = add_trading_days(as_of, -lookback_trading_days)
        past_days = trading_days(start, as_of - timedelta(days=1))
    days = past_days + future_days

    fetched: dict[date, list[dict[str, Any]]] = {}
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
        rows: list[dict[str, Any]] = []
        for entry in entries:
            row = _parse_entry(entry, day, as_of)
            if row is None:
                continue
            if known_tickers and row["ticker"] not in known_tickers:
                continue
            rows.append(row)
        # An empty listing is ambiguous (no reports vs. a soft API failure): it never
        # deletes anything, but a non-empty one is authoritative for its day.
        if entries:
            fetched[day] = rows

    n_rows, n_deleted = _write(conn, as_of, end, fetched)
    log.info(
        "update_upcoming_earnings: as_of=%s days_ahead=%s -> %d rows across %d trading days "
        "(%d stale calendar rows removed)",
        as_of, days_ahead, n_rows, len(days), n_deleted,
    )


def _write(
    conn: sqlite3.Connection, as_of: date, end: date, fetched: dict[date, list[dict[str, Any]]]
) -> tuple[int, int]:
    """Apply one refresh. Returns (rows written, stale rows deleted)."""
    if not fetched:
        return 0, 0
    lo = min(min(fetched), as_of).isoformat()
    stored = {
        (r["ticker"], r["event_date"]): dict(r)
        for r in _select(conn, "SELECT * FROM earnings_dates WHERE source = ? AND event_date >= ? "
                         "AND event_date <= ?", (CALENDAR_SOURCE, lo, end.isoformat()))
    }

    future_by_ticker: dict[str, list[str]] = {}
    for ticker, event_date in stored:
        if event_date >= as_of.isoformat():
            future_by_ticker.setdefault(ticker, []).append(event_date)

    stale: set[tuple[str, str]] = set()
    for day, rows in fetched.items():
        if day < as_of:
            continue
        listed = {r["ticker"] for r in rows}
        stale.update(k for k in stored if k[1] == day.isoformat() and k[0] not in listed)
        for r in rows:
            for other in future_by_ticker.get(r["ticker"], []):
                if abs((date.fromisoformat(other) - day).days) <= SUPERSEDE_WINDOW_DAYS:
                    stale.add((r["ticker"], other))
    # a date listed by this refresh is never stale, even if another day "superseded" it
    listed_keys = {(r["ticker"], r["event_date"]) for rows in fetched.values() for r in rows}
    stale -= listed_keys

    for ticker, event_date in sorted(stale):
        conn.execute(
            "DELETE FROM earnings_dates WHERE ticker = ? AND event_date = ? AND source = ?",
            (ticker, event_date, CALENDAR_SOURCE),
        )

    out: list[tuple] = []
    for rows in fetched.values():
        for r in rows:
            old = stored.get((r["ticker"], r["event_date"]))
            if old is not None:
                if old.get("known_date"):
                    r["known_date"] = min(old["known_date"], r["known_date"])
                for k in _STICKY:
                    if r[k] is None and old.get(k) is not None:
                        r[k] = old[k]
            out.append(tuple(r[c] for c in _COLUMNS))
    if out:
        conn.executemany(
            f"INSERT OR REPLACE INTO earnings_dates ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))})",
            out,
        )
    conn.commit()
    return len(out), len(stale)


def _select(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]
