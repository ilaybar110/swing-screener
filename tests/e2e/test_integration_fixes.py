"""Regression tests for integration mismatches fixed while wiring the entrypoints."""

from __future__ import annotations

from src import stats
from tests.report_helpers import insert_rec, make_db, make_rec


def test_stats_summary_with_open_recommendations_but_nothing_closed(tmp_path):
    """First weeks of live running: recommendations exist, none has closed. This used to
    raise KeyError('id') inside stats.summary."""
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA"))
    out = stats.summary(conn, "live")
    assert out["counts"]["recommendations"] == 1 and out["counts"]["closed"] == 0
    assert out["by_module_contains"]  # modules are listed (with zero closed trades)
    assert all(v["count"] == 0 for v in out["by_module_contains"].values())
