"""Fixed-horizon forward returns for recommendations and their baselines.

Complements the trade simulation in ``src/tracker.py``: instead of following the trade
plan (buy-stop, stop, target, time stop), it asks the plain question "what did the
stock do after the signal?" for every recommendation -- filled or not -- and for every
one of its baseline (random-ticker) samples, at fixed horizons of
``measurement.forward_horizons`` trading days (default 5/10/20/40/60).

Conventions (point-in-time, deterministic):

* Start = the open of the first bar strictly after ``signal_date`` (the same "next
  open" the baselines enter at). Horizon ``h`` ends at the close of the ``h``-th bar
  counted from that start bar (the start bar is bar 1), so the 5-day return covers
  five sessions: ``close[i0 + h - 1] / open[i0] - 1``. No costs are charged.
* SPY and the sector ETF are measured over the same calendar window: open of their
  first bar on/after the start date to the close of their last bar on/before the end
  date. Excess = stock return minus benchmark return.
* The sector ETF of a recommendation comes from ``tracking.sector_etfs[rec.sector]``
  (as in the tracker); a baseline uses its own ticker's sector from ``tickers``.
* Only bars dated <= ``as_of`` are used. A (subject, horizon) row is written once,
  when it is complete -- the stock has its ``h``-th bar and SPY has a bar on/after
  the end date -- and never recomputed (``INSERT OR IGNORE``), exactly like final
  tracker results. Incomplete horizons are simply re-tried on the next run.

Excess versus a recommendation's *own* baselines is not stored; ``src/stats.py``
pairs each recommendation with the mean of its baselines at the same horizon.

The table is created on demand by ``ensure_table`` (it is not part of the original
``src/db.py`` schema) and persisted month-sharded by signal date in
``state/forward_returns/YYYY-MM.csv`` through ``src/state_io.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from src.config import Config, load_config
from src.tracker import (
    BENCHMARK,
    PriceCache,
    _bench_return,
    _Bars,
    _split_factor_and_normalise,
    _to_date,
)
from src.utils.logging import get_logger

log = get_logger(__name__)

TABLE = "forward_returns"

FIELDS = [
    "subject_id", "horizon", "kind", "parent_rec_id", "source", "signal_date", "ticker",
    "start_date", "end_date", "start_price", "end_price", "ret", "spy_return",
    "sector_etf", "sector_etf_return", "excess_vs_spy", "excess_vs_sector",
]

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    subject_id TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    kind TEXT NOT NULL,
    parent_rec_id TEXT NOT NULL,
    source TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    start_price REAL,
    end_price REAL,
    ret REAL,
    spy_return REAL,
    sector_etf TEXT,
    sector_etf_return REAL,
    excess_vs_spy REAL,
    excess_vs_sector REAL,
    PRIMARY KEY (subject_id, horizon)
);
CREATE INDEX IF NOT EXISTS idx_forward_returns_parent ON {TABLE}(parent_rec_id);
CREATE INDEX IF NOT EXISTS idx_forward_returns_source ON {TABLE}(source, signal_date);
"""

