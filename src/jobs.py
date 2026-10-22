"""Shared plumbing for the run_* entrypoints (owned by Bot 7).

- ``job_log`` helpers: every pipeline step is recorded with start/finish/status/message
  so a later run can tell which trading days are complete (``day`` rows) and the
  report can show data-quality notes (``prices`` rows). The weekly job writes its rows
  under ``weekly:<step>`` (``WEEKLY_PREFIX``) so a daily run for the same trading date
  cannot overwrite them; rows written before that change keep their bare names.
- ``day`` rows: ``ok`` (every sub-step succeeded), ``partial`` (some sub-step failed; the
  next runs retry the day, ``MAX_DAY_ATTEMPTS`` in total) or ``failed`` (gave up). All
  three count as processed for catch-up purposes, so a broken step cannot loop forever.
- ``edgar_index`` rows: one per EDGAR daily-index date, ``ok`` once its filings were read
  or ``missing`` (with an attempt count) while it has not been published.
- Exchange-session helpers: which US session is the latest *finished* one.
- Price-universe helper: the set of tickers whose prices a live run must download.
"""

from __future__ import annotations

import json
import re
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

import pandas_market_calendars as mcal

from src.config import Config
from src.data_access import DataAccess
from src.utils.calendar import trading_days
from src.utils.logging import get_logger

log = get_logger(__name__)

DAY_JOB = "day"            # one row per fully-processed trading day (prepare stage)
FINALIZE_JOB = "finalize"  # one row per finalised report date
CATCHUP_JOB = "catchup"    # message = missed days the daily run left for its next run (R2)
EDGAR_INDEX_JOB = "edgar_index"  # one row per EDGAR daily-index date (ok / missing)
MISSED_RUN_JOB = "missed_run"    # a run noticed earlier scheduled runs did not happen
WEEKLY_PREFIX = "weekly:"        # namespace of the weekly job's step names
DAY_DONE_STATUSES = ("ok", "partial", "failed")
MAX_DAY_ATTEMPTS = 3       # runs that may (re)process a partial day before it is marked failed
RETRY_WINDOW_SESSIONS = 5  # partial days older than this many sessions are no longer retried
EDGAR_BACKFILL_SESSIONS = 10   # look this many sessions back for unpublished EDGAR indexes
MAX_EDGAR_INDEX_ATTEMPTS = 3   # then the date is given up (e.g. SEC holidays never get one)
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


def sessions_behind(last_done: Optional[date], latest: date) -> int:
    """Number of NYSE sessions after ``last_done`` up to and including ``latest``
    (0 when ``last_done`` is ``latest`` or later)."""
    if last_done is None or last_done >= latest:
        return 0
    return len(trading_days(last_done + timedelta(days=1), latest))


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
    prefix: str = ""  # prepended to every job name (``WEEKLY_PREFIX`` for the weekly job)

    def _write(self, job: str, trading_date: Optional[date], started: datetime,
               status: str, message: str) -> None:
        job = self.prefix + job
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
            msg = f"{self.prefix}{job}{' ' + trading_date.isoformat() if trading_date else ''}: {type(exc).__name__}: {exc}"
            log.error("step failed (%s): %s", "critical" if critical else "non-critical", msg)
            log.debug("traceback:\n%s", traceback.format_exc())
            self.errors.append(msg)
            if critical:
                raise
            return StepResult(False, None, msg)


