"""Ranking tests (Bot 4)."""

from __future__ import annotations

from datetime import date

import pytest

import src.ranking as ranking
from src.config import load_config
from src.contracts import Candidate, GuardrailResult
from src.data_access import DataAccess
from src.ranking import rank, track_scores

AS_OF = date(2024, 12, 31)


@pytest.fixture()
def cfg():
    return load_config()


def _all_tickers(conn):
    return [r[0] for r in conn.execute(
        "SELECT ticker FROM tickers WHERE ticker NOT IN ('SPY') AND sector_etf IS NOT NULL ORDER BY ticker")]


def _cand(data, ticker, module="momentum_pullback", score=50.0):
    bar = data.get_prices(ticker, AS_OF, AS_OF).iloc[0]
    return Candidate(ticker, module, AS_OF, float(bar["high"]), float(bar["high"]) * 0.94, score,
                     "r.", {})


def _set_regime(conn, regime):
    conn.execute("UPDATE regime_log SET regime=? WHERE date=?", (regime, AS_OF.isoformat()))
    conn.commit()


@pytest.fixture()
def env(writable_fixture_conn):
    return writable_fixture_conn, DataAccess(writable_fixture_conn)


def test_empty_candidates(env, cfg):
    conn, data = env
    assert rank([], AS_OF, data, conn, cfg) == []


def test_ranking_orders_by_total_and_assigns_ranks(env, cfg):
    conn, data = env
    cands = [_cand(data, "MOMA1", score=80), _cand(data, "CTRL01", score=20), _cand(data, "CTRL02", score=50)]
    recs = rank(cands, AS_OF, data, conn, cfg)
    assert [r.rank for r in recs] == [1, 2, 3]
    totals = [r.total_score for r in recs]
    assert totals == sorted(totals, reverse=True)
    assert all(0 <= r.total_score <= 100 for r in recs)
    assert all(r.guardrail_status == "unknown" for r in recs)  # stub until Bot 2 lands
    assert all(r.status == "PENDING" for r in recs)
    # score = .45*setup pct (within module) + .35*universe RS pct + .20*overlap(0), no track record
    for r in recs:
        c = r.details["score_components"]
        assert c["rs_percentile_universe"] == r.rs_pct
        expected = 0.45 * c["setup_percentile"] + 0.35 * c["rs_percentile_universe"]
        assert r.total_score == pytest.approx(expected, abs=1e-3)
        assert r.track_score == 0.0 and c["track_weight"] == 0.0


def test_overlap_merges_modules_and_scores_70(env, cfg):
    conn, data = env
    cands = [
        _cand(data, "MOMA1", "momentum_pullback", 40),
        _cand(data, "MOMA1", "base_breakout", 60),
        _cand(data, "CTRL01", "momentum_pullback", 90),
    ]
    recs = {r.ticker: r for r in rank(cands, AS_OF, data, conn, cfg)}
    assert len(recs) == 2
    assert recs["MOMA1"].modules == ["base_breakout", "momentum_pullback"]
    assert recs["MOMA1"].overlap_score == 70
    assert recs["CTRL01"].overlap_score == 0
    three = [_cand(data, "MOMA1", m, 50) for m in ("momentum_pullback", "base_breakout", "insider_cluster")]
    assert rank(three, AS_OF, data, conn, cfg)[0].overlap_score == 100


def test_guardrail_fail_is_dropped_unknown_and_pass_kept(env, cfg, monkeypatch):
    conn, data = env
    verdict = {"NEGA1": "fail", "MOMA1": "pass", "CTRL01": "unknown"}
    monkeypatch.setattr(
        ranking, "guardrail_evaluate",
        lambda t, a, d: GuardrailResult(verdict[t], [f"why {t}"], {}, {"ev_fcf": 1.0}),
    )
    recs = rank([_cand(data, t) for t in verdict], AS_OF, data, conn, cfg)
    assert {r.ticker for r in recs} == {"MOMA1", "CTRL01"}
    by = {r.ticker: r for r in recs}
    assert by["MOMA1"].guardrail_status == "pass" and by["MOMA1"].valuation_info == {"ev_fcf": 1.0}
    assert by["CTRL01"].guardrail_status == "unknown"
    assert sorted(r.rank for r in recs) == [1, 2]  # ranks computed after the drop


