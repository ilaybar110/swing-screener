"""Random-ticker control group (docs/PLAN.md section 13).

For every recommendation, ``samples_per_recommendation`` tickers are drawn (seeded,
deterministic) from the universe in effect on the signal day, excluding the
recommendation's own ticker. Each baseline enters at the next session's open with
the same stop distance in percent, and is exited by the same rules via
``tracker.simulate(entry_mode="open")``. Rows live in `baselines` and are advanced
by ``tracker.update_all`` (which calls ``update_baselines``).
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from typing import Optional

from src.config import Config, load_config
from src.contracts import Recommendation
from src.data_access import DataAccess
from src.tracker import PriceCache, simulate
from src.utils.logging import get_logger

log = get_logger(__name__)

_WARMUP_DAYS = 60


def _draw(rec: Recommendation, universe: list[str], k: int, seed: int) -> list[str]:
    pool = sorted(t for t in set(universe) if t != rec.ticker)
    # string seeds hash with sha512 -> identical across runs, platforms, versions
    # keyed on date+ticker (not rec.id, which backtests namespace per run) so the
    # control group is reproducible across runs
    rng = random.Random(f"{seed}:{rec.signal_date.isoformat()}:{rec.ticker}")
    return rng.sample(pool, min(k, len(pool)))


def create_for(
    conn: sqlite3.Connection,
    recs: list[Recommendation],
    as_of: date,
    config: Optional[Config] = None,
) -> int:
    """Insert baseline rows for ``recs`` (those with signal_date <= as_of).
    Idempotent: existing baseline ids are left untouched. Returns rows inserted."""
    config = config or load_config()
    k = config.baseline.samples_per_recommendation
    seed = config.baseline.random_seed
    data = DataAccess(conn)
    inserted = 0
    for rec in recs:
        if rec.signal_date > as_of:
            continue
        uni = data.get_universe(rec.signal_date)
        if uni.empty:
            log.warning("no universe snapshot for %s; no baselines for %s", rec.signal_date, rec.id)
            continue
        stop_pct = (rec.entry - rec.stop) / rec.entry
        for n, ticker in enumerate(_draw(rec, list(uni["ticker"]), k, seed), start=1):
            cur = conn.execute(
                "INSERT OR IGNORE INTO baselines (id, parent_rec_id, sample_no, source, "
                "signal_date, ticker, stop_pct, status) VALUES (?,?,?,?,?,?,?,?)",
                (f"{rec.id}_B{n}", rec.id, n, rec.source, rec.signal_date.isoformat(),
                 ticker, stop_pct, "pending"),
            )
            inserted += cur.rowcount
    conn.commit()
    log.info("baseline.create_for: %d rows inserted for %d recs", inserted, len(recs))
    return inserted


def update_baselines(
    conn: sqlite3.Connection, as_of: date, source: str, config: Optional[Config] = None
) -> tuple[int, int]:
    """Re-simulate every non-final baseline of ``source`` up to ``as_of``. Final rows
    are written once and skipped thereafter. Returns (simulated, newly_final)."""
    config = config or load_config()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM baselines WHERE source = ? AND signal_date <= ? "
        "AND COALESCE(status,'') NOT IN ('stopped','target_hit','time_stop','expired') "
        "ORDER BY signal_date, id",
        (source, as_of.isoformat()),
    ).fetchall()
    if not rows:
        return 0, 0
    first = date.fromisoformat(min(r["signal_date"] for r in rows))
    cache = PriceCache(conn, first - timedelta(days=_WARMUP_DAYS), as_of)
    newly_final = 0
    for row in rows:
        sig = date.fromisoformat(row["signal_date"])
        frame = cache.frame_for([row["ticker"], "SPY"], sig)
        stock = frame[frame["ticker"] == row["ticker"]]
        after = stock[stock["date"] > sig.isoformat()]
        if after.empty:
            continue  # next open not yet known: stays pending
        entry = float(after.iloc[0]["open"])
        pseudo = _pseudo_rec(row, entry)
        res = simulate(pseudo, frame, None, config, entry_mode="open")
        if res.is_final:
            conn.execute(
                "UPDATE baselines SET status=?, r_multiple=?, pct_return=?, exit_date=?, "
                "exit_reason=? WHERE id=?",
                (res.status, res.r_multiple, res.pct_return,
                 res.exit_date.isoformat() if res.exit_date else None, res.exit_reason, row["id"]),
            )
            newly_final += 1
        else:
            conn.execute("UPDATE baselines SET status=? WHERE id=?", (res.status, row["id"]))
    conn.commit()
    return len(rows), newly_final


def _pseudo_rec(row: sqlite3.Row, entry: float) -> Recommendation:
    stop = entry * (1.0 - row["stop_pct"])
    target = entry + 2.0 * (entry - stop)  # recomputed in simulate(); placeholder
    sig = date.fromisoformat(row["signal_date"])
    return Recommendation(
        id=row["id"], source=row["source"], signal_date=sig, ticker=row["ticker"],
        modules=[], primary_module="baseline", setup_score=0, rs_pct=0, overlap_score=0,
        track_score=0, total_score=0, rank=None, in_report=False, entry=entry, stop=stop,
        target=target, valid_until=sig, regime="", sector=None, industry=None,
        earnings_date=None, earnings_in_window=False, guardrail_status="unknown",
        guardrail_reasons=[], valuation_info={}, rationale="", details={}, llm_brief=None,
        status="pending", current_r=None, last_updated="",
    )
