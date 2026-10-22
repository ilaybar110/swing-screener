"""tools/check_staleness.py: the GitHub Actions missed-run watchdog (no network)."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from tools import check_staleness as cs


def _root(tmp_path: Path, finalized: list[str], heading_date: str | None) -> Path:
    (tmp_path / "state").mkdir()
    (tmp_path / "reports").mkdir()
    with open(tmp_path / "state" / "job_log.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "job", "trading_date", "started_at", "finished_at", "status", "message"])
        for d in finalized:
            w.writerow(["r", "finalize", d, "", "", "ok", ""])
            w.writerow(["r", "day", d, "", "", "ok", "3 recs"])
        w.writerow(["r", "finalize", "2099-01-01", "", "", "error", ""])  # failed rows do not count
    if heading_date:
        (tmp_path / "reports" / "latest.md").write_text(
            f"# Swing Screener - {heading_date}\n\n**Market regime: Caution**\n", encoding="utf-8")
    return tmp_path


def _run(root: Path, now: str, *extra: str) -> tuple[int, list[str]]:
    sent: list[str] = []
    code = cs.main(["--root", str(root), "--now", now, *extra], send=lambda t: sent.append(t) or True)
    return code, sent


def test_fresh_report_passes(tmp_path):
    root = _root(tmp_path, ["2026-10-19", "2026-10-20"], "2026-10-20")
    assert _run(root, "2026-10-21T10:17:00") == (0, [])


def test_monday_expects_fridays_report(tmp_path):
    root = _root(tmp_path, ["2026-10-16"], "2026-10-16")
    assert _run(root, "2026-10-19T10:17:00") == (0, [])
    (tmp_path / "b").mkdir()
    root2 = _root(tmp_path / "b", ["2026-10-15"], "2026-10-15")
    code, sent = _run(root2, "2026-10-19T10:17:00")
    assert code == 1 and len(sent) == 1 and "2026-10-15" in sent[0] and "2026-10-16" in sent[0]


def test_stale_report_alerts(tmp_path):
    root = _root(tmp_path, ["2026-10-16", "2026-10-19"], "2026-10-19")
    code, sent = _run(root, "2026-10-22T10:17:00")
    assert code == 1 and "2 session(s) behind" in sent[0]


def test_max_behind_and_no_alert_flags(tmp_path):
    root = _root(tmp_path, ["2026-10-19"], "2026-10-19")
    assert _run(root, "2026-10-21T10:17:00", "--max-behind", "1")[0] == 0
    assert _run(root, "2026-10-21T10:17:00", "--no-alert") == (1, [])


def test_newest_of_job_log_and_latest_md_wins(tmp_path):
    root = _root(tmp_path, ["2026-10-19"], "2026-10-20")  # report written, job_log row pruned/missing
    assert _run(root, "2026-10-21T10:17:00")[0] == 0
    st = cs.check(root, cs.datetime(2026, 10, 21, 10, 17, tzinfo=cs.timezone.utc))
    assert st.last_job_log == date(2026, 10, 19) and st.last_report == date(2026, 10, 20)


def test_nothing_readable_is_exit_2(tmp_path):
    code, sent = _run(tmp_path, "2026-10-21T10:17:00")
    assert code == 2 and len(sent) == 1


def test_heading_parser_ignores_other_first_lines(tmp_path):
    p = tmp_path / "latest.md"
    p.write_text("\n# Swing Screener - 2026-10-20\n", encoding="utf-8")
    assert cs.report_date(p) == date(2026, 10, 20)
    p.write_text("Some text\n# Swing Screener - 2026-10-20\n", encoding="utf-8")
    assert cs.report_date(p) is None
