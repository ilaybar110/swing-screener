"""Weekly job (cloud routine, Sundays): universe refresh, fundamentals summary, upcoming
earnings for the next 35 trading days, then export state.

    python run_weekly.py [--date YYYY-MM-DD] [--dry-run] [--limit N]

--dry-run: nothing is written to state/ and no alert is sent (the scratch DB
data/run_dry.db is still used). --limit N: only the N largest listed companies are
considered (trial runs).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src import universe
from src.config import BASE_DIR_ENV, Config, load_config
from src.data import earnings, fundamentals, prices
from src.db import init_db
from src.jobs import (
    BENCHMARK, CriticalError, JobLog, latest_completed_session, new_run_id, price_message,
    prune_job_log, sector_etfs,
)
from src.state_io import export_state, import_state
from src.utils.calendar import add_trading_days
from src.utils.logging import get_logger, setup_logging

log = get_logger("run_weekly")

EARNINGS_TRADING_DAYS_AHEAD = 35
PRICE_HISTORY_CALENDAR_DAYS = 440  # >= 300 trading days, enough for 126-day history + 52w highs


def open_db(cfg: Config, *, dry_run: bool):
    """Fresh scratch DB built from state/ (the cloud run starts from a clean clone)."""
    path = Path(cfg.paths.run_db)
    if dry_run:
        path = path.with_name(path.stem + "_dry.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = init_db(path)
    import_state(conn, Path(cfg.paths.state_dir))
    return conn


def candidate_tickers(cfg: Config, limit: Optional[int] = None) -> tuple[pd.DataFrame, str]:
    """Listed companies worth downloading prices for: not excluded by name/SIC, market
    cap and price above the universe minimums (when the source reports them)."""
    df, source = universe._fetch_universe_source(cfg)
    keep = []
    for r in df.itertuples():
        if universe._is_excluded(r.name, r.sic, cfg):
            continue
        if source == "nasdaq":
            if r.market_cap is None or pd.isna(r.market_cap) or r.market_cap < cfg.universe.min_market_cap:
                continue
            if r.price is not None and not pd.isna(r.price) and r.price < cfg.universe.min_price:
                continue
        keep.append(r.Index)
    out = df.loc[keep]
    if "market_cap" in out:
        out = out.sort_values("market_cap", ascending=False, na_position="last")
    if limit:
        out = out.head(limit)
    return out.reset_index(drop=True), source


def refresh_universe_job(conn, cfg: Config, as_of: date, jl: JobLog, *, limit: Optional[int] = None) -> dict:
    """Download prices for every plausible universe member, then apply the PLAN section 5
    filters and save the weekly snapshot. Raises CriticalError when it cannot work."""
    cand, source = candidate_tickers(cfg, limit)
    if cand.empty:
        raise CriticalError("universe source returned no candidate tickers")
    tickers = sorted(set(cand["ticker"]) | {BENCHMARK, *sector_etfs(cfg)})
    log.info("universe source=%s, downloading prices for %d tickers", source, len(tickers))
    start = as_of - timedelta(days=PRICE_HISTORY_CALENDAR_DAYS)
    rep = jl.run("prices", lambda: prices.update_prices(conn, tickers, start, as_of), as_of,
                 message_fn=price_message)
    if not rep.ok:
        raise CriticalError(f"price download failed: {rep.error}")
    if conn.execute("SELECT COUNT(*) FROM prices WHERE ticker = ?", (BENCHMARK,)).fetchone()[0] == 0:
        raise CriticalError("no SPY prices could be downloaded")
    res = jl.run("universe", lambda: universe.refresh_universe(conn, as_of), as_of, critical=True)
    size = conn.execute(
        "SELECT COUNT(*) FROM universe_snapshots WHERE snapshot_date = ?", (as_of.isoformat(),)
    ).fetchone()[0]
    if size == 0:
        raise CriticalError("universe refresh produced an empty universe")
    if limit:  # trial runs: the snapshot is only meaningful for the limited ticker set
        log.info("trial run (--limit %d): universe has %d tickers", limit, size)
    return {"candidates": len(cand), "universe": size, "prices": rep.value, "source": source, "ok": res.ok}


def run(cfg: Config, *, as_of: Optional[date] = None, dry_run: bool = False,
        limit: Optional[int] = None, now: Optional[datetime] = None, conn=None) -> int:
    run_id = new_run_id(now)
    as_of = as_of or latest_completed_session(now)
    owns_conn = conn is None
    conn = conn or open_db(cfg, dry_run=dry_run)
    jl = JobLog(conn, run_id)
    summary: dict = {}
    exit_code = 0
    try:
        summary["universe"] = refresh_universe_job(conn, cfg, as_of, jl, limit=limit)

        jl.run("fundamentals_summary", lambda: fundamentals.update_summary(conn, as_of), as_of,
               message_fn=lambda r: f"universe={r['universe_size']} requests={r['requests_made']}")
        end = add_trading_days(as_of, EARNINGS_TRADING_DAYS_AHEAD)
        jl.run("earnings_calendar",
               lambda: earnings.update_upcoming_earnings(conn, as_of, (end - as_of).days), as_of)
    except CriticalError as exc:
        log.error("CRITICAL: %s", exc)
        _alert(f"swing-screener weekly run {as_of}: {exc}", dry_run)
        exit_code = 2
    except Exception as exc:  # noqa: BLE001
        log.exception("weekly run failed")
        _alert(f"swing-screener weekly run {as_of} crashed: {type(exc).__name__}: {exc}", dry_run)
        exit_code = 1
    finally:
        if not dry_run:
            prune_job_log(conn, as_of)
            export_state(conn, Path(cfg.paths.state_dir), as_of)

    errors = [e for e in jl.errors]
    n_sum = conn.execute("SELECT COUNT(*) FROM fundamentals_summary WHERE as_of = ?",
                         (as_of.isoformat(),)).fetchone()[0]
    n_earn = conn.execute("SELECT COUNT(*) FROM earnings_dates WHERE source = 'nasdaq_calendar' "
                          "AND event_date >= ?", (as_of.isoformat(),)).fetchone()[0]
    u = summary.get("universe", {})
    print(f"weekly run {as_of}{' (dry run)' if dry_run else ''}: "
          f"universe {u.get('universe', 'n/a')} tickers (source {u.get('source', 'n/a')}), "
          f"fundamentals summary {n_sum} rows, upcoming earnings {n_earn} rows, "
          f"{len(errors)} non-critical error(s)"
          + ("" if exit_code == 0 else f", FAILED (exit {exit_code})"))
    for e in errors:
        print(f"  - {e}")
    if owns_conn:
        conn.close()
    return exit_code


def _alert(text: str, dry_run: bool) -> None:
    if dry_run:
        log.info("dry run: alert not sent: %s", text)
        return
    from notify import telegram

    telegram.send_alert(text)


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, help="as-of trading day (default: latest completed session)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing to state/, send nothing")
    ap.add_argument("--limit", type=int, help="only the N largest companies (trial runs)")
    ap.add_argument("--base-dir", type=Path, help="root holding state/, reports/, data/, work/ (default: the repo; "
                                                  "use a temporary copy for trial runs)")
    args = ap.parse_args(argv)
    if args.base_dir:
        os.environ[BASE_DIR_ENV] = str(args.base_dir)
    cfg = load_config()
    setup_logging(Path(cfg.paths.logs_dir))
    return run(cfg, as_of=args.date, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
