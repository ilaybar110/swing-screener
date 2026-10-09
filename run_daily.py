"""Daily job (cloud routine, Tue-Sat): two idempotent stages.

    python run_daily.py --stage prepare   # data, scans, ranking, tracking, LLM inputs, export
    (the routine agent writes work/brief_outputs/<TICKER>.json)
    python run_daily.py --stage finalize  # merge briefs, build report, export, Telegram

prepare: finds the last trading day completed in job_log and processes EVERY missed US
session up to the latest finished one, oldest first (EDGAR -> regime -> scan -> rank ->
save -> baselines), after downloading prices once for the whole range. It then updates
tracking, writes the LLM inputs for the latest day and exports state so progress
survives any later failure. It does nothing when already up to date.

finalize: merges briefs (missing/invalid ones are fine), builds the report, exports
state, sends Telegram and prints a short summary. It does nothing if that day was
already finalised.

Flags: --date YYYY-MM-DD (treat that session as the latest), --dry-run (no state/report
writes, no Telegram; scratch DB data/run_dry.db), --limit N (trial runs: download
prices for at most N universe tickers), --force (redo an up-to-date day), --stage.
Exit codes: 0 ok, 2 critical failure (alert sent), 1 unexpected crash.
"""

from __future__ import annotations

import argparse
import os
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from backtest.runner import insert_recommendations
from notify import telegram
from run_weekly import open_db, refresh_universe_job
from src import baseline, report, tracker
from src.config import BASE_DIR_ENV, Config, load_config
from src.contracts import Recommendation
from src.data import earnings, edgar_daily, prices
from src.data_access import DataAccess
from src.db import init_db
from src.jobs import (
    BENCHMARK, DAY_JOB, FINALIZE_JOB, CriticalError, JobLog, finalize_done, last_completed_day,
    latest_completed_session, live_start, missed_days, new_run_id, open_position_tickers,
    price_message, price_plan, prune_job_log,
)
from src.llm import brief_io
from src.modules import get_enabled_modules
from src.ranking import rank
from src.regime import compute_regime
from src.state_io import export_state, import_state
from src.utils.logging import get_logger, setup_logging

log = get_logger("run_daily")

UPCOMING_EARNINGS_DAYS = 7
INPUT_DIR = Path("work/brief_inputs")
OUTPUT_DIR = Path("work/brief_outputs")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _alert(text: str, dry_run: bool) -> None:
    if dry_run:
        log.info("dry run: alert not sent: %s", text)
        return
    telegram.send_alert(text)


def _db_path(cfg: Config, dry_run: bool) -> Path:
    path = Path(cfg.paths.run_db)
    return path.with_name(path.stem + "_dry.db") if dry_run else path


def _work_dirs(cfg: Config) -> tuple[Path, Path]:
    """work/ lives next to state/ and reports/ (repo root unless paths are sandboxed)."""
    root = Path(cfg.paths.state_dir).parent
    return root / INPUT_DIR, root / OUTPUT_DIR


def _export(conn, cfg: Config, as_of: date, dry_run: bool) -> None:
    if dry_run:
        log.info("dry run: state not exported")
        return
    prune_job_log(conn, as_of)
    export_state(conn, Path(cfg.paths.state_dir), as_of)
    log.info("state exported to %s", cfg.paths.state_dir)


def _stamp(recs: list[Recommendation], day: date) -> None:
    for r in recs:  # same convention tracker.update_all uses (a date, not a wall-clock time)
        r.last_updated = day.isoformat()


# ---------------------------------------------------------------------------
# stage 1: prepare
# ---------------------------------------------------------------------------


