"""src/jobs.py reliability helpers: day status / retries, weekly namespace, EDGAR index
backfill bookkeeping, missed-run detection, price messages (no network)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from src import jobs
from src.db import init_db


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "run.db")
    yield c
    c.close()


def _row(conn, job, day, status="ok", message=""):
    conn.execute("INSERT INTO job_log (run_id, job, trading_date, started_at, finished_at, status, message) "
                 "VALUES ('r', ?, ?, '', '', ?, ?)", (job, day, status, message))
    conn.commit()


def test_sessions_behind_counts_nyse_sessions():
    assert jobs.sessions_behind(date(2026, 10, 16), date(2026, 10, 19)) == 1  # Fri -> Mon
    assert jobs.sessions_behind(date(2026, 10, 15), date(2026, 10, 19)) == 2
    assert jobs.sessions_behind(date(2026, 10, 19), date(2026, 10, 19)) == 0
    assert jobs.sessions_behind(None, date(2026, 10, 19)) == 0
    assert jobs.sessions_behind(date(2024, 12, 24), date(2024, 12, 26)) == 1  # Christmas skipped


def test_day_result_partial_then_failed_after_max_attempts():
    status, msg = jobs.day_result(["scan:x"], 3, None)
    assert status == "partial" and "failed: scan:x" in msg and "attempt 1/3" in msg
    status, msg = jobs.day_result(["scan:x"], 3, (status, msg))
    assert status == "partial" and "attempt 2/3" in msg
    status, msg = jobs.day_result(["scan:x", "rank"], 0, (status, msg))
    assert status == "failed" and "attempt 3/3" in msg and "gave up" in msg
    assert jobs.day_result([], 4, ("partial", "4 recs; failed: x; attempt 1/3")) == \
        ("ok", "4 recs; ok on attempt 2/3")
    assert jobs.day_result([], 4, ("ok", "4 recs")) == ("ok", "4 recs")  # --force on an ok day
    assert jobs.day_result(["edgar"], 4, ("failed", "attempt 3/3"))[0] == "partial"  # forced redo starts over


def test_partial_and_failed_days_count_as_completed(conn):
    _row(conn, "day", "2026-10-15", "ok", "5 recs")
    _row(conn, "day", "2026-10-16", "partial", "1 recs; failed: edgar; attempt 1/3")
    assert jobs.last_completed_day(conn) == date(2026, 10, 16)
    _row(conn, "day", "2026-10-19", "failed", "gave up")
    assert jobs.last_completed_day(conn) == date(2026, 10, 19)
    assert jobs.partial_days(conn, date(2026, 10, 20)) == [date(2026, 10, 16)]


def test_partial_days_outside_the_retry_window_are_not_retried(conn):
    _row(conn, "day", "2026-10-01", "partial", "attempt 1/3")
    _row(conn, "day", "2026-10-19", "partial", "attempt 1/3")
    assert jobs.partial_days(conn, date(2026, 10, 20)) == [date(2026, 10, 19)]


def test_joblog_prefix_namespaces_weekly_rows(conn):
    daily, weekly = jobs.JobLog(conn, "d"), jobs.JobLog(conn, "w", prefix=jobs.WEEKLY_PREFIX)
    weekly.record("prices", date(2026, 10, 16), "ok", "weekly")
    daily.record("prices", date(2026, 10, 16), "ok", "daily")  # same date: must not delete the weekly row
    rows = dict(conn.execute("SELECT job, message FROM job_log").fetchall())
    assert rows == {"weekly:prices": "weekly", "prices": "daily"}
    res = weekly.run("boom", lambda: 1 / 0, date(2026, 10, 16))
    assert not res.ok and weekly.errors[-1].startswith("weekly:boom 2026-10-16")


def test_edgar_backfill_dates_reads_new_rows_and_legacy_edgar_messages(conn):
    legacy = lambda idx: json.dumps({"index_date": idx, "form4_processed": 0})  # noqa: E731
    _row(conn, "edgar", "2026-10-09", "ok", legacy("2026-10-09"))
    _row(conn, "edgar", "2026-10-12", "ok", legacy("2026-10-09"))   # Columbus Day: fell back
    _row(conn, "edgar", "2026-10-13", "ok", legacy("2026-10-13"))
    _row(conn, "edgar_index", "2026-10-19", "missing", "attempt 1/3: not published")
    _row(conn, "edgar_index", "2026-10-14", "missing", "attempt 3/3: gave up")
    _row(conn, "edgar_index", "2026-10-15", "missing", "attempt 1/3")
    _row(conn, "edgar_index", "2026-10-15", "ok", "backfilled")  # (two rows cannot coexist live; ok wins)
    assert jobs.edgar_backfill_dates(conn, date(2026, 10, 20)) == [date(2026, 10, 12), date(2026, 10, 19)]
    _row(conn, "edgar_index", "2026-10-12", "missing", "attempt 3/3: gave up")
    assert jobs.edgar_backfill_dates(conn, date(2026, 10, 20)) == [date(2026, 10, 19)]
    # far outside the window: ignored
    assert jobs.edgar_backfill_dates(conn, date(2026, 12, 31)) == []


def test_check_missed_runs_alerts_once_and_records(conn):
    jl = jobs.JobLog(conn, "r")
    sent: list[str] = []
    assert jobs.check_missed_runs(conn, jl, date(2026, 10, 20), "daily run", sent.append) == 0  # no history
    _row(conn, "finalize", "2026-10-15")
    assert jobs.check_missed_runs(conn, jl, date(2026, 10, 16), "daily run", sent.append) == 1
    assert sent == []
    assert jobs.check_missed_runs(conn, jl, date(2026, 10, 20), "daily run", sent.append) == 3
    assert len(sent) == 1 and "2026-10-15" in sent[0] and "3 session(s) behind" in sent[0]
    assert jobs.job_row(conn, jobs.MISSED_RUN_JOB, date(2026, 10, 20))[0] == "alert"
    jobs.check_missed_runs(conn, jl, date(2026, 10, 20), "daily run", sent.append)
    assert len(sent) == 1  # not repeated for the same session
    weekly = jobs.JobLog(conn, "w", prefix=jobs.WEEKLY_PREFIX)
    jobs.check_missed_runs(conn, weekly, date(2026, 10, 20), "weekly run",
                           lambda t: (_ for _ in ()).throw(OSError("net")))  # alert failure is swallowed
    assert jobs.job_row(conn, "weekly:missed_run", date(2026, 10, 20)) is not None


def test_price_message_reports_stale_separately():
    msg = json.loads(jobs.price_message({"successes": ["A", "B", "C"], "retried": ["D"], "nasdaq_filled": ["E"],
                                         "missing": ["F"], "stale": ["B", "E"]}))
    assert msg == {"ok": 3, "missing": ["F"], "nasdaq_filled": ["E"], "stale": ["B", "E"]}
    legacy = json.loads(jobs.price_message({"successes": ["A"], "retried": [], "nasdaq_filled": [], "missing": []}))
    assert legacy["ok"] == 1 and legacy["stale"] == []
    text = jobs.stale_message({"stale": [f"T{i:02d}" for i in range(14)]}, date(2026, 10, 20))
    assert text.startswith("14 ticker(s) have no price bar for 2026-10-20") and "and 2 more" in text


def test_latest_completed_session_matches_routine_timing():
    # Wednesday 07:00 UTC -> Tuesday's session; Monday 10:00 UTC -> Friday's
    assert jobs.latest_completed_session(datetime(2026, 10, 21, 7, tzinfo=timezone.utc)) == date(2026, 10, 20)
    assert jobs.latest_completed_session(datetime(2026, 10, 19, 10, tzinfo=timezone.utc)) == date(2026, 10, 16)
