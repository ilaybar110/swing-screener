"""Trade-plan tests (Bot 4)."""

from __future__ import annotations

from datetime import date

import pytest

from src.config import load_config
from src.contracts import Candidate
from src.data_access import DataAccess
from src.db import ensure_earnings_columns
from src.trade_plan import apply_earnings_policy, build, earnings_blackout_end, earnings_block, plan_levels
from src.utils.calendar import add_trading_days

CFG = load_config()
AS_OF = date(2024, 12, 31)


def _cand(ticker="MOMA1", module="momentum_pullback", score=50.0, entry=100.0, stop=94.0, d=AS_OF):
    return Candidate(ticker, module, d, entry, stop, score, f"{module} rationale.", {"k": 1})


def test_levels_target_is_entry_plus_2r(data_access):
    lv = plan_levels(_cand(), data_access, CFG)
    assert lv["target"] == pytest.approx(112.0)
    assert lv["valid_until"] == add_trading_days(AS_OF, 5)


def test_build_single_candidate(data_access):
    rec = build([_cand()], data_access, CFG, rs_pct=88.0, now=None)
    assert rec.id == "2024-12-31_MOMA1"
    assert rec.status == "PENDING" and rec.source == "live"
    assert rec.modules == ["momentum_pullback"] and rec.primary_module == "momentum_pullback"
    assert (rec.entry, rec.stop, rec.target) == (100.0, 94.0, 112.0)
    assert rec.valid_until == date(2025, 1, 8)  # 5 trading days after 2024-12-31 (NYSE)
    assert rec.sector == "Technology" and rec.industry == "Technology Industry"
    assert rec.rs_pct == 88.0 and rec.regime in {"Favorable", "Caution", "Unfavorable"}
    assert rec.details["stop_pct"] == pytest.approx(0.06)
    assert rec.guardrail_status == "unknown" and rec.llm_brief is None


def test_build_merges_same_day_candidates(data_access):
    a = _cand(module="momentum_pullback", score=40.0, entry=100.0, stop=94.0)
    d = _cand(module="base_breakout", score=75.0, entry=101.0, stop=95.0)
    rec = build([a, d], data_access, CFG)
    assert rec.modules == ["base_breakout", "momentum_pullback"]
    assert rec.primary_module == "base_breakout"
    assert (rec.entry, rec.stop) == (101.0, 95.0)
    assert rec.target == pytest.approx(113.0)
    assert rec.setup_score == 75.0
    assert set(rec.details["by_module"]) == {"base_breakout", "momentum_pullback"}
    assert "Also signaled by: momentum_pullback" in rec.rationale


def test_build_rejects_mixed_tickers_or_days(data_access):
    with pytest.raises(ValueError):
        build([_cand("MOMA1"), _cand("MOMB1")], data_access, CFG)
    with pytest.raises(ValueError):
        build([_cand(d=AS_OF), _cand(d=date(2024, 12, 30))], data_access, CFG)
    with pytest.raises(ValueError):
        build([], data_access, CFG)


def test_earnings_flag_point_in_time_and_window(writable_fixture_conn):
    conn = writable_fixture_conn
    data = DataAccess(conn)
    sig = date(2024, 11, 20)
    # The fixture's MOMB1 2.02 row (2024-12-03) was accepted after this date: unknowable.
    rec = build([_cand("MOMB1", d=sig)], data, CFG)
    assert rec.earnings_date is None and rec.earnings_in_window is False

    # A calendar entry 10 trading days out is visible and inside the 25-day window.
    conn.execute(
        "INSERT INTO earnings_dates (ticker, event_date, timing, source) VALUES "
        "('MOMB1', '2024-12-05', NULL, 'nasdaq_calendar')"
    )
    # ... and one far beyond it (but nearer ones win) for a second ticker.
    conn.execute(
        "INSERT INTO earnings_dates (ticker, event_date, timing, source) VALUES "
        "('MOMA1', '2025-02-20', NULL, 'nasdaq_calendar')"
    )
    conn.commit()
    rec = build([_cand("MOMB1", d=sig)], data, CFG)
    assert rec.earnings_date == date(2024, 12, 5) and rec.earnings_in_window is True
    rec = build([_cand("MOMA1", d=sig)], data, CFG)
    assert rec.earnings_date == date(2025, 2, 20) and rec.earnings_in_window is False


# --- earnings policy: skip candidates with a known report inside the blackout -------------


def _cfg(policy="skip", min_sessions=10):
    cfg = CFG.model_copy(deep=True)
    cfg.earnings.policy = policy
    cfg.earnings.min_sessions_before = min_sessions
    return cfg


def _cal(conn, ticker, day, known=None, timing="AMC"):
    ensure_earnings_columns(conn)
    conn.execute(
        "INSERT INTO earnings_dates (ticker, event_date, timing, source, known_date) "
        "VALUES (?, ?, ?, 'nasdaq_calendar', ?)", (ticker, day, timing, known))
    conn.commit()


def test_config_defaults_to_skip_with_ten_sessions():
    assert CFG.earnings.policy == "skip" and CFG.earnings.min_sessions_before == 10


def test_blackout_end_is_entry_window_plus_min_sessions():
    sig = date(2024, 11, 20)
    assert earnings_blackout_end(sig, _cfg()) == add_trading_days(sig, 5 + 10)


def test_skip_policy_blocks_known_earnings_inside_the_blackout(writable_fixture_conn):
    conn = writable_fixture_conn
    sig = date(2024, 11, 20)
    end = add_trading_days(sig, 15)
    _cal(conn, "MOMA1", end.isoformat())                         # last blackout session: blocked
    _cal(conn, "MOMD1", add_trading_days(sig, 16).isoformat())   # one session later: allowed
    data = DataAccess(conn)
    assert earnings_block(data, "MOMA1", sig, _cfg()) == end
    assert earnings_block(data, "MOMD1", sig, _cfg()) is None
    assert earnings_block(data, "MOMA1", sig, _cfg(policy="flag")) is None
    by = {"MOMA1": [_cand("MOMA1", d=sig)], "MOMD1": [_cand("MOMD1", d=sig)]}
    assert list(apply_earnings_policy(by, sig, data, _cfg())) == ["MOMD1"]
    assert list(apply_earnings_policy(by, sig, data, _cfg(policy="flag"))) == ["MOMA1", "MOMD1"]


def test_skip_policy_is_point_in_time(writable_fixture_conn):
    conn = writable_fixture_conn
    sig = date(2024, 11, 20)
    day = add_trading_days(sig, 5).isoformat()
    _cal(conn, "MOMA1", day, known="2024-11-21")  # first listed the day after the signal
    data = DataAccess(conn)
    assert earnings_block(data, "MOMA1", sig, _cfg()) is None
    assert earnings_block(data, "MOMA1", date(2024, 11, 21), _cfg()) is not None
    # an 8-K accepted after the signal day is unknowable too (fixture MOMB1 row, 2024-12-03)
    assert earnings_block(data, "MOMB1", date(2024, 11, 26), _cfg()) is None


def test_skip_policy_ignores_the_event_module_b_trades(writable_fixture_conn):
    # module B signals after its own report: that event's reaction is behind the signal
    data = DataAccess(writable_fixture_conn)
    assert earnings_block(data, "MOMB1", date(2024, 12, 10), _cfg()) is None
