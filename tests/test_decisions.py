"""Regression tests for the audit decisions D1-D5 and requests R1-R3 (Bot 9)."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

import src.ranking as ranking
from backtest.runner import insert_recommendations, run
from src import guardrail, regime, report
from src.config import load_config
from src.contracts import Candidate, GuardrailResult
from src.data import edgar_daily as ed
from src.data import news
from src.data_access import DataAccess
from src.ranking import rank
from tests.report_helpers import AS_OF as REPORT_DAY
from tests.report_helpers import insert_rec, make_config, make_db, make_rec
from tests.test_guardrail import _insert_fundamental, _insert_ticker

CFG = load_config()
D1, D2, D3 = date(2024, 12, 12), date(2024, 12, 13), date(2024, 12, 31)


# ---------------------------------------------------------------------------
# D1 - breadth threshold lives in config.yaml
# ---------------------------------------------------------------------------


def test_breadth_threshold_is_in_config_and_still_50():
    assert CFG.regime.breadth_threshold_pct == 50


def _breadth_env(conn):
    """AAA above its 50-day SMA, BBB below it -> breadth exactly 50 %; SPY rising."""
    from src.utils.calendar import trading_days

    for table in ("regime_log", "prices", "universe_snapshots"):
        conn.execute(f"DELETE FROM {table}")
    days = trading_days(date(2023, 10, 1), date(2024, 2, 1))[-70:]
    rows = []
    for i, d in enumerate(days):
        spy = 400.0 + i
        rows.append(("SPY", d.isoformat(), spy, spy, spy, spy, spy, 1000, "yfinance"))
        for t, close in (("AAA", 50.0 + i * 0.5), ("BBB", 100.0 - i * 0.5)):
            rows.append((t, d.isoformat(), close, close, close, close, close, 1000, "yfinance"))
    conn.executemany("INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, "
                     "price_source) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
                     "VALUES (?,?,?,?,?)", [(days[-1].isoformat(), t, 60.0, 2e9, 3e7) for t in ("AAA", "BBB")])
    conn.commit()
    return days[-1]


@pytest.mark.parametrize("threshold,label", [(50, "Favorable"), (50.01, "Caution"), (10, "Favorable")])
def test_regime_reads_breadth_threshold_from_config(writable_fixture_conn, monkeypatch, threshold, label):
    as_of = _breadth_env(writable_fixture_conn)
    cfg = load_config()
    cfg.regime.breadth_threshold_pct = threshold
    monkeypatch.setattr(regime, "load_config", lambda: cfg)
    row = regime.compute_regime(writable_fixture_conn, as_of)
    assert row["breadth_pct"] == 50.0 and row["regime"] == label


def test_report_breadth_ok_uses_config_threshold(tmp_path):
    conn = make_db(tmp_path, regime="Caution")  # breadth 41 %
    cfg = make_config(tmp_path)
    assert report.build_context(conn, REPORT_DAY, cfg)["regime"]["breadth_ok"] is False
    cfg.regime.breadth_threshold_pct = 40
    assert report.build_context(conn, REPORT_DAY, cfg)["regime"]["breadth_ok"] is True


# ---------------------------------------------------------------------------
# D2 - pass_partial
# ---------------------------------------------------------------------------


def _seed_without_share_history(conn, ticker="PARTL", cik="0000000077"):
    """Operating income / FCF / debt are evaluable, share-count history is not."""
    _insert_ticker(conn, ticker, cik)
    for end in ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "total_debt", "2024-12-31", 20.0, "2024-12-31", period_type="instant")
    conn.commit()
    return ticker


def test_guardrail_pass_with_unevaluated_check_is_pass_partial(writable_fixture_conn):
    t = _seed_without_share_history(writable_fixture_conn)
    res = guardrail.evaluate(t, D3, DataAccess(writable_fixture_conn))
    assert res.status == "pass_partial"
    assert res.reasons == ["not evaluated: shares outstanding history unavailable"]
    assert res.metrics["unevaluated"] == ["shares outstanding history unavailable"]


def test_guardrail_missing_data_still_never_fails(writable_fixture_conn):
    conn = writable_fixture_conn
    _insert_ticker(conn, "NOFCF", "0000000066")
    for end in ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]:
        _insert_fundamental(conn, "0000000066", "NOFCF", "operating_income", end, -5.0, end)
    conn.commit()
    res = guardrail.evaluate("NOFCF", D3, DataAccess(conn))
    assert res.status in ("pass_partial", "unknown")  # never "fail" on missing data


def _cand(data, ticker, day, module="momentum_pullback", score=50.0, scale=1.0, stop=0.94, stop_abs=None):
    """``scale`` > 1 puts the buy-stop far above the market (never fills); ``stop_abs`` is the stop as a
    multiple of the day's high, so such an order can stay pending instead of dying on a close below the stop."""
    high = float(data.get_prices(ticker, day, day).iloc[0]["high"])
    px = high * scale
    return Candidate(ticker, module, day, px, high * stop_abs if stop_abs else px * stop, score, "r.", {})


