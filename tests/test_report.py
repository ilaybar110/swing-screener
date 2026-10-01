"""Report rendering on synthetic live data: normal day, empty day, Unfavorable, catch-up
day, missing briefs, invalid brief JSON, data-quality note, tracking updates."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from src import report
from tests.report_helpers import (AS_OF, GOOD_BRIEF, PREV, add_result, insert_rec, make_config, make_db,
                                  make_rec)


def _build(conn, cfg, as_of=AS_OF, **kw):
    html_path, md_path = report.build(conn, as_of, config=cfg, **kw)
    from pathlib import Path
    return Path(html_path).read_text(encoding="utf-8"), Path(md_path).read_text(encoding="utf-8")


def _assert_clean(html: str, md: str) -> None:
    """Self-contained HTML, no sizing language, footer present in both."""
    assert not re.search(r"""(src|href)\s*=""", html), "HTML must not load/link external resources"
    assert "url(" not in html and "@import" not in html
    for text in (html, md):
        low = text.lower()
        assert "position size" not in low and "share count" not in low and "number of shares" not in low
        assert "Research tool, not financial advice." in text
        assert "All candidates are tracked, including ones not shown." in text


@pytest.fixture()
def cfg(tmp_path):
    return make_config(tmp_path)


def test_normal_day(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", rank=1, llm_brief=GOOD_BRIEF, earnings_in_window=True,
                              modules=["a_momentum_pullback", "d_base_breakout"]))
    insert_rec(conn, make_rec("BBB", rank=2, guardrail="unknown"))
    conn.execute("INSERT INTO insider_trades (accession, row_num, ticker, insider_name, officer_title, "
                 "is_officer, trans_code, filed_date, value, shares) VALUES ('x',1,'AAA','Jane Roe','CFO',1,'P',?,?,?)",
                 ((AS_OF - timedelta(days=3)).isoformat(), 150000.0, 1000))
    conn.commit()
    html, md = _build(conn, cfg)
    _assert_clean(html, md)

    for text in (html, md):
        assert "Alpha Corp" in text and "AAA" in text and "BBB" in text
        assert "$100.00" in text and "$95.00" in text and "-5.0%" in text
        assert "$110.00" in text and "+10.0%" in text
        assert "2026-10-07" in text  # valid until
        assert "35th percentile vs sector" in text
        assert "2026-10-20" in text and "holding window" in text  # earnings warning on AAA
        assert "Jane Roe" in text and "$150K" in text
        assert "not enough data yet" in text  # module track record
        assert "Raised full-year revenue guidance" in text
        assert "at-the-market equity offering" in text
        assert "Investor day" in text and "2026-11-10" in text
        assert "Missing fundamentals" in text  # BBB guardrail reason
        assert "Favorable" in text and "$450.00" in text and "62% of the universe" in text
    assert 'class="flags"' in html  # red flags highlighted
    assert "1,000" not in html and "1000 shares" not in md  # no share counts
    assert (cfg.paths.reports_dir / f"{AS_OF.isoformat()}.html").exists()
    assert (cfg.paths.reports_dir / "latest.md").read_text(encoding="utf-8") == md
    assert (cfg.paths.reports_dir / "latest.html").read_text(encoding="utf-8") == html
    assert len(html.encode()) < 40_000


def test_empty_day(tmp_path, cfg):
    conn = make_db(tmp_path)
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    assert "No new recommendations today." in html and "No new recommendations today." in md
    assert "New recommendations (0)" in md
    assert "Statistics" in md  # rest of the page still renders


def test_unfavorable_suppresses_cards(tmp_path, cfg):
    conn = make_db(tmp_path, regime="Unfavorable")
    insert_rec(conn, make_rec("AAA", regime="Unfavorable"))  # even if one slipped through as in_report
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    for text in (html, md):
        assert "Unfavorable" in text and "No new recommendations" in text
        assert "pulled back to the 20-day SMA" not in text
    assert "<article" not in html
    # still tracked/open section present
    assert "Open recommendations" in md


def test_catch_up_day(tmp_path, cfg):
    conn = make_db(tmp_path)
    cfg.paths.reports_dir.mkdir(parents=True, exist_ok=True)
    prev_report = date(2026, 9, 25)
    (cfg.paths.reports_dir / f"{prev_report.isoformat()}.md").write_text("old", encoding="utf-8")
    insert_rec(conn, make_rec("AAA", rank=1))
    missed = date(2026, 9, 28)
    insert_rec(conn, make_rec("BBB", signal=missed, rank=1, status="open", current_r=0.8))
    insert_rec(conn, make_rec("CCC", signal=date(2026, 9, 24), rank=1))  # before previous report: not listed
    conn.commit()
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    for text in (html, md):
        assert "missed days" in text
        assert "2026-09-28" in text
    section = md.split("## Catch-up")[1].split("## Tracking")[0]
    assert "BBB" in section and "AAA" not in section and "CCC" not in section
    assert "+0.80R" in section
    assert "since the previous report (2026-09-25)" in section


def test_no_catch_up_section_when_nothing_missed(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA"))
    html, md = _build(conn, cfg)
    assert "missed days" not in html and "missed days" not in md


def test_missing_brief_omits_section(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", llm_brief=None))
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    assert "AAA" in md and "Summary." not in md and "Red flags" not in md
    assert "Summary." not in html and 'class="flags"' not in html


@pytest.mark.parametrize("bad", [
    "not json at all {{{",
    json.dumps({"summary": "x" * 700, "upcoming_catalysts": [], "red_flags": [],
                "recent_positive_events": [], "sources": []}),
    json.dumps({"summary": "ok"}),
    json.dumps([1, 2, 3]),
])
def test_invalid_brief_json_never_blocks(tmp_path, cfg, bad):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", llm_brief=bad))
    insert_rec(conn, make_rec("BBB", rank=2, llm_brief=GOOD_BRIEF))
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    assert md.count("**Summary.**") == 1  # only BBB's brief shown
    assert "AAA" in md and "BBB" in md


def test_html_escapes_untrusted_text(tmp_path, cfg):
    conn = make_db(tmp_path)
    evil = dict(GOOD_BRIEF, summary="<script>alert(1)</script> | pipe", red_flags=["<img src=x onerror=1>"])
    insert_rec(conn, make_rec("AAA", llm_brief=evil))
    html, md = _build(conn, cfg)
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in md and "\\|" in md


def test_data_quality_note_from_job_log(tmp_path, cfg):
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO job_log (run_id, job, trading_date, started_at, status, message) VALUES "
                 "('r','prices',?,?,?,?)", (AS_OF.isoformat(), "2026-09-30T10:00", "ok",
                                            json.dumps({"missing": ["ZZZ"], "nasdaq_filled": ["QQQ1", "QQQ2"]})))
    conn.commit()
    html, md = _build(conn, cfg)
    for text in (html, md):
        assert "Data quality" in text and "ZZZ" in text and "QQQ1" in text and "Nasdaq" in text


def test_no_data_quality_note_when_clean(tmp_path, cfg):
    conn = make_db(tmp_path)
    html, md = _build(conn, cfg)
    assert "Data quality" not in html and "Data quality" not in md


def test_tracking_updates_and_open_table(tmp_path, cfg):
    conn = make_db(tmp_path)
    cfg.paths.reports_dir.mkdir(parents=True, exist_ok=True)
    (cfg.paths.reports_dir / f"{PREV.isoformat()}.md").write_text("old", encoding="utf-8")
    old = date(2026, 9, 15)
    add_result(conn, make_rec("AAA", signal=old, rank=3), "target_hit", AS_OF, 1.4)
    add_result(conn, make_rec("BBB", signal=old, rank=4), "stopped", AS_OF, -1.05)
    add_result(conn, make_rec("CCC", signal=old, rank=5), "expired", AS_OF, 0.0)
    add_result(conn, make_rec("OLD", signal=old, rank=6), "time_stop", date(2026, 9, 20), 0.3)  # before window
    insert_rec(conn, make_rec("OPN", signal=date(2026, 9, 28), rank=1, status="open", current_r=0.45))
    insert_rec(conn, make_rec("PND", signal=date(2026, 9, 29), rank=2, status="pending"))
    conn.commit()
    html, md = _build(conn, cfg)
    _assert_clean(html, md)
    updates = md.split("## Tracking updates")[1].split("## Open recommendations")[0]
    assert "Target hit" in updates and "+1.40R" in updates
    assert "Stopped out" in updates and "-1.05R" in updates
    assert "Expired" in updates
    assert "OLD" not in updates
    open_section = md.split("## Open recommendations")[1].split("## Statistics")[0]
    assert "OPN" in open_section and "+0.45R" in open_section and "PND" in open_section
    assert "AAA" not in open_section  # finals are not open
    assert "Open recommendations (2)" in md


def test_statistics_section(tmp_path, cfg):
    conn = make_db(tmp_path)
    add_result(conn, make_rec("AAA", signal=date(2026, 9, 10), rank=1), "target_hit", date(2026, 9, 20), 1.4)
    add_result(conn, make_rec("BBB", signal=date(2026, 9, 11), rank=2), "stopped", date(2026, 9, 21), -1.0)
    html, md = _build(conn, cfg)
    stats = md.split("## Statistics")[1]
    assert "A - Momentum pullback" in stats
    assert "not enough data yet" in stats  # only 2 of 30 closed
    assert "By rank bucket" in stats and "1-10" in stats
    assert "Baseline R" in stats


def test_summary_text(tmp_path, cfg):
    conn = make_db(tmp_path)
    for i, t in enumerate(["AAA", "BBB", "CCC", "OLD"], 1):
        insert_rec(conn, make_rec(t, rank=i))
    conn.commit()
    text = report.build_summary(conn, AS_OF, config=cfg)
    assert "Regime: Favorable" in text and "New recommendations: 4" in text
    assert "AAA" in text and "CCC" in text and "OLD" not in text  # top 3 only
    assert "entry $100.00" in text and "stop $95.00" in text and "target $110.00" in text
    assert "not financial advice" in text


def test_summary_text_unfavorable(tmp_path, cfg):
    conn = make_db(tmp_path, regime="Unfavorable")
    text = report.build_summary(conn, AS_OF, config=cfg)
    assert "No new recommendations" in text


def test_missing_regime_row_still_renders(tmp_path, cfg):
    conn = make_db(tmp_path)
    conn.execute("DELETE FROM regime_log")
    conn.commit()
    html, md = _build(conn, cfg)
    assert "Regime data unavailable" in md


def test_rebuild_is_idempotent(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA"))
    first = _build(conn, cfg)
    second = _build(conn, cfg)  # same-day report file exists but must not count as "previous"
    assert first == second


def test_module_label():
    assert report.module_label("c_insider_cluster") == "C - Insider cluster"
    assert report.module_label(None) == "n/a"
