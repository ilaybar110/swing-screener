"""Report edge cases: statistics fallback and markdown block structure."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src import report
from tests.report_helpers import AS_OF, GOOD_BRIEF, insert_rec, make_config, make_db, make_rec


@pytest.fixture()
def cfg(tmp_path):
    return make_config(tmp_path)


def _build(conn, cfg):
    html_path, md_path = report.build(conn, AS_OF, config=cfg)
    return Path(html_path).read_text(encoding="utf-8"), Path(md_path).read_text(encoding="utf-8")


def test_statistics_with_no_closed_trades_does_not_fail(tmp_path, cfg):
    """stats.summary currently raises when recs exist but none closed; report falls back."""
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", status="open", signal=date(2026, 9, 28)))
    conn.commit()
    _, md = _build(conn, cfg)
    stats = md.split("## Statistics")[1]
    assert "1 recommendations tracked: 1 open" in stats
    assert "not enough data yet" in stats


def test_markdown_blocks_are_separated(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", llm_brief=GOOD_BRIEF))
    insert_rec(conn, make_rec("OPN", signal=date(2026, 9, 28), status="open", current_r=0.2, rank=2))
    conn.commit()
    _, md = _build(conn, cfg)
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#") and i:
            assert lines[i - 1] == "", f"heading not preceded by blank line: {line}"
        if line.startswith("|") and i and not lines[i - 1].startswith("|"):
            assert lines[i - 1] == "", "table must start after a blank line"
    assert "\n\n\n" not in md
