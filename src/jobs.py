"""Shared plumbing for the run_* entrypoints (owned by Bot 7).

- ``job_log`` helpers: every pipeline step is recorded with start/finish/status/message
  so a later run can tell which trading days are complete (``day`` rows) and the
  report can show data-quality notes (``prices`` rows).
- Exchange-session helpers: which US session is the latest *finished* one.
- Price-universe helper: the set of tickers whose prices a live run must download.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import pandas_market_calendars as mcal

from src.config import Config
from src.data_access import DataAccess
from src.utils.calendar import trading_days
from src.utils.logging import get_logger

log = get_logger(__name__)

DAY_JOB = "day"            # one row per fully-processed trading day (prepare stage)
FINALIZE_JOB = "finalize"  # one row per finalised report date
CATCHUP_JOB = "catchup"    # message = missed days the daily run left for its next run (R2)
SESSION_BUFFER = timedelta(minutes=20)  # data vendors need a few minutes after the close
JOB_LOG_RETENTION_DAYS = 45


class CriticalError(RuntimeError):
    """A failure that makes the run's output meaningless (no prices at all, EDGAR fully
    down, ...): the entrypoint alerts, exports whatever state is valid and exits non-zero."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(now: Optional[datetime] = None) -> str:
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def latest_completed_session(now: Optional[datetime] = None) -> date:
    """The most recent NYSE session whose close (plus a small buffer) is at or before
    ``now`` (UTC). Handles early closes and holidays via the exchange calendar."""
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cal = mcal.get_calendar("NYSE")
    sched = cal.schedule(start_date=(now - timedelta(days=14)).date(), end_date=now.date())
    for idx in reversed(range(len(sched))):
        close = sched.iloc[idx]["market_close"]
        close = close.to_pydatetime()
        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        if close + SESSION_BUFFER <= now:
            return sched.index[idx].date()
    raise RuntimeError(f"no completed NYSE session found before {now}")


def missed_days(last_done: Optional[date], target: date) -> list[date]:
    """Trading days still to process, oldest first. With no history (first ever run)
    only ``target`` itself is processed."""
    if last_done is None:
        return [target]
    if last_done >= target:
        return []
    return trading_days(last_done + timedelta(days=1), target)


# ---------------------------------------------------------------------------
# job_log
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    ok: bool
    value: Any = None
    error: Optional[str] = None


@dataclass
class JobLog:
    """Writes one ``job_log`` row per step. ``message`` is free text, or JSON for the
    ``prices`` step (the shape ``src.report`` reads for its data-quality note)."""

    conn: sqlite3.Connection
    run_id: str
    errors: list[str] = field(default_factory=list)

    def _write(self, job: str, trading_date: Optional[date], started: datetime,
               status: str, message: str) -> None:
        if trading_date is not None:  # a re-run of a step replaces its earlier row
            self.conn.execute(
                "DELETE FROM job_log WHERE job = ? AND trading_date = ?",
                (job, trading_date.isoformat()),
            )
        self.conn.execute(
            "INSERT INTO job_log (run_id, job, trading_date, started_at, finished_at, status, message) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.run_id, job, trading_date.isoformat() if trading_date else None,
             started.replace(microsecond=0).isoformat(), utc_now().replace(microsecond=0).isoformat(),
             status, message[:2000]),
        )
        self.conn.commit()

    def record(self, job: str, trading_date: Optional[date], status: str = "ok",
               message: str = "") -> None:
        self._write(job, trading_date, utc_now(), status, message)

    @contextmanager
    def step(self, job: str, trading_date: Optional[date] = None) -> Iterator[dict[str, Any]]:
        """Context manager: logs status ``ok`` (or ``error`` + re-raises). Set
        ``ctx["message"]`` inside the block to attach a message."""
        ctx: dict[str, Any] = {"message": ""}
        started = utc_now()
        try:
            yield ctx
        except BaseException as exc:
            self._write(job, trading_date, started, "error", f"{type(exc).__name__}: {exc}")
            raise
        self._write(job, trading_date, started, "ok", str(ctx["message"]))

    def run(self, job: str, fn, trading_date: Optional[date] = None, *, critical: bool = False,
            message_fn=None) -> StepResult:
        """Run ``fn()`` as a logged step. Non-critical failures are logged and swallowed
        (``StepResult.ok`` False); critical ones are re-raised after logging."""
        try:
            with self.step(job, trading_date) as ctx:
                value = fn()
                if message_fn is not None:
                    ctx["message"] = message_fn(value)
            return StepResult(True, value)
        except Exception as exc:  # noqa: BLE001
            msg = f"{job}{' ' + trading_date.isoformat() if trading_date else ''}: {type(exc).__name__}: {exc}"
            log.error("step failed (%s): %s", "critical" if critical else "non-critical", msg)
            log.debug("traceback:\n%s", traceback.format_exc())
            self.errors.append(msg)
            if critical:
                raise
            return StepResult(False, None, msg)