def test_guardrail_exception_becomes_unknown(monkeypatch, fixture_conn):
    import sys
    import types

    def boom(*a):
        raise RuntimeError("boom")

    fake = types.ModuleType("src.guardrail")
    fake.evaluate = boom
    monkeypatch.setitem(sys.modules, "src.guardrail", fake)
    res = ranking.guardrail_evaluate("MOMA1", AS_OF, DataAccess(fixture_conn))
    assert res.status == "unknown"


@pytest.mark.parametrize("regime,slots", [("Favorable", 10), ("Caution", 5), ("Unfavorable", 0)])
def test_regime_top_n_and_industry_cap(env, cfg, regime, slots):
    conn, data = env
    _set_regime(conn, regime)
    tickers = [t for t in _all_tickers(conn)]
    cands = [_cand(data, t, score=10 + i) for i, t in enumerate(tickers)]
    recs = rank(cands, AS_OF, data, conn, cfg)
    assert len(recs) == len(tickers)  # every recommendation is returned/tracked
    shown = [r for r in recs if r.in_report]
    assert len(shown) == slots
    per_ind: dict[str, int] = {}
    for r in shown:
        per_ind[r.industry] = per_ind.get(r.industry, 0) + 1
    assert all(n <= cfg.ranking.max_per_industry for n in per_ind.values())
    # the report slots go to the best-ranked eligible recommendations, in order
    assert [r.rank for r in shown] == sorted(r.rank for r in shown)


def test_industry_cap_skips_to_next_best(env, cfg):
    conn, data = env
    _set_regime(conn, "Favorable")
    tech = [r[0] for r in conn.execute("SELECT ticker FROM tickers WHERE industry='Technology Industry' ORDER BY ticker")]
    assert len(tech) >= 3
    other = [r[0] for r in conn.execute("SELECT ticker FROM tickers WHERE industry='Energy Industry' ORDER BY ticker LIMIT 1")]
    # tech names score highest, the Energy name lowest (but above min_setup_score)
    cands = [_cand(data, t, score=90 - i) for i, t in enumerate(tech[:3])] + [_cand(data, other[0], score=30)]
    recs = rank(cands, AS_OF, data, conn, cfg)
    flags = {r.ticker: r.in_report for r in recs}
    assert sum(flags[t] for t in tech[:3]) == 2
    assert flags[other[0]] is True  # the 3rd tech name does not consume a slot


def test_track_record_locked_below_threshold(env, cfg):
    conn, data = env
    _seed_results(conn, "momentum_pullback", 29, 2.0)
    assert track_scores(conn, AS_OF, cfg) == {}
    recs = rank([_cand(data, "MOMA1")], AS_OF, data, conn, cfg)
    assert recs[0].track_score == 0.0


def test_track_record_unlocks_at_30_and_rescales_weights(env, cfg):
    conn, data = env
    _seed_results(conn, "momentum_pullback", 30, 2.0)
    assert track_scores(conn, AS_OF, cfg) == {"momentum_pullback": 100.0}
    cands = [_cand(data, "MOMA1", "momentum_pullback", 70), _cand(data, "CTRL01", "base_breakout", 70)]
    recs = {r.ticker: r for r in rank(cands, AS_OF, data, conn, cfg)}
    a = recs["MOMA1"]
    assert a.track_score == 100.0
    c = a.details["score_components"]
    assert c["track_weight"] == pytest.approx(0.10)
    base = 0.45 * c["setup_percentile"] + 0.35 * c["rs_percentile_universe"]
    assert a.total_score == pytest.approx(0.9 * base + 10.0, abs=1e-3)
    assert recs["CTRL01"].track_score == 0.0  # other module still locked