def _download_prices(conn, cfg: Config, jl: JobLog, days: list[date], limit: Optional[int]) -> None:
    target = days[-1]
    core, extra = price_plan(conn, days, cfg)
    if limit:
        core = sorted({BENCHMARK, *cfg.tracking.sector_etfs.values(), *_largest(conn, days[-1], core, limit)})
    start = live_start(target, cfg)
    log.info("downloading prices: %d core ticker(s) from %s, %d extra with older history",
             len(core), start, len(extra))

    def work() -> dict:
        rep = prices.update_prices(conn, core, start, target)
        if extra:
            by_start: dict[date, list[str]] = {}
            for t, s in extra.items():
                by_start.setdefault(s, []).append(t)
            for s, ts in by_start.items():
                more = prices.update_prices(conn, ts, s, target)
                for k in rep:
                    rep[k] = sorted(set(rep[k]) | set(more[k]))
        return rep

    res = jl.run("prices", work, target, message_fn=price_message)
    if not res.ok:
        raise CriticalError(f"price download failed: {res.error}")
    n = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
    spy = conn.execute("SELECT MAX(date) FROM prices WHERE ticker = ?", (BENCHMARK,)).fetchone()[0]
    if n == 0 or spy is None:
        raise CriticalError("no prices could be downloaded (SPY and the universe are all missing)")
    if spy < target.isoformat():
        raise CriticalError(f"latest SPY price is {spy}, expected {target}: the session's data is not available")

    positions = [r[0] for r in conn.execute(
        "SELECT DISTINCT r.ticker FROM recommendations r "
        "LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = 'live' AND x.rec_id IS NULL "
        "AND COALESCE(r.status,'') NOT IN ('stopped','target_hit','time_stop','expired')")]
    if positions:
        jl.run("splits", lambda: prices.update_splits(conn, positions), target,
               message_fn=lambda n_rows: f"{len(positions)} ticker(s), {n_rows} split row(s)")


def _largest(conn, day: date, core: list[str], limit: int) -> list[str]:
    """The `limit` universe tickers with the largest market cap (for --limit trial runs)."""
    uni = DataAccess(conn).get_universe(day)
    if uni.empty:
        return core[:limit]
    return uni.sort_values("market_cap", ascending=False)["ticker"].head(limit).tolist()


def _process_day(conn, cfg: Config, jl: JobLog, day: date, modules, data: DataAccess) -> dict:
    out = {"edgar_ok": False, "candidates": 0, "recs": 0, "in_report": 0}

    r = jl.run("edgar", lambda: edgar_daily.process_day(conn, day), day,
               message_fn=lambda rep: json.dumps({k: rep[k] for k in (
                   "index_date", "form4_processed", "eightk_processed", "earnings_events")}
                   | {"errors": len(rep["errors"])}))
    out["edgar_ok"] = r.ok

    jl.run("regime", lambda: compute_regime(conn, day), day,
           message_fn=lambda row: f"{row['regime']} breadth={row['breadth_pct']:.1f}%")

    candidates = []
    for m in modules:
        res = jl.run(f"scan:{m.name}", lambda m=m: m.scan(day, data), day,
                     message_fn=lambda found: f"{len(found)} candidate(s)")
        if res.ok:
            candidates.extend(res.value)
    out["candidates"] = len(candidates)

    if candidates:  # share counts in the guardrail must be split-adjusted
        jl.run("splits_candidates", lambda: prices.update_splits(conn, sorted({c.ticker for c in candidates})), day,
               message_fn=lambda n_rows: f"{n_rows} split row(s)")
    ranked = jl.run("rank", lambda: rank(candidates, day, data, conn, cfg, "live"), day,
                    message_fn=lambda recs: f"{len(recs)} recommendation(s)")
    recs = ranked.value or []
    _stamp(recs, day)
    jl.run("save_recommendations", lambda: insert_recommendations(conn, recs), day,
           message_fn=lambda n: f"{n} new")
    jl.run("baselines", lambda: baseline.create_for(conn, recs, day, cfg), day,
           message_fn=lambda n: f"{n} new")
    out["recs"] = len(recs)
    out["in_report"] = sum(1 for x in recs if x.in_report)
    # Completed even if a step failed (errors are logged and shown in the summary): the
    # next run starts after this day, so a crash cannot loop forever.
    jl.record(DAY_JOB, day, "ok", f"{len(recs)} recs" + ("" if ranked.ok else "; ranking failed"))
    return out


