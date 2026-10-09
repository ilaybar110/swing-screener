"""Repeat signals (decision D3).

A ticker that already has a recommendation still PENDING (entry order working) or ACTIVE
(``open``: filled, not yet closed) does not get a second recommendation. The repeat
signal is recorded in the existing recommendation's ``details["repeat_signals"]`` -- a
list of ``{"date": "YYYY-MM-DD", "modules": [...]}`` -- and nothing new is created or
tracked. Once the earlier recommendation is final (stopped / target_hit / time_stop /
expired) the ticker may be recommended again.

"Still live" is decided by re-simulating the earlier recommendation on prices up to the
signal date (point-in-time), not by the stored ``status`` column: during a multi-day
catch-up, and during a backtest, the tracker has not run yet for the days in between, so
the stored status can be stale. Live runs and backtests share this code path so their
statistics stay comparable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Optional

from src.config import Config
from src.contracts import Candidate, Recommendation
from src.utils.logging import get_logger

log = get_logger(__name__)

REPEAT_KEY = "repeat_signals"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def find_live_recommendation(
    conn: sqlite3.Connection, ticker: str, as_of: date, config: Config, source: str
) -> Optional[Recommendation]:
    """The earlier recommendation of ``ticker`` (same ``source``, signalled before
    ``as_of``) that is still PENDING or ACTIVE as of ``as_of``, or None."""
    from src.tracker import PriceCache, _load_splits, row_to_rec, simulate

    rows = _rows(
        conn,
        "SELECT r.* FROM recommendations r LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = ? AND r.ticker = ? AND r.signal_date < ? AND x.rec_id IS NULL "
        "ORDER BY r.signal_date DESC, r.id",
        (source, ticker, as_of.isoformat()),
    )
    for row in rows:
        rec = row_to_rec(row)
        cache = PriceCache(conn, rec.signal_date - timedelta(days=60), as_of)
        frame = cache.frame_for([ticker], rec.signal_date)
        splits = _load_splits(conn, ticker) if source == "live" else None
        if not simulate(rec, frame, splits, config).is_final:
            return rec
    return None


def record_repeat(
    conn: sqlite3.Connection, rec: Recommendation, as_of: date, candidates: list[Candidate]
) -> None:
    """Append the repeat signal (date + modules) to ``rec.details`` and persist it.
    Idempotent per date: re-processing a day replaces that day's entry."""
    details = dict(rec.details or {})
    repeats = [r for r in details.get(REPEAT_KEY, []) if r.get("date") != as_of.isoformat()]
    repeats.append({"date": as_of.isoformat(), "modules": sorted({c.module for c in candidates})})
    repeats.sort(key=lambda r: r["date"])
    details[REPEAT_KEY] = repeats
    rec.details = details
    conn.execute(
        "UPDATE recommendations SET details = ? WHERE id = ?",
        (json.dumps(details, sort_keys=True, default=str), rec.id),
    )
    conn.commit()


def split_repeats(
    by_ticker: dict[str, list[Candidate]],
    conn: sqlite3.Connection,
    as_of: date,
    config: Config,
    source: str,
) -> dict[str, list[Candidate]]:
    """Remove from ``by_ticker`` every ticker that already has a live recommendation,
    recording the repeat signal on that recommendation. Returns the remaining tickers."""
    fresh: dict[str, list[Candidate]] = {}
    for ticker, cands in by_ticker.items():
        live = find_live_recommendation(conn, ticker, as_of, config, source)
        if live is None:
            fresh[ticker] = cands
            continue
        record_repeat(conn, live, as_of, cands)
        log.info("repeat signal %s %s: recommendation %s (signalled %s) is still live",
                 ticker, as_of, live.id, live.signal_date)
    return fresh