def test_track_record_ignores_unentered_and_future_results(env, cfg):
    conn, _ = env
    _seed_results(conn, "momentum_pullback", 30, 1.0, entered=False)
    assert track_scores(conn, AS_OF, cfg) == {}
    _seed_results(conn, "base_breakout", 30, 1.0, exit_date="2025-03-01", prefix="F")
    assert track_scores(conn, AS_OF, cfg) == {}


def _seed_results(conn, module, n, r, entered=True, exit_date="2024-06-01", prefix="T"):
    for i in range(n):
        rid = f"2024-01-02_{prefix}{module[:3]}{i}"
        conn.execute(
            "INSERT INTO recommendations (id, source, signal_date, ticker, primary_module) "
            "VALUES (?, 'live', '2024-01-02', ?, ?)", (rid, f"{prefix}{i}", module),
        )
        conn.execute(
            "INSERT INTO recommendation_results (rec_id, final_status, entry_date, exit_date, r_multiple) "
            "VALUES (?, ?, ?, ?, ?)",
            (rid, "target_hit" if entered else "expired", "2024-01-03" if entered else None, exit_date, r),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Within-module setup percentile, absolute floor, per-module report slots
# ---------------------------------------------------------------------------


def test_percentile_is_mid_rank_so_a_lone_value_is_50():
    assert ranking._percentile([]) == []
    assert ranking._percentile([42.0]) == [50.0]
    assert ranking._percentile([10.0, 30.0]) == [25.0, 75.0]
    assert ranking._percentile([5.0, 5.0]) == [50.0, 50.0]


def test_setup_quality_is_percentiled_within_each_module(env, cfg):
    conn, data = env
    # module A scores generously, module D stingily: pooled, every A name would beat every D name
    cands = [
        _cand(data, "CTRL01", "momentum_pullback", 90), _cand(data, "CTRL02", "momentum_pullback", 80),
        _cand(data, "CTRL03", "base_breakout", 40), _cand(data, "CTRL04", "base_breakout", 30),
        _cand(data, "CTRL05", "insider_cluster", 25),
    ]
    by = {r.ticker: r.details["score_components"]["setup_percentile"] for r in rank(cands, AS_OF, data, conn, cfg)}
    assert by["CTRL01"] == by["CTRL03"] == 75.0  # best of its own module
    assert by["CTRL02"] == by["CTRL04"] == 25.0
    assert by["CTRL05"] == 50.0  # a module's only candidate is not "the best" by default


def test_module_a_rs_is_not_counted_twice(env, cfg):
    """Ranking reads Module A's setup score without its RS component (details)."""
    conn, data = env
    hi_rs = _cand(data, "CTRL01", "momentum_pullback", 90)  # high score mostly from RS
    hi_rs.details = {"setup_score_ex_rs": 30.0}
    lo_rs = _cand(data, "CTRL02", "momentum_pullback", 60)  # better pullback, weaker RS
    lo_rs.details = {"setup_score_ex_rs": 70.0}
    by = {r.ticker: r for r in rank([hi_rs, lo_rs], AS_OF, data, conn, cfg)}
    assert by["CTRL01"].details["score_components"]["setup_quality"] == 30.0
    assert by["CTRL02"].details["score_components"]["setup_percentile"] == 75.0
    assert by["CTRL01"].setup_score == 90  # the stored module score itself is unchanged
    assert ranking.setup_quality(by["CTRL02"]) == 70.0


def test_min_setup_score_keeps_weak_setups_tracked_but_unreported(env, cfg):
    conn, data = env
    _set_regime(conn, "Favorable")
    floor = cfg.ranking.min_setup_score
    recs = rank([_cand(data, "CTRL01", score=floor - 1), _cand(data, "CTRL02", score=floor)],
                AS_OF, data, conn, cfg)
    by = {r.ticker: r for r in recs}
    assert set(by) == {"CTRL01", "CTRL02"}  # both returned, i.e. tracked
    assert by["CTRL01"].in_report is False
    assert by["CTRL02"].in_report is True


def _industries(conn):
    return dict(conn.execute("SELECT ticker, industry FROM tickers").fetchall())


def test_each_module_gets_a_report_slot(env, cfg):
    conn, data = env
    _set_regime(conn, "Caution")  # 5 slots
    ind_of = _industries(conn)
    tickers = [t for t in _all_tickers(conn) if t.startswith("CTRL")]
    # pick A names from distinct industries so the industry cap does not interfere
    a_names, seen = [], set()
    for t in tickers:
        if ind_of[t] not in seen:
            a_names.append(t)
            seen.add(ind_of[t])
    a_names = a_names[:6]
    assert len(a_names) == 6
    d_name = next(t for t in tickers if t not in a_names)
    cands = [_cand(data, t, "momentum_pullback", 90 - i) for i, t in enumerate(a_names)]
    cands.append(_cand(data, d_name, "base_breakout", 25))
    recs = rank(cands, AS_OF, data, conn, cfg)
    shown = [r for r in recs if r.in_report]
    assert len(shown) == 5
    assert d_name in {r.ticker for r in shown}
    assert {r.primary_module for r in shown} == {"momentum_pullback", "base_breakout"}
    # the reserved slot displaces the weakest A name, not a stronger one
    shown_a = sorted((r for r in shown if r.primary_module == "momentum_pullback"), key=lambda r: r.rank)
    all_a = sorted((r for r in recs if r.primary_module == "momentum_pullback"), key=lambda r: r.rank)
    assert [r.ticker for r in shown_a] == [r.ticker for r in all_a[:4]]


def test_module_slot_respects_floor_industry_cap_and_unfavorable(env, cfg):
    conn, data = env
    tech = [r[0] for r in conn.execute("SELECT ticker FROM tickers WHERE industry='Technology Industry' ORDER BY ticker")]
    assert len(tech) >= 3
    cands = [_cand(data, tech[0], "momentum_pullback", 90), _cand(data, tech[1], "momentum_pullback", 80),
             _cand(data, tech[2], "base_breakout", 70)]  # D's only name is in a capped industry
    _set_regime(conn, "Favorable")
    flags = {r.ticker: r.in_report for r in rank(cands, AS_OF, data, conn, cfg)}
    assert sum(flags.values()) == cfg.ranking.max_per_industry == 2
    # below the floor: no guaranteed slot
    weak = [_cand(data, "CTRL01", "momentum_pullback", 90),
            _cand(data, "CTRL02", "base_breakout", cfg.ranking.min_setup_score - 5)]
    flags = {r.ticker: r.in_report for r in rank(weak, AS_OF, data, conn, cfg)}
    assert flags == {"CTRL01": True, "CTRL02": False}
    _set_regime(conn, "Unfavorable")
    assert not any(r.in_report for r in rank(cands, AS_OF, data, conn, cfg))


def test_min_slots_per_module_zero_is_plain_top_n(env, cfg):
    conn, data = env
    _set_regime(conn, "Caution")
    cfg.ranking.min_slots_per_module = 0
    tickers = [t for t in _all_tickers(conn) if t.startswith("CTRL")][:8]
    cands = [_cand(data, t, "momentum_pullback", 90 - i) for i, t in enumerate(tickers[:7])]
    cands.append(_cand(data, tickers[7], "base_breakout", 25))
    recs = rank(cands, AS_OF, data, conn, cfg)
    shown = [r for r in recs if r.in_report]
    assert len(shown) == 5
    # without the guarantee the report is simply the best 5 eligible by rank (industry cap aside)
    assert [r.rank for r in shown] == sorted(r.rank for r in shown)