def prepare(cfg: Config, *, target: Optional[date] = None, dry_run: bool = False,
            force: bool = False, limit: Optional[int] = None, now: Optional[datetime] = None) -> int:
    run_id = new_run_id(now)
    latest = latest_completed_session(now)
    if target is not None and target > latest:
        print(f"prepare: {target} is not a finished session yet (latest finished: {latest}); nothing to do")
        return 0
    target = target or latest

    db_path = _db_path(cfg, dry_run)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    conn = init_db(db_path)
    import_state(conn, Path(cfg.paths.state_dir))
    jl = JobLog(conn, run_id)

    last = last_completed_day(conn)
    days = missed_days(last, target)
    if force and not days:
        days = [target]
    if not days:
        print(f"prepare: already up to date through {last}; nothing to do")
        conn.close()
        return 0
    log.info("prepare: last completed day %s, processing %d day(s): %s..%s",
             last, len(days), days[0], days[-1])

    exit_code = 0
    summary: dict = {}
    try:
        with_universe = DataAccess(conn).get_universe(days[0])
        if with_universe.empty:
            log.warning("no universe snapshot yet: building one now (normally the weekly job does this)")
            jl.run("bootstrap_universe",
                   lambda: refresh_universe_job(conn, cfg, days[0], jl, limit=limit), target, critical=True)

        _download_prices(conn, cfg, jl, days, limit)
        jl.run("earnings_calendar",
               lambda: earnings.update_upcoming_earnings(conn, target, UPCOMING_EARNINGS_DAYS), target)

        data = DataAccess(conn)
        modules = get_enabled_modules(cfg)
        per_day = [_process_day(conn, cfg, jl, d, modules, data) for d in days]
        summary["days"] = per_day
        if not any(p["edgar_ok"] for p in per_day):
            raise CriticalError("EDGAR is unavailable: no daily filing index could be processed")

        jl.run("tracker", lambda: tracker.update_all(conn, target, "live", cfg), target,
               message_fn=json.dumps)

        in_dir, _ = _work_dirs(cfg)
        today_recs = [tracker.row_to_rec(r) for r in conn.execute(
            "SELECT * FROM recommendations WHERE source = 'live' AND signal_date = ? AND in_report = 1 "
            "ORDER BY rank", (target.isoformat(),))]
        if dry_run:
            log.info("dry run: %d brief input(s) not written", len(today_recs))
        else:
            jl.run("brief_inputs", lambda: brief_io.write_inputs(conn, today_recs, target, in_dir, config=cfg),
                   target, message_fn=lambda paths: f"{len(paths)} file(s)")
    except CriticalError as exc:
        log.error("CRITICAL: %s", exc)
        _alert(f"swing-screener daily run {target}: {exc}", dry_run)
        exit_code = 2
    except Exception as exc:  # noqa: BLE001
        log.exception("prepare crashed")
        _alert(f"swing-screener daily run {target} crashed: {type(exc).__name__}: {exc}", dry_run)
        exit_code = 1
    finally:
        _export(conn, cfg, target, dry_run)

    done = last_completed_day(conn)
    print(f"prepare {target}{' (dry run)' if dry_run else ''}: processed {len(days)} day(s) "
          f"({days[0]}..{days[-1]}), completed through {done}, "
          f"{sum(p['recs'] for p in summary.get('days', []))} recommendation(s), "
          f"{len(jl.errors)} non-critical error(s)" + ("" if exit_code == 0 else f", FAILED (exit {exit_code})"))
    for e in jl.errors:
        print(f"  - {e}")
    conn.close()
    return exit_code


# ---------------------------------------------------------------------------
# stage 2: finalize
# ---------------------------------------------------------------------------


def _open_for_finalize(cfg: Config, dry_run: bool):
    """Reuse the DB prepare just built (it holds this run's prices, which the report's
    tracking section needs); otherwise rebuild from state/ and fetch prices for open
    positions only."""
    path = _db_path(cfg, dry_run)
    conn = None
    if path.exists():
        conn = init_db(path)
        if last_completed_day(conn) is None:
            conn.close()
            conn = None
    rebuilt = conn is None
    if rebuilt:
        conn = open_db(cfg, dry_run=dry_run)
    return conn, rebuilt


