"""LOCAL research backfill into data/research.db (never run in the cloud).

    python run_backfill.py                  # everything, resumable
    python run_backfill.py --limit 50       # trial run on the 50 largest companies (data/research_trial.db)
    python run_backfill.py --stages prices,regime

Stages, in order (each one is resumable -- just run the script again after an interruption,
and re-running later tops the data up to the latest session):

  1. tickers      listed companies + CIKs + sector/industry (Nasdaq screener, SEC fallback)
  2. prices       daily OHLCV from prices.research_start_date (yfinance, Nasdaq fallback),
                  plus splits; chunked, only tickers that are missing or stale are fetched
  3. edgar        bulk submissions.zip (filings, earnings events) + Insider Transactions Data
                  Sets (Form 4 purchases); ~1.5 GB download the first time
  4. fundamentals bulk companyfacts.zip -> `fundamentals` (point-in-time by filing date)
  5. regime       market regime for every trading day, vectorised from the stored prices

Progress lines show done/total, elapsed time and an ETA. Survivorship caveat: the ticker
list is today's listing, so backtests on this data are an upper bound (docs/PLAN.md s.15).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from src import universe
from src.config import BASE_DIR_ENV, Config, load_config
from src.data import edgar_bulk, fundamentals, prices, universe_source
from src.data.edgar_client import EdgarClient
from src.db import init_db
from src.jobs import BENCHMARK, JobLog, latest_completed_session, new_run_id, sector_etfs
from src.utils.logging import get_logger, setup_logging

log = get_logger("run_backfill")

STAGES = ["tickers", "prices", "edgar", "fundamentals", "regime"]
COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
STALE_DAYS = 5          # a ticker whose newest price is older than this is topped up
PRICE_CHUNK = 100
SPLIT_CHUNK = 50
MIN_CAP_FACTOR = 0.5    # keep names at >= 50% of the universe market-cap floor (they may have grown)


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


def _fmt(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


class Progress:
    """Logs 'label: done/total (pct) elapsed .. ETA ..'."""

    def __init__(self, label: str, total: int, emit: Callable[[str], None] = print):
        self.label, self.total, self.emit = label, max(total, 1), emit
        self.t0 = time.monotonic()

    def update(self, done: int) -> None:
        elapsed = time.monotonic() - self.t0
        eta = elapsed / done * (self.total - done) if done else 0
        self.emit(f"  {self.label}: {done}/{self.total} ({100 * done / self.total:.0f}%) "
                  f"elapsed {_fmt(elapsed)}, ETA {_fmt(eta)}")


# ---------------------------------------------------------------------------
# stage 1: tickers
# ---------------------------------------------------------------------------


def stage_tickers(conn, cfg: Config, limit: Optional[int], emit=print) -> pd.DataFrame:
    """Upsert the listing into `tickers` and return the candidate set (largest first)."""
    client = EdgarClient(cfg.paths.raw_cache_dir, cfg.secrets.sec_email, cfg.edgar.max_requests_per_second)
    df, source = universe_source.get_universe_source(Path(cfg.paths.raw_cache_dir), client)
    keep = [r.Index for r in df.itertuples() if not universe._is_excluded(r.name, r.sic, cfg)]
    df = df.loc[keep]
    floor = cfg.universe.min_market_cap * MIN_CAP_FACTOR
    if source == "nasdaq":
        df = df[df["market_cap"].notna() & (df["market_cap"] >= floor)]
    df = df.sort_values("market_cap", ascending=False, na_position="last")
    if limit:
        df = df.head(limit)
    df = df.reset_index(drop=True)
    universe_source.upsert_tickers(conn, df, client)
    for etf in sorted({BENCHMARK, *sector_etfs(cfg)}):
        conn.execute(
            "INSERT OR IGNORE INTO tickers (ticker, name, exchange, is_active, updated_at) VALUES (?,?,?,1,?)",
            (etf, etf, "ARCA", date.today().isoformat()))
    conn.commit()
    emit(f"tickers: {len(df)} candidate companies from {source}")
    return df


# ---------------------------------------------------------------------------
# stage 2: prices + splits
# ---------------------------------------------------------------------------


def tickers_needing_prices(conn, tickers: list[str], start: date, end: date) -> tuple[list[str], list[str]]:
    """(missing entirely, stale) among ``tickers``."""
    have = {r[0]: r[1] for r in conn.execute("SELECT ticker, MAX(date) FROM prices GROUP BY ticker")}
    cutoff = (end - timedelta(days=STALE_DAYS)).isoformat()
    missing = [t for t in tickers if t not in have]
    stale = [t for t in tickers if t in have and have[t] < cutoff]
    return missing, stale


def stage_prices(conn, cfg: Config, tickers: list[str], start: date, end: date, emit=print) -> dict:
    wanted = sorted(set(tickers) | {BENCHMARK, *sector_etfs(cfg)})
    missing, stale = tickers_needing_prices(conn, wanted, start, end)
    emit(f"prices: {len(wanted)} tickers, {len(missing)} missing, {len(stale)} stale")
    report = {"missing": [], "nasdaq_filled": [], "downloaded": 0}

    def run_group(group: list[str], group_start: date, label: str) -> None:
        if not group:
            return
        prog = Progress(label, len(group), emit)
        for i in range(0, len(group), PRICE_CHUNK):
            rep = prices.update_prices(conn, group[i:i + PRICE_CHUNK], group_start, end)
            report["missing"] += rep["missing"]
            report["nasdaq_filled"] += rep["nasdaq_filled"]
            report["downloaded"] += len(rep["successes"]) + len(rep["retried"]) + len(rep["nasdaq_filled"])
            prog.update(min(i + PRICE_CHUNK, len(group)))

    run_group(missing, start, "prices (history)")
    # top-ups only need the recent tail (overlap generously: Yahoo may revise recent bars)
    tail_start = max(start, end - timedelta(days=60))
    run_group(stale, tail_start, "prices (top-up)")

    done = {r[0] for r in conn.execute(
        "SELECT message FROM job_log WHERE job = 'backfill:splits' AND status = 'ok'")}
    todo = [t for t in wanted if t not in done and t not in report["missing"]]
    if todo:
        emit(f"splits: {len(todo)} tickers")
        prog = Progress("splits", len(todo), emit)
        for i in range(0, len(todo), SPLIT_CHUNK):
            chunk = todo[i:i + SPLIT_CHUNK]
            prices.update_splits(conn, chunk)
            conn.executemany(
                "INSERT INTO job_log (run_id, job, trading_date, started_at, finished_at, status, message) "
                "VALUES (?, 'backfill:splits', NULL, ?, ?, 'ok', ?)",
                [("backfill", datetime.now().isoformat(timespec="seconds"),
                  datetime.now().isoformat(timespec="seconds"), t) for t in chunk])
            conn.commit()
            prog.update(min(i + SPLIT_CHUNK, len(todo)))
    report["missing"] = sorted(set(report["missing"]))
    return report


# ---------------------------------------------------------------------------
# stage 3 / 4: EDGAR bulk + fundamentals bulk
# ---------------------------------------------------------------------------


def stage_edgar(conn, cfg: Config, start: date, emit=print) -> dict:
    emit("edgar: bulk submissions + insider data sets (first run downloads ~1.5 GB; resumable)")
    return edgar_bulk.backfill(conn, start_year=start.year)


def stage_fundamentals(conn, cfg: Config, emit=print) -> dict:
    client = EdgarClient(cfg.paths.raw_cache_dir, cfg.secrets.sec_email, cfg.edgar.max_requests_per_second)
    dest = Path(cfg.paths.raw_cache_dir) / "bulk" / "companyfacts.zip"
    emit("fundamentals: companyfacts.zip (~1 GB; resumable)")
    if not edgar_bulk.download_file(client, COMPANYFACTS_ZIP_URL, dest):
        raise RuntimeError(f"companyfacts.zip is not available at {COMPANYFACTS_ZIP_URL}")
    return fundamentals.backfill_bulk(conn, dest)


# ---------------------------------------------------------------------------
# stage 5: regime (vectorised twin of src.regime.compute_regime)
# ---------------------------------------------------------------------------


def stage_regime(conn, cfg: Config, start: date, end: date, emit=print) -> int:
    """Regime for every trading day in [start, end] from the stored prices.

    Same definition as ``src.regime.compute_regime`` (SPY close vs its SMA, breadth = % of the
    universe above its own SMA, favourable / caution / unfavourable) but computed on whole
    price matrices at once. The universe on each day is reconstructed point-in-time from
    prices alone: price floor, 20-day dollar-volume floor and minimum history, among the
    tickers in the database (market cap cannot be known historically, so the candidate list
    already applied today's cap -- survivorship caveat)."""
    rc, uc = cfg.regime, cfg.universe
    t0 = time.monotonic()
    df = pd.read_sql_query(
        "SELECT ticker, date, close, volume FROM prices WHERE close IS NOT NULL ORDER BY date", conn)
    if df.empty or BENCHMARK not in set(df["ticker"]):
        raise RuntimeError("no SPY prices in the database: run the prices stage first")
    close = df.pivot(index="date", columns="ticker", values="close").sort_index()
    volume = df.pivot(index="date", columns="ticker", values="volume").reindex_like(close)
    del df
    emit(f"regime: loaded {close.shape[1]} tickers x {close.shape[0]} days in {_fmt(time.monotonic() - t0)}")

    spy = close[BENCHMARK].dropna()
    spy_sma = spy.rolling(rc.spy_sma_days).mean()
    spy_ok = spy > spy_sma

    stocks = close.drop(columns=[c for c in (BENCHMARK, *sector_etfs(cfg)) if c in close.columns])
    vol = volume[stocks.columns]
    sma = stocks.rolling(rc.breadth_sma_days, min_periods=int(rc.breadth_sma_days * 0.8)).mean()
    history = stocks.notna().cumsum()
    adv = (stocks * vol).rolling(20, min_periods=15).mean()
    eligible = (history >= uc.min_history_days) & (stocks > uc.min_price) & (adv >= uc.min_adv20_dollars)
    above = eligible & (stocks > sma)
    n_elig = eligible.sum(axis=1)
    breadth = (100.0 * above.sum(axis=1) / n_elig.replace(0, np.nan)).fillna(0.0)

    from src.utils.calendar import trading_days

    rows = []
    for d in trading_days(start, end):
        key = d.isoformat()
        if key not in spy_ok.index or pd.isna(spy_sma.get(key)):
            continue
        b = float(breadth.get(key, 0.0))
        s_ok, b_ok = bool(spy_ok[key]), b >= 50.0
        label = "Favorable" if s_ok and b_ok else "Caution" if (s_ok or b_ok) else "Unfavorable"
        rows.append((key, float(spy[key]), float(spy_sma[key]), b, label))
    conn.executemany(
        "INSERT OR REPLACE INTO regime_log (date, spy_close, spy_sma200, breadth_pct, regime) VALUES (?,?,?,?,?)",
        rows)
    conn.commit()
    emit(f"regime: {len(rows)} days written in {_fmt(time.monotonic() - t0)}")
    return len(rows)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run(cfg: Config, *, stages: list[str], limit: Optional[int] = None, start: Optional[date] = None,
        end: Optional[date] = None, db_path: Optional[Path] = None, emit=print) -> int:
    start = start or date.fromisoformat(cfg.prices.research_start_date)
    end = end or latest_completed_session()
    if db_path is None:
        research = Path(cfg.paths.research_db)
        db_path = research.with_name(research.stem + "_trial.db") if limit else research
    emit(f"backfill into {db_path} ({start}..{end}), stages: {', '.join(stages)}"
         + (f", trial limit {limit}" if limit else ""))
    conn = init_db(db_path)
    jl = JobLog(conn, new_run_id())
    t0 = time.monotonic()
    failed = 0

    def stage(name: str, fn):
        nonlocal failed
        if name not in stages:
            return None
        emit(f"== stage {name} ==")
        res = jl.run(f"backfill:{name}", fn, None)
        if not res.ok:
            failed += 1
            emit(f"stage {name} FAILED: {res.error} (re-run to resume)")
        return res.value

    cand = stage("tickers", lambda: stage_tickers(conn, cfg, limit, emit))
    if cand is None:  # tickers stage skipped or failed: use whatever is stored
        names = [r[0] for r in conn.execute(
            "SELECT ticker FROM tickers WHERE cik IS NOT NULL ORDER BY ticker")]
        if limit:
            names = names[:limit]
    else:
        names = cand["ticker"].tolist()
    stage("prices", lambda: stage_prices(conn, cfg, names, start, end, emit))
    stage("edgar", lambda: stage_edgar(conn, cfg, start, emit))
    stage("fundamentals", lambda: stage_fundamentals(conn, cfg, emit))
    stage("regime", lambda: stage_regime(conn, cfg, start, end, emit))

    n = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
         for t in ("tickers", "prices", "splits", "filings", "insider_trades", "earnings_dates",
                   "fundamentals", "regime_log")}
    emit(f"done in {_fmt(time.monotonic() - t0)}: " + ", ".join(f"{k}={v:,}" for k, v in n.items())
         + (f"; {failed} stage(s) failed" if failed else ""))
    conn.close()
    return 1 if failed else 0


def main(argv: Optional[list[str]] = None) -> int:
    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stages", default=",".join(STAGES), help=f"comma list from: {', '.join(STAGES)}")
    ap.add_argument("--limit", type=int, help="trial run on the N largest companies (separate DB)")
    ap.add_argument("--start", type=date.fromisoformat, help="history start (default prices.research_start_date)")
    ap.add_argument("--end", type=date.fromisoformat, help="history end (default: latest completed session)")
    ap.add_argument("--db", type=Path, help="database path (default data/research.db)")
    ap.add_argument("--base-dir", type=Path, help="root holding state/, reports/, data/, work/ (default: the repo; "
                                                  "use a temporary copy for trial runs)")
    args = ap.parse_args(argv)
    if args.base_dir:
        os.environ[BASE_DIR_ENV] = str(args.base_dir)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        ap.error(f"unknown stage(s): {', '.join(bad)}")
    cfg = load_config()
    setup_logging(Path(cfg.paths.logs_dir))
    return run(cfg, stages=stages, limit=args.limit, start=args.start, end=args.end, db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
