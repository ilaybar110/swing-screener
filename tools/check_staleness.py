"""Missed-run watchdog, run by .github/workflows/staleness.yml (weekdays ~10:00 UTC).

Reads the committed outputs only -- ``state/job_log.csv`` (``finalize`` rows) and the
heading of ``reports/latest.md`` -- and compares the newest finalised trading day with
the latest finished NYSE session. If the report is more than ``--max-behind`` sessions
behind it sends a Telegram alert (``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``; skipped
when unset) and exits 1, so the workflow run also fails visibly on GitHub.

    python tools/check_staleness.py [--root DIR] [--max-behind N] [--now ISO] [--no-alert]

Exit codes: 0 fresh, 1 stale, 2 nothing could be read (also alerted).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run as a script from the repo root or anywhere else
    sys.path.insert(0, str(ROOT))

from src.jobs import FINALIZE_JOB, latest_completed_session, sessions_behind  # noqa: E402

_HEADING = re.compile(r"^#\s.*?(\d{4}-\d{2}-\d{2})\s*$")


@dataclass
class Staleness:
    expected: date                  # latest finished NYSE session
    last_job_log: Optional[date]    # newest finalize ok row in state/job_log.csv
    last_report: Optional[date]     # date in the heading of reports/latest.md
    behind: int                     # sessions after the newest of the two, up to ``expected``

    @property
    def last(self) -> Optional[date]:
        known = [d for d in (self.last_job_log, self.last_report) if d is not None]
        return max(known) if known else None


def last_finalized_from_job_log(path: Path) -> Optional[date]:
    """Newest ``finalize``/``ok`` trading date in a job_log CSV (None if absent/unreadable)."""
    if not path.is_file():
        return None
    best: Optional[date] = None
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("job") != FINALIZE_JOB or row.get("status") != "ok":
                    continue
                try:
                    d = date.fromisoformat((row.get("trading_date") or "").strip())
                except ValueError:
                    continue
                best = d if best is None or d > best else best
    except (OSError, csv.Error):
        return None
    return best


def report_date(path: Path) -> Optional[date]:
    """The date in the first heading line of reports/latest.md (``# Swing Screener - YYYY-MM-DD``)."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = _HEADING.match(line.strip())
                if m:
                    return date.fromisoformat(m.group(1))
                if line.strip():
                    return None
    except (OSError, ValueError):
        return None
    return None


def check(root: Path, now: Optional[datetime] = None) -> Staleness:
    expected = latest_completed_session(now)
    st = Staleness(expected, last_finalized_from_job_log(root / "state" / "job_log.csv"),
                   report_date(root / "reports" / "latest.md"), 0)
    st.behind = sessions_behind(st.last, expected) if st.last is not None else -1
    return st


def main(argv: Optional[list[str]] = None, *, send: Optional[Callable[[str], bool]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root holding state/ and reports/")
    ap.add_argument("--max-behind", type=int, default=0,
                    help="sessions the newest report may lag the latest finished session (default 0)")
    ap.add_argument("--now", type=datetime.fromisoformat, help="pretend it is this UTC time (tests)")
    ap.add_argument("--no-alert", action="store_true", help="report only, never send Telegram")
    args = ap.parse_args(argv)
    now = args.now
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    st = check(args.root, now)
    if st.last is None:
        text = (f"swing-screener watchdog: no finalised report found in state/job_log.csv or "
                f"reports/latest.md (latest NYSE session {st.expected}).")
        code = 2
    elif st.behind > args.max_behind:
        text = (f"swing-screener watchdog: the newest report is for {st.last} but the latest finished "
                f"NYSE session is {st.expected} ({st.behind} session(s) behind). The daily routine "
                f"probably did not run - check the routine's runs and schedule on claude.ai/code.")
        code = 1
    else:
        print(f"fresh: newest report {st.last}, latest session {st.expected}, {st.behind} session(s) behind")
        return 0

    print(text)
    if not args.no_alert:
        if send is None:
            from notify import telegram

            send = telegram.send_alert
        print("alert sent" if send(text) else "alert not sent (Telegram unconfigured or failed)")
    return code


if __name__ == "__main__":
    sys.exit(main())