def last_completed_day(conn: sqlite3.Connection) -> Optional[date]:
    row = conn.execute(
        "SELECT MAX(trading_date) FROM job_log WHERE job = ? AND status = 'ok'", (DAY_JOB,)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def catchup_remaining(conn: sqlite3.Connection, day: date) -> int:
    """Missed trading days the prepare run that completed ``day`` left unprocessed."""
    row = conn.execute(
        "SELECT message FROM job_log WHERE job = ? AND trading_date = ? AND status = 'ok'",
        (CATCHUP_JOB, day.isoformat()),
    ).fetchone()
    try:
        return int(row[0]) if row and row[0] else 0
    except ValueError:
        return 0


def finalize_done(conn: sqlite3.Connection, day: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM job_log WHERE job = ? AND trading_date = ? AND status = 'ok'",
        (FINALIZE_JOB, day.isoformat()),
    ).fetchone()
    return row is not None


def prune_job_log(conn: sqlite3.Connection, as_of: date) -> None:
    """Drop old step rows so state/job_log.csv stays small; ``day``/``finalize``
    markers for the most recent dates are always kept."""
    cutoff = (as_of - timedelta(days=JOB_LOG_RETENTION_DAYS)).isoformat()
    conn.execute(
        "DELETE FROM job_log WHERE COALESCE(trading_date, '') < ? AND job NOT IN (?, ?)",
        (cutoff, DAY_JOB, FINALIZE_JOB),
    )
    conn.execute(
        "DELETE FROM job_log WHERE job IN (?, ?) AND COALESCE(trading_date,'') < ? "
        "AND COALESCE(trading_date,'') < (SELECT MAX(trading_date) FROM job_log j2 WHERE j2.job = job_log.job)",
        (DAY_JOB, FINALIZE_JOB, cutoff),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# price universe
# ---------------------------------------------------------------------------

BENCHMARK = "SPY"


def sector_etfs(config: Config) -> list[str]:
    return sorted(set(config.tracking.sector_etfs.values()))


def live_start(as_of: date, config: Config) -> date:
    """Calendar start date giving ~``prices.live_lookback_days`` trading days."""
    return as_of - timedelta(days=int(config.prices.live_lookback_days * 1.5) + 10)


def open_position_tickers(conn: sqlite3.Connection) -> dict[str, date]:
    """Tickers of non-final live recommendations and baselines -> earliest signal date."""
    out: dict[str, date] = {}
    queries = (
        "SELECT r.ticker, MIN(r.signal_date) FROM recommendations r "
        "LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = 'live' AND x.rec_id IS NULL "
        "AND COALESCE(r.status,'') NOT IN ('stopped','target_hit','time_stop','expired') "
        "GROUP BY r.ticker",
        "SELECT ticker, MIN(signal_date) FROM baselines WHERE source = 'live' "
        "AND COALESCE(status,'') NOT IN ('stopped','target_hit','time_stop','expired') "
        "GROUP BY ticker",
    )
    for q in queries:
        for t, d in conn.execute(q).fetchall():
            day = date.fromisoformat(d)
            out[t] = min(out.get(t, day), day)
    return out


def price_plan(conn: sqlite3.Connection, days: list[date], config: Config) -> tuple[list[str], dict[str, date]]:
    """(core tickers, extra {ticker: start}) a live run must download.

    core = universe snapshots in effect on any processed day + SPY + sector ETFs, all
    from ``live_start``; extra = tickers with open recommendations/baselines that need
    history reaching back before their signal date (warm-up for the SMA exit)."""
    data = DataAccess(conn)
    core: set[str] = {BENCHMARK, *sector_etfs(config)}
    for d in days:
        uni = data.get_universe(d)
        core.update(uni["ticker"].tolist())
    extra: dict[str, date] = {}
    base = live_start(days[-1], config)
    for t, sig in open_position_tickers(conn).items():
        need = sig - timedelta(days=90)
        if t not in core or need < base:
            extra[t] = min(need, base)
    return sorted(core), extra


def price_message(report: dict) -> str:
    """JSON message for the ``prices`` job_log row (src.report reads missing/nasdaq_filled)."""
    return json.dumps({
        "ok": len(report.get("successes", [])) + len(report.get("retried", [])),
        "missing": report.get("missing", []),
        "nasdaq_filled": report.get("nasdaq_filled", []),
    })