_WARMUP_DAYS = 5  # only the next open matters; no indicator warm-up needed


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the forward_returns table if it does not exist (idempotent)."""
    conn.executescript(_DDL)


def table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# pure computation
# ---------------------------------------------------------------------------


def compute(
    bars: _Bars,
    signal_date: date,
    horizons: list[int],
    spy: Optional[_Bars] = None,
    sector: Optional[_Bars] = None,
) -> dict[int, dict[str, Any]]:
    """Forward returns of one subject for every *complete* horizon.

    ``bars`` must already be limited to the point-in-time window (dates <= as_of).
    Returns {horizon: row fields (start/end date and price, ret, spy_return,
    sector_etf_return, excess_vs_spy, excess_vs_sector)}; horizons that are not yet
    complete (stock bar missing, or SPY not yet covering the end date) are absent.
    """
    out: dict[int, dict[str, Any]] = {}
    i0 = bars.index_after(signal_date)
    if i0 >= bars.n:
        return out
    start_px = float(bars.o[i0])
    if not start_px > 0:
        return out
    start_d = bars.dates[i0]
    for h in sorted(set(int(x) for x in horizons)):
        if h <= 0:
            continue
        i1 = i0 + h - 1
        if i1 >= bars.n:
            break
        end_d = bars.dates[i1]
        end_px = float(bars.c[i1])
        if spy is None or not spy.n or spy.dates[-1] < end_d:
            break  # benchmark not yet known up to the end date: try again next run
        ret = end_px / start_px - 1.0
        spy_r = _bench_return(spy, start_d, end_d, exit_at_open=False)
        sec_r = _bench_return(sector, start_d, end_d, exit_at_open=False) if sector else None
        out[h] = {
            "start_date": start_d.isoformat(), "end_date": end_d.isoformat(),
            "start_price": start_px, "end_price": end_px, "ret": ret,
            "spy_return": spy_r, "sector_etf_return": sec_r,
            "excess_vs_spy": None if spy_r is None else ret - spy_r,
            "excess_vs_sector": None if sec_r is None else ret - sec_r,
        }
    return out


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def _subjects(conn: sqlite3.Connection, source: str, as_of: date, n_h: int) -> list[dict[str, Any]]:
    """Recommendations and baselines of ``source`` signalled on/before ``as_of`` that
    still miss at least one horizon row."""
    as_of_s = as_of.isoformat()
    missing = (
        f"(SELECT COUNT(*) FROM {TABLE} f WHERE f.subject_id = {{id}}) < {int(n_h)}"
    )
    recs = conn.execute(
        "SELECT r.id AS subject_id, 'rec' AS kind, r.id AS parent_rec_id, r.signal_date, "
        "r.ticker, r.sector, t.sector_etf AS ticker_etf FROM recommendations r "
        "LEFT JOIN tickers t ON t.ticker = r.ticker "
        f"WHERE r.source = ? AND r.signal_date <= ? AND {missing.format(id='r.id')} ",
        (source, as_of_s),
    ).fetchall()
    base = conn.execute(
        "SELECT b.id AS subject_id, 'baseline' AS kind, b.parent_rec_id, b.signal_date, "
        "b.ticker, t.sector AS sector, t.sector_etf AS ticker_etf FROM baselines b "
        "LEFT JOIN tickers t ON t.ticker = b.ticker "
        f"WHERE b.source = ? AND b.signal_date <= ? AND {missing.format(id='b.id')} ",
        (source, as_of_s),
    ).fetchall()
    rows = [dict(r) for r in recs] + [dict(r) for r in base]
    rows.sort(key=lambda r: (str(r["signal_date"]), str(r["subject_id"])))
    return rows


def _etf_for(row: dict[str, Any], config: Config) -> Optional[str]:
    sector = row.get("sector")
    etf = config.tracking.sector_etfs.get(sector) if sector else None
    return etf or (row.get("ticker_etf") or None)


def update_forward_returns(
    conn: sqlite3.Connection, as_of: date, source: str, config: Optional[Config] = None
) -> int:
    """Compute and insert every newly complete forward-return row of ``source`` on
    prices up to ``as_of``. Idempotent; complete rows are never rewritten. Split
    normalisation of raw histories (see ``tracker._split_factor_and_normalise``) is
    applied for ``source == "live"`` only, as in the tracker. Returns rows inserted."""
    config = config or load_config()
    horizons = sorted(set(int(h) for h in config.measurement.forward_horizons if int(h) > 0))
    if not horizons:
        return 0
    ensure_table(conn)
    conn.row_factory = sqlite3.Row
    subjects = _subjects(conn, source, as_of, len(horizons))
    if not subjects:
        return 0
    have: dict[str, set[int]] = {}
    for r in conn.execute(
        f"SELECT subject_id, horizon FROM {TABLE} WHERE source = ?", (source,)
    ):
        have.setdefault(r["subject_id"], set()).add(int(r["horizon"]))

    first = min(_to_date(s["signal_date"]) for s in subjects)
    cache = PriceCache(conn, first - timedelta(days=_WARMUP_DAYS), as_of)
    bars_cache: dict[str, Optional[_Bars]] = {}

    def bars_for(ticker: Optional[str], normalise: bool) -> Optional[_Bars]:
        if not ticker:
            return None
        if ticker not in bars_cache:
            df = cache.get(ticker)
            b = _Bars(df) if len(df) else None
            if b is not None and normalise:
                splits = conn.execute(
                    "SELECT date, ratio FROM splits WHERE ticker = ? ORDER BY date", (ticker,)
                ).fetchall()
                # signal date irrelevant here: only the in-place normalisation is used
                _split_factor_and_normalise(
                    b, [(_to_date(d), float(r)) for d, r in splits], date.max
                )
            bars_cache[ticker] = b
        return bars_cache[ticker]

    spy = bars_for(BENCHMARK, False)
    inserted = 0
    for s in subjects:
        stock = bars_for(s["ticker"], source == "live")
        if stock is None:
            continue
        etf = _etf_for(s, config)
        res = compute(stock, _to_date(s["signal_date"]), horizons, spy, bars_for(etf, False))
        done = have.get(s["subject_id"], set())
        for h, fields in res.items():
            if h in done:
                continue
            row = {
                "subject_id": s["subject_id"], "horizon": h, "kind": s["kind"],
                "parent_rec_id": s["parent_rec_id"], "source": source,
                "signal_date": str(s["signal_date"]), "ticker": s["ticker"],
                "sector_etf": etf, **fields,
            }
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {TABLE} ({', '.join(FIELDS)}) "
                f"VALUES ({', '.join('?' * len(FIELDS))})",
                tuple(row[f] for f in FIELDS),
            )
            inserted += cur.rowcount
    conn.commit()
    log.info("forward_returns(%s, %s): %d rows inserted for %d subjects",
             source, as_of.isoformat(), inserted, len(subjects))
    return inserted


def load(conn: sqlite3.Connection, source: str) -> pd.DataFrame:
    """All forward-return rows of ``source`` (empty frame with FIELDS columns when the
    table does not exist yet). Read-only."""
    if not table_exists(conn):
        return pd.DataFrame(columns=FIELDS)
    return pd.read_sql_query(
        f"SELECT {', '.join(FIELDS)} FROM {TABLE} WHERE source = ? ORDER BY subject_id, horizon",
        conn, params=(source,),
    )
