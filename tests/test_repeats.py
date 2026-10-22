"""Stop-out re-entry cooldown (src/repeats.py, Bot 4)."""

from __future__ import annotations

from datetime import date

import pytest

import src.ranking as ranking
from backtest.runner import insert_recommendations
from src.config import load_config
from src.contracts import Candidate, GuardrailResult
from src.data_access import DataAccess
from src.ranking import rank
from src.repeats import find_recent_stopout

D1 = date(2024, 12, 12)  # signal; fills and is stopped out on 2024-12-13


@pytest.fixture()
def cfg():
    return load_config()


@pytest.fixture()
def env(writable_fixture_conn, monkeypatch):
    monkeypatch.setattr(ranking, "guardrail_evaluate", lambda t, a, d: GuardrailResult("pass", [], {}, {}))
    return writable_fixture_conn, DataAccess(writable_fixture_conn)


def _cand(data, ticker, day, module="momentum_pullback"):
    high = float(data.get_prices(ticker, day, day).iloc[0]["high"])
    return Candidate(ticker, module, day, high, high * 0.94, 50.0, "r.", {})


def _doomed(data, ticker, day):
    """Candidate whose buy-stop fills on the next session and whose stop is touched that
    same session (a same-day stop-out wins over the target)."""
    nxt = data.get_prices(ticker, day, date(2025, 1, 31)).iloc[1]
    return Candidate(ticker, "momentum_pullback", day, float(nxt["high"]) * 0.999,
                     float(nxt["low"]) * 1.001, 50.0, "r.", {})


def _rank_and_save(env, cfg, cands, day):
    conn, data = env
    recs = rank(cands, day, data, conn, cfg)
    insert_recommendations(conn, recs)
    return recs


def _stopped_first(env, cfg):
    conn, data = env
    first = _rank_and_save(env, cfg, [_doomed(data, "MOMA1", D1)], D1)
    hit = find_recent_stopout(conn, "MOMA1", date(2024, 12, 16), cfg, "live")
    assert hit is not None, "test premise: the first recommendation is stopped out"
    assert hit[1].exit_date == date(2024, 12, 13)
    return first


@pytest.mark.parametrize("day,module", [
    (date(2024, 12, 16), "momentum_pullback"),  # 1 session after the stop-out
    (date(2024, 12, 16), "base_breakout"),      # any module, not only the one stopped out
    (date(2024, 12, 30), "insider_cluster"),    # 10th session: still cooling down
])
def test_no_new_recommendation_during_cooldown(env, cfg, day, module):
    conn, data = env
    _stopped_first(env, cfg)
    assert _rank_and_save(env, cfg, [_cand(data, "MOMA1", day, module)], day) == []
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE ticker='MOMA1'").fetchone()[0] == 1


def test_new_recommendation_after_cooldown(env, cfg):
    conn, data = env
    _stopped_first(env, cfg)
    day = date(2024, 12, 31)  # 11th session after the stop-out
    assert [r.ticker for r in _rank_and_save(env, cfg, [_cand(data, "MOMA1", day)], day)] == ["MOMA1"]


def test_cooldown_does_not_touch_other_tickers_and_can_be_disabled(env, cfg):
    conn, data = env
    _stopped_first(env, cfg)
    day = date(2024, 12, 16)
    got = _rank_and_save(env, cfg, [_cand(data, "MOMA1", day), _cand(data, "MOMB1", day)], day)
    assert [r.ticker for r in got] == ["MOMB1"]
    cfg.ranking.stopout_cooldown_sessions = 0
    assert find_recent_stopout(conn, "MOMA1", day, cfg, "live") is None


def test_cooldown_is_point_in_time(env, cfg):
    """A stop-out is known from the session it happens on (signals are taken at the close),
    never earlier: the earlier recommendation is re-simulated on prices up to ``as_of``."""
    conn, data = env
    day = date(2024, 11, 29)
    # fills on 2024-12-02 (open < entry <= high), stopped on 2024-12-03 (low 56.58 < 57.0 < 57.83)
    nxt = data.get_prices("MOMA1", date(2024, 12, 2), date(2024, 12, 2)).iloc[0]
    _rank_and_save(env, cfg, [Candidate("MOMA1", "momentum_pullback", day, float(nxt["high"]) * 0.999,
                                         57.0, 50.0, "r.", {})], day)
    assert find_recent_stopout(conn, "MOMA1", date(2024, 12, 2), cfg, "live") is None  # open, not stopped
    hit = find_recent_stopout(conn, "MOMA1", date(2024, 12, 3), cfg, "live")
    assert hit is not None and hit[1].exit_date == date(2024, 12, 3)


def test_cooldown_other_source_is_ignored(env, cfg):
    conn, _ = env
    _stopped_first(env, cfg)
    assert find_recent_stopout(conn, "MOMA1", date(2024, 12, 16), cfg, "bt_other") is None