def last_completed_day(conn: sqlite3.Connection) -> Optional[date]:
    """Newest trading day the prepare stage has processed (``ok``, ``partial`` or ``failed``)."""
    row = conn.execute(
        f"SELECT MAX(trading_date) FROM job_log WHERE job = ? "
        f"AND status IN ({','.join('?' * len(DAY_DONE_STATUSES))})", (DAY_JOB, *DAY_DONE_STATUSES)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def last_finalized_day(conn: sqlite3.Connection) -> Optional[date]:
    """Newest trading day whose report was finalised (None with no history)."""
    row = conn.execute(
        "SELECT MAX(trading_date) FROM job_log WHERE job = ? AND status = 'ok'", (FINALIZE_JOB,)
    ).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


_ATTEMPT_RE = re.compile(r"attempt (\d+)/")


def parse_attempt(message: Optional[str]) -> int:
    """The ``attempt N/M`` counter in a ``day`` or ``edgar_index`` message (0 if absent)."""
    m = _ATTEMPT_RE.search(message or "")
    return int(m.group(1)) if m else 0


def job_row(conn: sqlite3.Connection, job: str, day: date) -> Optional[tuple[str, str]]:
    """(status, message) of the ``job`` row for ``day``, or None."""
    row = conn.execute(
        "SELECT status, COALESCE(message,'') FROM job_log WHERE job = ? AND trading_date = ?",
        (job, day.isoformat())).fetchone()
    return (row[0], row[1]) if row else None


def day_result(failed_steps: list[str], n_recs: int, previous: Optional[tuple[str, str]]) -> tuple[str, str]:
    """(status, message) for a ``day`` row. ``previous`` is the day's existing row: a
    re-processed ``partial`` day counts one more attempt, and after ``MAX_DAY_ATTEMPTS``
    runs a still-failing day becomes ``failed`` (no further retries)."""
    attempt = parse_attempt(previous[1]) + 1 if previous and previous[0] == "partial" else 1
    if not failed_steps:
        return "ok", f"{n_recs} recs" + (f"; ok on attempt {attempt}/{MAX_DAY_ATTEMPTS}" if attempt > 1 else "")
    base = f"{n_recs} recs; failed: {', '.join(failed_steps)}; attempt {attempt}/{MAX_DAY_ATTEMPTS}"
    if attempt >= MAX_DAY_ATTEMPTS:
        return "failed", base + "; gave up"
    return "partial", base


def check_missed_runs(conn: sqlite3.Connection, jl: JobLog, latest: date, who: str,
                      alert: Callable[[str], Any]) -> int:
    """Alert (once per ``latest`` session) when the last finalised report is more than one
    session behind ``latest``, i.e. scheduled daily runs did not happen, and record a
    ``missed_run`` row (under the JobLog's prefix). Returns the sessions behind (0 with no
    finalised history, e.g. the very first run)."""
    last = last_finalized_day(conn)
    behind = sessions_behind(last, latest) if last else 0
    if behind <= 1:
        return behind
    if job_row(conn, jl.prefix + MISSED_RUN_JOB, latest) is not None:
        log.info("missed runs already reported for %s", latest)
        return behind
    text = (f"swing-screener {who}: the last finalised report is for {last} but the latest finished "
            f"session is {latest} ({behind} session(s) behind) - scheduled daily runs were missed. "
            "Check the routine's schedule and recent runs on claude.ai/code.")
    log.warning(text)
    try:
        alert(text)
    except Exception as exc:  # noqa: BLE001 - an alert must never stop the run
        log.error("missed-run alert failed: %s", exc)
    jl.record(MISSED_RUN_JOB, latest, "alert", f"last finalised {last}, {behind} session(s) behind")
    return behind


def partial_days(conn: sqlite3.Connection, latest: date) -> list[date]:
    """``partial`` days within ``RETRY_WINDOW_SESSIONS`` sessions of ``latest``, oldest first."""
    sessions = trading_days(latest - timedelta(days=3 * RETRY_WINDOW_SESSIONS + 10), latest)
    if not sessions:
        return []
    floor = sessions[-RETRY_WINDOW_SESSIONS:][0]
    rows = conn.execute(
        "SELECT trading_date FROM job_log WHERE job = ? AND status = 'partial' "
        "AND trading_date >= ? AND trading_date <= ? ORDER BY trading_date",
        (DAY_JOB, floor.isoformat(), latest.isoformat())).fetchall()
    return [date.fromisoformat(r[0]) for r in rows]


def edgar_backfill_dates(conn: sqlite3.Connection, latest: date) -> list[date]:
    """EDGAR daily-index dates that were not published when their day was processed and
    have not been read since, oldest first: ``edgar_index`` rows with status ``missing``
    and fewer than ``MAX_EDGAR_INDEX_ATTEMPTS`` attempts, plus (state written before
    ``edgar_index`` rows existed) ``edgar`` rows whose JSON ``index_date`` is an older
    day. Only the last ``EDGAR_BACKFILL_SESSIONS`` sessions up to ``latest`` are looked at."""
    sessions = trading_days(latest - timedelta(days=3 * EDGAR_BACKFILL_SESSIONS + 10), latest)
    if not sessions:
        return []
    window = set(sessions[-EDGAR_BACKFILL_SESSIONS:])
    floor = min(window).isoformat()
    ok: set[str] = set()
    missing: dict[str, int] = {}
    for d, status, msg in conn.execute(
            "SELECT trading_date, status, COALESCE(message,'') FROM job_log WHERE job = ? AND trading_date >= ?",
            (EDGAR_INDEX_JOB, floor)):
        if status == "ok":
            ok.add(d)
        elif status == "missing":
            missing[d] = parse_attempt(msg)
    for d, msg in conn.execute(
            "SELECT trading_date, COALESCE(message,'') FROM job_log WHERE job = 'edgar' AND status = 'ok' "
            "AND trading_date >= ?", (floor,)):
        try:
            idx = json.loads(msg).get("index_date")
        except (ValueError, AttributeError):
            continue
        if idx:
            ok.add(idx)
            if idx != d:
                missing.setdefault(d, 0)
    out = [date.fromisoformat(d) for d, n in missing.items()
           if d not in ok and n < MAX_EDGAR_INDEX_ATTEMPTS and date.fromisoformat(d) in window]
    return sorted(out)


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
    """JSON message for the ``prices`` job_log row (src.report reads missing/nasdaq_filled).
    ``ok`` counts downloaded tickers whose data reaches the expected session; ``stale``
    lists the ones that stop earlier."""
    stale = report.get("stale", [])
    fresh = (set(report.get("successes", [])) | set(report.get("retried", []))) - set(stale)
    return json.dumps({
        "ok": len(fresh),
        "missing": report.get("missing", []),
        "nasdaq_filled": report.get("nasdaq_filled", []),
        "stale": stale,
    })


def stale_message(report: dict, day: date) -> str:
    """Free-text note for the ``prices_stale`` row (shown in the report's data-quality note)."""
    stale = sorted(report.get("stale", []))
    names = ", ".join(stale[:12]) + (f" and {len(stale) - 12} more" if len(stale) > 12 else "")
    return f"{len(stale)} ticker(s) have no price bar for {day} (stale data): {names}"
