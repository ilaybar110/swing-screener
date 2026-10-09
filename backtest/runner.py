"""Local backtest runner (docs/PLAN.md section 15). Never used in the cloud routine.

``run(start, end, notes)`` replays the real pipeline over history on research.db:

1. a ``backtest_runs`` row stores the config snapshot and its hash;
2. weekly universe snapshots are filled in where missing, from
   ``universe.build_historical_universe`` (point-in-time approximation);
3. every enabled module's ``scan_history`` produces candidates (point-in-time
   ``DataAccess``);
4. ``ranking.rank`` ranks each signal day, recommendations are stored with
   ``source = run_id`` (ids become ``<run_id>/<date>_<ticker>``);
5. baselines are drawn, and ``tracker.update_all`` simulates everything with the very
   same ``simulate()`` the live pipeline uses.

Signals are generated for [start, end]; their outcomes are tracked through
``track_until`` (default: the last price date in the database) so trades signalled
near ``end`` can finish. The ranking track-record component is not used in backtests
(it stays at 0 weight): results would otherwise depend on simulation order.

SURVIVORSHIP BIAS: the universe only contains tickers that exist today, so every
number printed here is an upper bound on real performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import numpy as np

from src.baseline import create_for
from src.config import BASE_DIR_ENV, Config, load_config
from src.contracts import Candidate, Recommendation
from src.data_access import DataAccess
from src.db import init_db
from src.stats import NOT_ENOUGH_DATA, summary, summary_by_period
from src.tracker import update_all
from src.utils.logging import get_logger

log = get_logger(__name__)

SURVIVORSHIP_WARNING = (
    "WARNING: survivorship bias is NOT corrected - the universe contains only tickers "
    "that exist today (delisted/acquired names are missing). Treat every result below "
    "as an UPPER BOUND on real-world performance."
)

_REC_COLUMNS = [
    "id", "source", "signal_date", "ticker", "modules", "primary_module", "setup_score",
    "rs_pct", "overlap_score", "track_score", "total_score", "rank", "in_report", "entry",
    "stop", "target", "valid_until", "regime", "sector", "industry", "earnings_date",
    "earnings_in_window", "guardrail_status", "guardrail_reasons", "valuation_info",
    "rationale", "details", "llm_brief", "status", "current_r", "last_updated",
]
_JSON_COLS = {"modules", "guardrail_reasons", "valuation_info", "details", "llm_brief"}


# ---------------------------------------------------------------------------
# collaborators (other bots' code, with local stand-ins if it is missing)
# ---------------------------------------------------------------------------


def _get_modules(config: Config) -> list[Any]:
    try:
        from src.modules import get_enabled_modules
    except ImportError:
        log.warning("src.modules not available: backtest has no strategy modules")
        return []
    return get_enabled_modules(config)


def _rank_day(
    cands: list[Candidate], day: date, data: DataAccess, conn: sqlite3.Connection,
    config: Config, source: str,
) -> list[Recommendation]:
    try:
        from src.ranking import rank
    except ImportError:
        return _fallback_rank(cands, day, data, source)
    return rank(cands, day, data, conn, config=config, source=source)


def _fallback_rank(
    cands: list[Candidate], day: date, data: DataAccess, source: str
) -> list[Recommendation]:
    """Stand-in used only if src/ranking.py is missing: one rec per ticker, highest
    setup score primary, ranked by setup score."""
    by_ticker: dict[str, list[Candidate]] = {}
    for c in cands:
        by_ticker.setdefault(c.ticker, []).append(c)
    regime_row = data.get_regime(day)
    recs = []
    for t, cs in sorted(by_ticker.items()):
        cs.sort(key=lambda c: (-c.setup_score, c.module))
        p = cs[0]
        risk = p.entry - p.stop
        recs.append(Recommendation(
            id=f"{day.isoformat()}_{t}", source=source, signal_date=day, ticker=t,
            modules=[c.module for c in cs], primary_module=p.module, setup_score=p.setup_score,
            rs_pct=0.0, overlap_score=0.0, track_score=0.0, total_score=p.setup_score, rank=None,
            in_report=False, entry=p.entry, stop=p.stop, target=p.entry + 2 * risk,
            valid_until=day, regime=regime_row["regime"] if regime_row else "Unknown",
            sector=None, industry=None, earnings_date=None, earnings_in_window=False,
            guardrail_status="unknown", guardrail_reasons=[], valuation_info={},
            rationale=p.rationale, details=dict(p.details), llm_brief=None, status="pending",
            current_r=None, last_updated="",
        ))
    recs.sort(key=lambda r: (-r.total_score, r.ticker))
    for i, r in enumerate(recs, start=1):
        r.rank = i
    return recs


def _historical_universe(data: DataAccess, day: date) -> Any:
    try:
        from src.universe import build_historical_universe
    except ImportError:
        log.warning("src.universe not available: cannot build a historical universe")
        return None
    return build_historical_universe(data, day)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


def config_snapshot(config: Config) -> tuple[str, str]:
    """(canonical JSON snapshot, sha256 hash) of the effective config."""
    snap = json.dumps(config.model_dump(mode="json"), sort_keys=True, default=str)
    return snap, hashlib.sha256(snap.encode("utf-8")).hexdigest()


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, np.generic):
        return o.item()
    return str(o)


def insert_recommendations(conn: sqlite3.Connection, recs: list[Recommendation]) -> int:
    """INSERT OR IGNORE recommendation rows (a backtest run_id is unique, so a re-run
    never double counts). Returns the number of rows written."""
    rows = []
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for r in recs:
        row = []
        for col in _REC_COLUMNS:
            v = getattr(r, col)
            if col in _JSON_COLS:
                v = None if v is None else json.dumps(v, sort_keys=True, default=_json_default)
            elif isinstance(v, bool):
                v = int(v)
            elif isinstance(v, date):
                v = v.isoformat()
            elif isinstance(v, np.generic):
                v = v.item()
            if col == "last_updated" and not v:
                v = stamp
            row.append(v)
        rows.append(tuple(row))
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO recommendations ({', '.join(_REC_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_REC_COLUMNS))})", rows)
    conn.commit()
    return conn.total_changes - before


def ensure_weekly_universe(
    conn: sqlite3.Connection, start: date, end: date, max_age_days: int = 10
) -> int:
    """Fill ``universe_snapshots`` with a point-in-time approximation for every week of
    [start, end] that has no snapshot within ``max_age_days``. Returns snapshots added."""
    from src.utils.calendar import trading_days

    data = DataAccess(conn)
    added = 0
    last_in_week: dict[tuple[int, int], date] = {}
    for d in trading_days(start, end):
        last_in_week[tuple(d.isocalendar()[:2])] = d
    for day in sorted(last_in_week.values()):
        existing = data.get_universe(day)
        if len(existing):
            newest = date.fromisoformat(str(existing["snapshot_date"].iloc[0]))
            if newest >= day - timedelta(days=max_age_days):
                continue
        uni = _historical_universe(data, day)
        if uni is None or len(uni) == 0:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
            "VALUES (?,?,?,?,?)",
            [(day.isoformat(), r.ticker, r.price, r.market_cap, r.adv20) for r in uni.itertuples()],
        )
        conn.commit()
        added += 1
    return added


def format_report(
    run_id: str, start: date, end: date, by_period: dict[str, dict[str, dict[str, Any]]],
    warnings: dict[str, str],
) -> str:
    """Per-module results per period as printable text, survivorship warning first."""
    def f(v: Any, fmt: str) -> str:
        return "-" if v is None else format(v, fmt)

    lines = [SURVIVORSHIP_WARNING, "", f"Backtest {run_id}: signals {start} .. {end}", ""]
    hdr = f"{'module':<24}{'recs':>6}{'closed':>8}{'win%':>7}{'avgR':>8}{'medR':>8}{'PF':>7}{'vs base R':>11}"
    for label, groups in by_period.items():
        lines += [f"== {label} ==", hdr]
        for name, m in groups.items():
            small = " *" if name != "ALL" and m["count"] < 30 else ""
            win = None if m["win_rate"] is None else m["win_rate"] * 100
            lines.append(
                f"{name:<24}{m.get('recommendations', 0):>6}{m['count']:>8}"
                f"{f(win, '.1f'):>7}{f(m['avg_r'], '.2f'):>8}{f(m['median_r'], '.2f'):>8}"
                f"{f(m['profit_factor'], '.2f'):>7}{f(m['excess_vs_baseline_r'], '+.2f'):>11}{small}"
            )
        lines.append("")
    if not by_period:
        lines += ["(no recommendations generated)", ""]
    lines.append(f"* = {NOT_ENOUGH_DATA} (< 30 closed trades)")
    for m, w in warnings.items():
        lines.append(f"  {m}: {w}")
    return "\n".join(lines)


def run(
    start: date,
    end: date,
    notes: str = "",
    *,
    conn: Optional[sqlite3.Connection] = None,
    config: Optional[Config] = None,
    modules: Optional[list[Any]] = None,
    track_until: Optional[date] = None,
    build_universe: bool = True,
    print_fn: Optional[Callable[[str], None]] = print,
) -> str:
    """Run a backtest over signal dates [start, end]; returns the run_id.

    ``conn`` defaults to research.db from the config (created/migrated if needed);
    ``modules`` overrides the enabled strategy modules (used by tests)."""
    config = config or load_config()
    own_conn = conn is None
    if conn is None:
        conn = init_db(config.paths.research_db)
    conn.row_factory = sqlite3.Row
    snap, chash = config_snapshot(config)
    created = datetime.now(timezone.utc)
    run_id = f"bt_{created.strftime('%Y%m%dT%H%M%S%f')}_{chash[:8]}"
    conn.execute(
        "INSERT INTO backtest_runs (run_id, created_at, start_date, end_date, config_hash, "
        "config_snapshot, notes) VALUES (?,?,?,?,?,?,?)",
        (run_id, created.isoformat(), start.isoformat(), end.isoformat(), chash, snap, notes),
    )
    conn.commit()
    log.info("backtest %s: %s..%s", run_id, start, end)

    data = DataAccess(conn)
    if build_universe:
        log.info("historical universe: %d weekly snapshot(s) added",
                 ensure_weekly_universe(conn, start, end))

    candidates: list[Candidate] = []
    for module in (modules if modules is not None else _get_modules(config)):
        found = module.scan_history(start, end, data)
        log.info("module %s: %d candidates", getattr(module, "name", "?"), len(found))
        candidates.extend(found)

    by_day: dict[date, list[Candidate]] = {}
    for c in candidates:
        by_day.setdefault(c.signal_date, []).append(c)
    recs: list[Recommendation] = []
    for day in sorted(by_day):
        day_recs = _rank_day(by_day[day], day, data, conn, config, run_id)
        for r in day_recs:
            # ids are primary keys across all sources, so namespace them per run
            r.source = run_id
            r.id = f"{run_id}/{r.id}"
        # stored day by day: the next day's repeat-signal rule (src.repeats) looks for
        # earlier recommendations of the same run that are still PENDING/ACTIVE
        insert_recommendations(conn, day_recs)
        recs.extend(day_recs)
    log.info("%d recommendations stored", len(recs))

    if track_until is None:
        row = conn.execute("SELECT MAX(date) AS d FROM prices WHERE ticker = 'SPY'").fetchone()
        track_until = date.fromisoformat(row["d"]) if row and row["d"] else end
    track_until = max(track_until, end)

    create_for(conn, recs, track_until, config)
    update_all(conn, track_until, run_id, config)

    if print_fn:
        s = summary(conn, run_id)
        print_fn(format_report(run_id, start, end, summary_by_period(conn, run_id), s["warnings"]))
    if own_conn:
        conn.close()
    return run_id


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a local backtest on research.db")
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--notes", default="")
    ap.add_argument("--base-dir", type=Path, help="root holding data/research.db (default: the repo)")
    a = ap.parse_args()
    if a.base_dir:
        os.environ[BASE_DIR_ENV] = str(a.base_dir)
    run(a.start, a.end, a.notes)


if __name__ == "__main__":
    main()