def test_ranking_treats_pass_partial_exactly_like_pass(writable_fixture_conn, monkeypatch):
    conn = writable_fixture_conn
    data = DataAccess(conn)
    results = {}
    for status in ("pass", "pass_partial"):
        monkeypatch.setattr(ranking, "guardrail_evaluate",
                            lambda t, a, d, s=status: GuardrailResult(s, ["not evaluated: x"] if s != "pass" else [], {}, {}))
        conn.execute("DELETE FROM recommendations")
        recs = rank([_cand(data, t, D3, score=sc) for t, sc in (("MOMA1", 70.0), ("MOMB1", 50.0))],
                    D3, data, conn, CFG)
        results[status] = [(r.ticker, r.rank, r.total_score, r.in_report) for r in recs]
        assert {r.guardrail_status for r in recs} == {status}
    assert results["pass"] == results["pass_partial"]
    assert len(results["pass"]) == 2  # kept, not dropped


@pytest.fixture()
def partial_report(tmp_path):
    conn = make_db(tmp_path)
    rec = make_rec("AAA", guardrail="pass_partial")
    rec.guardrail_reasons = ["not evaluated: shares outstanding history unavailable"]
    insert_rec(conn, rec)
    conn.commit()
    html_path, md_path = report.build(conn, REPORT_DAY, config=make_config(tmp_path))
    return Path(html_path).read_text(encoding="utf-8"), Path(md_path).read_text(encoding="utf-8")


def test_report_shows_pass_partial_with_missing_checks(partial_report):
    html, md = partial_report
    assert "guardrail: Pass (partial)" in html
    assert "not evaluated: shares outstanding history unavailable" in html
    assert 'class="chip pass_partial"' in html
    assert "**Guardrail:** Pass (partial) - not evaluated: shares outstanding history unavailable" in md


# ---------------------------------------------------------------------------
# D3 - no second recommendation while one is PENDING / ACTIVE
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(writable_fixture_conn, monkeypatch):
    monkeypatch.setattr(ranking, "guardrail_evaluate", lambda t, a, d: GuardrailResult("pass", [], {}, {}))
    return writable_fixture_conn, DataAccess(writable_fixture_conn)


def _rank_and_save(env, cands, day):
    conn, data = env
    recs = rank(cands, day, data, conn, CFG)
    insert_recommendations(conn, recs)
    return recs


def _details(conn, rec_id):
    return json.loads(conn.execute("SELECT details FROM recommendations WHERE id = ?", (rec_id,)).fetchone()[0])


def test_repeat_signal_while_pending_is_recorded_not_recommended(env):
    conn, data = env
    first = _rank_and_save(env, [_cand(data, "MOMA1", D1)], D1)
    assert [r.ticker for r in first] == ["MOMA1"]

    second = _rank_and_save(env, [_cand(data, "MOMA1", D2, module="base_breakout")], D2)
    assert second == []
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1
    assert _details(conn, first[0].id)["repeat_signals"] == [{"date": "2024-12-13", "modules": ["base_breakout"]}]


def test_repeat_signals_accumulate_and_reprocessing_a_day_is_idempotent(env):
    conn, data = env
    first = _rank_and_save(env, [_cand(data, "MOMA1", D1)], D1)
    for _ in range(2):  # day D2 processed twice (rerun) -> still one entry
        _rank_and_save(env, [_cand(data, "MOMA1", D2, module="a"), _cand(data, "MOMA1", D2, module="b")], D2)
    _rank_and_save(env, [_cand(data, "MOMA1", date(2024, 12, 16))], date(2024, 12, 16))
    assert _details(conn, first[0].id)["repeat_signals"] == [
        {"date": "2024-12-13", "modules": ["a", "b"]},
        {"date": "2024-12-16", "modules": ["momentum_pullback"]},
    ]