def finalize(cfg: Config, *, target: Optional[date] = None, dry_run: bool = False,
             force: bool = False, now: Optional[datetime] = None) -> int:
    run_id = new_run_id(now)
    conn, rebuilt = _open_for_finalize(cfg, dry_run)
    jl = JobLog(conn, run_id)
    target = target or last_completed_day(conn)
    if target is None:
        print("finalize: no completed trading day in state; nothing to do")
        conn.close()
        return 0
    if finalize_done(conn, target) and not force:
        print(f"finalize: {target} was already finalised; nothing to do")
        conn.close()
        return 0

    exit_code = 0
    html_path = None
    try:
        if rebuilt:
            _refetch_prices_for_report(conn, cfg, jl, target)
        _, out_dir = _work_dirs(cfg)
        in_dir, _ = _work_dirs(cfg)
        jl.run("merge_briefs", lambda: brief_io.merge_outputs(conn, out_dir, inputs_dir=in_dir), target,
               message_fn=lambda rep: json.dumps({k: (len(v) if hasattr(v, "__len__") else v)
                                                  for k, v in rep.items()}))
        since = _previous_finalized(conn, target)
        if dry_run:
            ctx = report.build_context(conn, target, cfg, since)
            log.info("dry run: report not written (%d new recommendation(s))", ctx["n_new"])
            summary_text = report.summary_text(ctx)
        else:
            res = jl.run("report", lambda: report.build(conn, target, config=cfg, since=since), target,
                         critical=True)
            html_path = res.value[0]
            summary_text = report.build_summary(conn, target, config=cfg, since=since)
        jl.record(FINALIZE_JOB, target, "ok", "")
    except Exception as exc:  # noqa: BLE001 - no report = no deliverable
        log.exception("finalize failed")
        _alert(f"swing-screener daily finalize {target} failed: {type(exc).__name__}: {exc}", dry_run)
        exit_code = 2
        summary_text = f"finalize failed: {exc}"
    finally:
        _export(conn, cfg, target, dry_run)

    if exit_code == 0 and not dry_run and html_path:
        jl.run("telegram", lambda: telegram.send_report(html_path, summary_text), target,
               message_fn=lambda ok: "sent" if ok else "not sent (unconfigured or failed)")
    print(summary_text)
    errs = [r[0] for r in conn.execute(
        "SELECT job || ': ' || COALESCE(message,'') FROM job_log WHERE status = 'error' AND trading_date = ?",
        (target.isoformat(),))]
    if errs:
        print(f"{len(errs)} non-critical error(s) during the run:")
        for e in errs:
            print(f"  - {e}")
    conn.close()
    return exit_code


def _previous_finalized(conn, target: date) -> Optional[date]:
    """The last day a report was finalised before ``target``: the catch-up section and the
    tracking updates cover everything since then (None -> the report picks its own default)."""
    row = conn.execute(
        "SELECT MAX(trading_date) FROM job_log WHERE job = ? AND status = 'ok' AND trading_date < ?",
        (FINALIZE_JOB, target.isoformat())).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def _refetch_prices_for_report(conn, cfg: Config, jl: JobLog, target: date) -> None:
    pos = open_position_tickers(conn)
    tickers = sorted({BENCHMARK, *cfg.tracking.sector_etfs.values(), *pos})
    start = min([live_start(target, cfg), *[d for d in pos.values()]])
    jl.run("prices_for_report", lambda: prices.update_prices(conn, tickers, start, target), target)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=["prepare", "finalize", "all"], default="all")
    ap.add_argument("--date", type=date.fromisoformat, help="treat this session as the latest one")
    ap.add_argument("--dry-run", action="store_true", help="no state/report writes, no Telegram")
    ap.add_argument("--limit", type=int, help="trial runs: at most N universe tickers")
    ap.add_argument("--force", action="store_true", help="redo a day that is already complete")
    ap.add_argument("--base-dir", type=Path, help="root holding state/, reports/, data/, work/ (default: the repo; "
                                                  "use a temporary copy for trial runs)")
    args = ap.parse_args(argv)
    if args.base_dir:
        os.environ[BASE_DIR_ENV] = str(args.base_dir)
    cfg = load_config()
    setup_logging(Path(cfg.paths.logs_dir))
    code = 0
    if args.stage in ("prepare", "all"):
        code = prepare(cfg, target=args.date, dry_run=args.dry_run, force=args.force, limit=args.limit)
        if code != 0:
            return code
    if args.stage in ("finalize", "all"):
        code = finalize(cfg, target=args.date, dry_run=args.dry_run, force=args.force)
    return code


if __name__ == "__main__":
    sys.exit(main())
