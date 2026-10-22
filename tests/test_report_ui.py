"""Report UI: trade strip / sparkline, open-position status board, honest track record,
source labels, shared module labels, optional stats keys and the Telegram HTML digest."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import report
from tests.report_helpers import AS_OF, GOOD_BRIEF, add_result, insert_rec, make_config, make_db, make_rec


@pytest.fixture()
def cfg(tmp_path):
    return make_config(tmp_path)


def _sessions(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def add_prices(conn, ticker: str, end: date, closes: list[float], highs: list[float] | None = None) -> list[date]:
    days = _sessions(end, len(closes))
    for i, (d, c) in enumerate(zip(days, closes)):
        h = highs[i] if highs else c * 1.005
        conn.execute("INSERT OR REPLACE INTO prices (ticker, date, open, high, low, close, adj_close, volume) "
                     "VALUES (?,?,?,?,?,?,?,?)", (ticker, d.isoformat(), c, h, c * 0.995, c, c, 1000))
    conn.commit()
    return days


def _build(conn, cfg):
    h, m = report.build(conn, AS_OF, config=cfg)
    return Path(h).read_text(encoding="utf-8"), Path(m).read_text(encoding="utf-8")


def test_module_labels_are_shared_and_prefix_agnostic():
    from dashboard import rec_chart

    assert report.module_label("momentum_pullback") == "A - Momentum pullback"
    assert report.module_label("a_momentum_pullback") == "A - Momentum pullback"
    assert report.module_label("base_breakout") == "D - Base breakout"
    assert report.module_label("x_new_thing") == "X - New thing"
    assert report.module_label("fake_module") == "Fake module"
    assert report.module_letter("insider_cluster") == "C"
    assert rec_chart.MODULE_LABEL["earnings_gap_drift"] == report.module_label("earnings_gap_drift")


@pytest.mark.parametrize("raw,short", [
    ("Amplitude Inc. Class A Common Stock", "Amplitude Inc."),
    ("Science Applications International Corporation Common Stock", "Science Applications International Corporation"),
    ("Genmab A/S American Depositary Shares", "Genmab A/S"),
    ("iShares Trust", "iShares Trust"),
    ("Alpha Corp", "Alpha Corp"),
])
def test_short_company(raw, short):
    assert report.short_company(raw) == short


def test_card_trade_strip_and_sparkline(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA", earnings_in_window=True, llm_brief=GOOD_BRIEF))
    add_prices(conn, "AAA", AS_OF, [90 + i * 0.15 for i in range(70)])  # last close 100.35 -> above entry
    insert_rec(conn, make_rec("BBB", rank=2, guardrail="pass_partial"))
    add_prices(conn, "BBB", AS_OF, [97.0] * 10)
    html, md = _build(conn, cfg)
    assert html.count('<svg class="spark"') == 2
    assert "Last 60 closes" in html and "Last 10 closes" in html  # at most 60 sessions
    assert "+3.1% to entry" in html and "+3.1% to entry" in md  # 100/97
    assert "at/above entry" in html  # AAA closed above its buy-stop
    assert 'class="bar"' in html and 'class="px" style="left:' in html
    assert 'badge earn' in html and "1 red flag" in html  # header badges
    assert 'chip pass_partial' in html
    css = html.split("<style>")[1].split("</style>")[0]
    assert ".chip.pass_partial,.chip.unknown{color:var(--warn)" in css  # amber, not green
    assert "<details><summary>AI brief" in html


def test_card_without_prices_has_no_strip(tmp_path, cfg):
    conn = make_db(tmp_path)
    insert_rec(conn, make_rec("AAA"))
    html, _ = _build(conn, cfg)
    assert '<svg' not in html and 'class="bar"' not in html
    assert "$100.00" in html and "$95.00" in html and "$110.00" in html


def test_source_labels(tmp_path, cfg):
    conn = make_db(tmp_path)
    conn.execute("INSERT INTO filings (accession, cik, ticker, form, filed_date) VALUES (?,?,?,?,?)",
                 ("0000000001-26-000001", "0000000001", "AAA", "8-K", "2026-09-28"))
    gn = "https://news.google.com/rss/articles/" + "X" * 250 + "?oc=5"
    brief = dict(GOOD_BRIEF, sources=["0000000001-26-000001", gn, gn, "https://www.reuters.com/a",
                                      "javascript:alert(1)"])
    insert_rec(conn, make_rec("AAA", llm_brief=brief))
    html, md = _build(conn, cfg)
    assert '>8-K</a>' in html and "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/" in html
    assert ">news: Google News</a>" in html and ">news: Google News #2</a>" in html
    assert ">news: reuters.com</a>" in html
    assert 'href="javascript' not in html
    assert "[8-K](<https://www.sec.gov/" in md
    assert gn not in html.split("Sources:")[1].split("</div>")[0].replace(f'href="{gn}"', "").replace(f'title="{gn}"', "")


def test_honest_track_record(tmp_path, cfg):
    conn = make_db(tmp_path)
    add_result(conn, make_rec("BBB", signal=date(2026, 9, 10), rank=2), "stopped", date(2026, 9, 21), -1.0)
    add_result(conn, make_rec("CCC", signal=date(2026, 9, 11), rank=3), "stopped", date(2026, 9, 22), -1.1)
    insert_rec(conn, make_rec("AAA"))
    html, md = _build(conn, cfg)
    for text in (html, md):
        assert "2 closed, win 0%, avg -1.05R" in text  # raw numbers on the card
        assert "preliminary" in text and "2/30" in text
    assert "Live record" in html and '<span class="neg">-1.05R</span>' in html  # scoreboard, colored
    assert "Live record: 2 closed, 0% wins, avg -1.05R" in md


def test_open_status_board(tmp_path, cfg):
    conn = make_db(tmp_path)
    days = _sessions(AS_OF, 40)
    # NEAR: entry 100 stop 95; filled 5 sessions ago, close 95.8 -> 0.16R above the stop
    near = make_rec("NEAR", signal=days[-7], status="open", current_r=-0.84, in_report=False)
    insert_rec(conn, near)
    add_prices(conn, "NEAR", AS_OF, [99.0] * 34 + [100.5, 99, 98, 97, 96.5, 95.8])
    # FAR: comfortable, close 104
    insert_rec(conn, make_rec("FAR", signal=days[-7], status="open", current_r=0.8))
    add_prices(conn, "FAR", AS_OF, [99.0] * 34 + [100.5, 101, 102, 103, 103.5, 104])
    insert_rec(conn, make_rec("PND", signal=days[-2], status="pending"))
    add_prices(conn, "PND", AS_OF, [98.0] * 5)
    insert_rec(conn, make_rec("TOD", status="pending"))  # today's signal: counted pending, not in the board
    insert_rec(conn, make_rec("TOH", status="pending", in_report=False, rank=None))
    html, md = _build(conn, cfg)
    ctx = report.build_context(conn, AS_OF, cfg)
    pos = ctx["positions"]
    assert [p["ticker"] for p in pos["open"]] == ["NEAR", "FAR"]  # nearest to stop first
    near_row = pos["open"][0]
    assert near_row["days"] == "5/20" and near_row["to_stop"] == "-0.8%" and near_row["to_target"] == "+14.8%"
    assert near_row["valid_until"] == "-"  # no entry-window date for open trades
    assert [p["ticker"] for p in pos["attention"]] == ["NEAR"] and "above stop" in near_row["attention"][0]
    assert not near_row["shown"] and "NEAR</b>&dagger;" in html
    assert [p["ticker"] for p in pos["pending"]] == ["PND"] and pos["pending"][0]["to_entry"] == "+2.0%"
    assert "the 1 below plus 2 signalled today (1 shown above, 1 tracked but not shown)" in pos["pending_note"]
    board = md.split("## Open recommendations")[1].split("## Statistics")[0]
    assert "**Needs attention**" in board and "### Pending entries (1)" in board
    assert '<span class="neg">-0.84R</span>' in html and 'class="n pos">+0.80R' in html


def test_partial_taken_moves_stop_to_breakeven(tmp_path, cfg):
    conn = make_db(tmp_path)
    days = _sessions(AS_OF, 30)
    insert_rec(conn, make_rec("PAR", signal=days[-9], status="open", current_r=1.2))
    closes = [99.0] * 22 + [100.5, 104, 108, 109, 107, 106, 106.5, 106]
    highs = [c * 1.005 for c in closes]
    highs[25] = 111.0  # target 110 touched
    add_prices(conn, "PAR", AS_OF, closes, highs)
    ctx = report.build_context(conn, AS_OF, cfg)
    row = ctx["positions"]["open"][0]
    assert row["partial"] and row["to_target"] == "partial taken"
    assert row["to_stop"] == "-5.2%"  # to the breakeven stop at the 100.50 fill (gap open)


def test_time_stop_attention(tmp_path, cfg):
    conn = make_db(tmp_path)
    days = _sessions(AS_OF, 45)
    insert_rec(conn, make_rec("OLD", signal=days[-20], status="open", current_r=0.4))
    add_prices(conn, "OLD", AS_OF, [99.0] * 26 + [100.5] + [102.0] * 18)
    row = report.build_context(conn, AS_OF, cfg)["positions"]["open"][0]
    assert row["days"] == "18/20" and row["left"] == 2
    assert any("time stop in 2 sessions" in a for a in row["attention"])
    assert row["time_stop"].startswith("~")


def test_optional_stats_keys(tmp_path, cfg, monkeypatch):
    from src import stats as stats_mod

    real = stats_mod.summary

    def richer(conn, source):
        out = real(conn, source)
        out["overall"]["avg_r_ci"] = [-1.4, -0.6]
        out["overall"]["evidence"] = "negative"
        out["evidence_by_module"] = {"a_momentum_pullback": "insufficient"}
        out["by_horizon"] = {"horizons": [5, 20], "overall": {
            "5d": {"n": 2, "mean_ret": 0.01, "mean_excess_vs_spy": -0.002, "mean_excess_vs_baseline": 0.004,
                   "excess_vs_baseline_ci": [-0.01, 0.02], "evidence": "insufficient"},
            "20d": {"n": 2, "mean_ret": -0.03, "mean_excess_vs_spy": -0.01, "mean_excess_vs_baseline": -0.012,
                    "excess_vs_baseline_ci": [-0.05, 0.02], "evidence": "no_edge"}}}
        return out

    monkeypatch.setattr(stats_mod, "summary", richer)
    conn = make_db(tmp_path)
    add_result(conn, make_rec("BBB", signal=date(2026, 9, 10)), "stopped", date(2026, 9, 21), -1.0)
    html, md = _build(conn, cfg)
    for text in (html, md):
        assert "-1.40 to -0.60R" in text
        assert "Forward returns by horizon" in text and "-1.2%" in text and "-5.0% to +2.0%" in text
    assert "A: insufficient" in html and "ev-negative" in html
    msgs = report.build_summary(conn, AS_OF, config=cfg).messages
    assert "20d vs baseline -1.2%" in msgs[0] and "Evidence: A insufficient" in msgs[0]


def test_absent_optional_keys_render_nothing(tmp_path, cfg):
    conn = make_db(tmp_path)
    add_result(conn, make_rec("BBB", signal=date(2026, 9, 10)), "stopped", date(2026, 9, 21), -1.0)
    html, md = _build(conn, cfg)
    assert "95% CI" not in html and "Forward returns" not in md


def test_telegram_digest_html_and_limits(tmp_path, cfg):
    conn = make_db(tmp_path)
    evil = dict(GOOD_BRIEF, red_flags=["a", "b"])
    for i in range(60):
        t = f"T{i:02d}" if i else "A<B"
        insert_rec(conn, make_rec(t, rank=i + 1, earnings_in_window=(i == 0), llm_brief=evil if i == 0 else None,
                                  modules=["momentum_pullback"]))
    add_prices(conn, "A<B", AS_OF, [98.0] * 5)
    digest = report.build_summary(conn, AS_OF, config=cfg)
    assert isinstance(digest, str) and "New recommendations: 60" in digest  # plain text for stdout
    assert "earnings in window" in digest and "2 red flag(s)" in digest
    msgs = digest.messages
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)
    first = msgs[0]
    assert "<b>#1 A&lt;B</b> · A - Momentum pullback" in first
    assert "Close $98.00 · entry $100.00 (<b>+2.0%</b> to trigger)" in first
    assert "R/R 2.0" in first and "⚠ earnings 2026-10-20 in window" in first and "⚑ 2 red flags" in first
    assert "Regime: <b>Favorable</b>" in first and "Record: 0 closed" in first
    joined = "\n".join(msgs)
    assert all(f"#{i} T" in joined for i in range(2, 61))
    assert "not financial advice" in msgs[-1]
    # tags balanced per message (each block is self-contained)
    for m in msgs:
        assert m.count("<b>") == m.count("</b>") and m.count("<i>") == m.count("</i>")
        assert "<B" not in re.sub(r"</?(b|i)>", "", m)


def test_telegram_unfavorable(tmp_path, cfg):
    conn = make_db(tmp_path, regime="Unfavorable")
    msgs = report.build_summary(conn, AS_OF, config=cfg).messages
    assert "No new recommendations (Unfavorable regime)." in msgs[0]