def test_other_tickers_are_unaffected_by_a_live_recommendation(env):
    conn, data = env
    _rank_and_save(env, [_cand(data, "MOMA1", D1)], D1)
    second = _rank_and_save(env, [_cand(data, "MOMA1", D2), _cand(data, "MOMB1", D2)], D2)
    assert [r.ticker for r in second] == ["MOMB1"]


def test_new_recommendation_allowed_once_previous_expired(env):
    conn, data = env
    # An entry far above the market never fills: the order expires after 5 sessions.
    _rank_and_save(env, [_cand(data, "MOMA1", D1, scale=1.5, stop_abs=0.9)], D1)
    third = _rank_and_save(env, [_cand(data, "MOMA1", D3)], D3)
    assert [r.ticker for r in third] == ["MOMA1"]
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE ticker = 'MOMA1'").fetchone()[0] == 2


def test_new_recommendation_allowed_once_previous_stopped(env):
    conn, data = env
    # Entry just under the day's high fills next session; a stop at 99.9 % of entry is hit at once.
    _rank_and_save(env, [_cand(data, "MOMA1", D1, scale=0.98, stop=0.999)], D1)
    from src.tracker import update_all

    update_all(conn, D3, "live", CFG)
    final = conn.execute("SELECT final_status FROM recommendation_results").fetchone()
    assert final is not None, "test premise: the first recommendation is final by now"
    again = _rank_and_save(env, [_cand(data, "MOMA1", D3)], D3)
    assert [r.ticker for r in again] == ["MOMA1"]


def test_catch_up_uses_prices_not_the_stale_status_column(env):
    """Tracker has not run between the days of a catch-up: status stays 'pending' in the DB
    although the order has long expired. The repeat rule must still allow the new one."""
    conn, data = env
    _rank_and_save(env, [_cand(data, "MOMA1", D1, scale=1.5, stop_abs=0.9)], D1)
    assert conn.execute("SELECT status FROM recommendations").fetchone()[0].lower() == "pending"
    assert [r.ticker for r in _rank_and_save(env, [_cand(data, "MOMA1", D3)], D3)] == ["MOMA1"]


class _Repeater:
    """Signals MOMA1 on three consecutive sessions (D1, D2 and 2024-12-16)."""

    name = "repeater"
    days = [D1, D2, date(2024, 12, 16)]

    def scan(self, as_of, data):
        return self.scan_history(as_of, as_of, data)

    def scan_history(self, start, end, data):
        out = []
        for d in self.days:
            if start <= d <= end:
                px = float(data.get_prices("MOMA1", d, d)["close"].iloc[0])
                out.append(Candidate("MOMA1", self.name, d, px * 1.5, px * 0.9, 60.0, "x", {}))
        return out


def test_backtest_applies_the_same_repeat_rule(writable_fixture_conn):
    conn = writable_fixture_conn
    rid = run(date(2024, 12, 2), date(2024, 12, 20), conn=conn, config=CFG, modules=[_Repeater()],
              build_universe=False, print_fn=None)
    rows = conn.execute("SELECT id, signal_date, details FROM recommendations WHERE source = ?", (rid,)).fetchall()
    assert [r["signal_date"] for r in rows] == ["2024-12-12"]  # D2 and 12-16 were repeats
    assert [d["date"] for d in json.loads(rows[0]["details"])["repeat_signals"]] == ["2024-12-13", "2024-12-16"]
    assert conn.execute("SELECT COUNT(*) FROM baselines WHERE source = ?", (rid,)).fetchone()[0] == 3


def test_report_shows_still_valid_from_signal_date(tmp_path):
    conn = make_db(tmp_path)
    old = make_rec("AAA", signal=date(2026, 9, 28), status="open", current_r=0.4)
    old.details = {**old.details, "repeat_signals": [{"date": "2026-09-30", "modules": ["base_breakout"]}]}
    insert_rec(conn, old)
    conn.commit()
    html_path, md_path = report.build(conn, REPORT_DAY, config=make_config(tmp_path))
    for text in (Path(html_path).read_text(encoding="utf-8"), Path(md_path).read_text(encoding="utf-8")):
        assert "still valid from 2026-09-28" in text and "2026-09-30" in text


# ---------------------------------------------------------------------------
# R1 - EDGAR daily pass only for universe CIKs
# ---------------------------------------------------------------------------


def test_edgar_daily_only_fetches_universe_ciks(tmp_path):
    from tests.test_edgar_daily import FakeClient, IDX_TEXT, EIGHTK
    from src.db import init_db

    conn = init_db(tmp_path / "t.db")
    conn.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)",
                     [("YI", "0001738906")] + [(t, k.zfill(10)) for k, t in EIGHTK.items()])
    conn.commit()

    everything = FakeClient()
    ed.process_day(conn, date(2026, 9, 29), client=everything)
    assert len(everything.calls) == 4  # no universe snapshot: fall back to all tickers

    conn2 = init_db(tmp_path / "t2.db")
    conn2.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)",
                      [("YI", "0001738906")] + [(t, k.zfill(10)) for k, t in EIGHTK.items()])
    conn2.execute("INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
                  "VALUES ('2026-09-27', 'FLWS', 10, 2e9, 3e7)")
    conn2.execute("INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
                  "VALUES ('2026-09-20', 'AIR', 10, 2e9, 3e7)")  # older snapshot is ignored
    conn2.commit()
    narrowed = FakeClient()
    rep = ed.process_day(conn2, date(2026, 9, 29), client=narrowed)
    assert len(narrowed.calls) == 1 and "0001084869" in narrowed.calls[0]  # FLWS's 8-K only
    assert (rep["form4_seen"], rep["eightk_seen"]) == (0, 1)
    assert IDX_TEXT  # sample index really contains the other companies' filings


def test_universe_cik_map_keeps_every_share_class_of_a_universe_company(tmp_path):
    from src.db import init_db

    conn = init_db(tmp_path / "t.db")
    conn.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)",
                     [("GOOG", "1652044"), ("GOOGL", "1652044"), ("OUT", "5")])
    conn.execute("INSERT INTO universe_snapshots (snapshot_date, ticker, price, market_cap, adv20) "
                 "VALUES ('2026-09-27', 'GOOGL', 10, 2e9, 3e7)")
    conn.commit()
    assert ed.load_universe_cik_map(conn) == {"0001652044": ["GOOG", "GOOGL"]}


# ---------------------------------------------------------------------------
# R2 - catch-up capped per run
# ---------------------------------------------------------------------------


def test_max_catchup_days_is_in_config():
    assert CFG.daily.max_catchup_days == 5


# ---------------------------------------------------------------------------
# R3 - news relevance filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,ticker,name,expected", [
    ("Apple unveils new iPhone", "AAPL", "Apple Inc.", True),                 # name
    ("Why AAPL shares rose today", "AAPL", "Apple Inc.", True),               # ticker as a word
    ("AAPLX fund adds holdings", "AAPL", "Apple Inc.", False),                # ticker only inside a word
    ("Madison Pacific Properties reports", "MPC", "Marathon Petroleum Corporation", False),
    ("Marathon Petroleum lifts dividend", "MPC", "Marathon Petroleum Corporation", True),
    ("Marathon runners flood the city", "XYZ", "Marathon Petroleum Corporation", True),  # distinctive first word
    ("Murata ships new capacitor", "MFG", "Mizuho Financial Group, Inc.", False),
    ("Mizuho raises outlook", "MFG", "Mizuho Financial Group, Inc.", True),
    ("Rates move on the Fed", "ON", "ON Semiconductor Corporation", False),   # "on" is not the ticker ON
    ("Why ON stock rallied", "ON", "Nobody Corp", True),
    ("First Financial Bankshares posts record", "FFIN", "First Financial Bankshares, Inc.", True),
    ("First lady visits school", "FFIN", "First Financial Bankshares, Inc.", False),  # generic words alone don't count
])
def test_news_relevance(title, ticker, name, expected):
    assert news.is_relevant(title, ticker, name) is expected


def test_get_news_drops_irrelevant_items_and_logs_the_count(caplog):
    from tests.test_news import FakeSession, _rss  # noqa: F401

    feed = _rss(
        ("Marathon Petroleum lifts dividend", "https://a/1", "Tue, 29 Sep 2026 10:00:00 GMT", "Reuters"),
        ("Madison Pacific Properties reports profit", "https://a/2", "Tue, 29 Sep 2026 11:00:00 GMT", "Other"),
        ("MPC shares gain on refining margins", "https://a/3", "Mon, 28 Sep 2026 11:00:00 GMT", "Wire"),
    )

    class Session:
        def get(self, url, params=None, **kw):
            class R:
                content = feed

                def raise_for_status(self):
                    pass

            return R()

    with caplog.at_level("INFO", logger="src.data.news"):
        out = news.get_news("MPC", "Marathon Petroleum Corporation", date(2026, 9, 30), session=Session())
    assert [o["url"] for o in out] == ["https://a/1", "https://a/3"]
    assert re.search(r"get_news MPC .* 2 items \(2 filtered", caplog.text), caplog.text
