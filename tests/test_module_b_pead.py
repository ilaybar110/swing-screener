"""Module B (earnings gap drift / PEAD) unit tests on synthetic price paths: gap-day close
position, the SMA-pullback trigger, the tighter stop anchor, 8-K preference and
during-hours acceptance, point-in-time calendar rows and the surprise score component.
No DB, no network: ``scan_ticker`` runs against a fake data layer."""

from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.modules.b_earnings_gap_drift import (
    TRIGGER_CONSOLIDATION, TRIGGER_PULLBACK, EarningsGapDrift,
)
from src.modules.base import ScanContext

CFG = load_config()
N_BASE = 70  # quiet sessions before the gap (enough for the 50-day volume average)


class FakeData:
    def __init__(self, events: list[dict], fundamentals: Optional[pd.DataFrame] = None):
        self.events = pd.DataFrame(events)
        self.fundamentals = fundamentals if fundamentals is not None else pd.DataFrame()

    def get_earnings_events(self, ticker, start, end, known_as_of=None):
        ev = self.events
        if ev.empty:
            return ev
        d = ev["event_date"]
        return ev[(d >= start.isoformat()) & (d <= end.isoformat())].sort_values("event_date").reset_index(drop=True)

    def get_fundamentals(self, ticker, as_of):
        fd = self.fundamentals
        if fd.empty:
            return fd
        return fd[fd["filed_date"] <= as_of.isoformat()]


def _frame(after: list[tuple[float, float, float, float, float]]) -> pd.DataFrame:
    """N_BASE quiet sessions around 100 followed by explicit (o, h, l, c, v) bars."""
    idx = pd.bdate_range("2024-01-02", periods=N_BASE + len(after))
    rows = []
    for i in range(N_BASE):
        c = 100.0 + (0.3 if i % 2 else 0.0)
        rows.append((c, c + 0.5, c - 0.5, c, 1_000_000.0))
    rows.extend(after)
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close", "volume"])


def _session(df: pd.DataFrame, i: int) -> str:
    return df.index[i].date().isoformat()


def _scan(df, data, end_pos: Optional[int] = None, cfg=CFG):
    mod = EarningsGapDrift(cfg)
    if end_pos is not None:
        df = df.iloc[: end_pos + 1]
    ctx = ScanContext(data=data, eval_dates=df.index, rs_pct=pd.DataFrame(index=df.index),
                      start=df.index[0].date(), end=df.index[-1].date())
    return mod.scan_ticker("TEST", df, ctx)


def _bmo(df, g, source="8k_2.02", **extra):
    row = {"ticker": "TEST", "event_date": _session(df, g), "timing": "BMO", "source": source,
           "acceptance_datetime": f"{_session(df, g)}T07:00:00" if source.startswith("8k") else None}
    row.update(extra)
    return row


GAP = (108.0, 111.0, 107.0, 110.5, 3_000_000.0)
CONSOLIDATION = [(110.4, 111.0, 109.7, 110.2, 1_500_000.0),
                 (110.2, 110.9, 109.6, 110.0, 1_400_000.0),
                 (110.0, 110.8, 109.8, 110.6, 1_300_000.0)]
BREAKOUT = (110.7, 112.0, 110.5, 111.6, 2_000_000.0)


def _breakout_df(gap=GAP):
    return _frame([gap, *CONSOLIDATION, BREAKOUT])


def test_consolidation_breakout_anchors_stop_on_the_consolidation_low():
    df = _breakout_df()
    got = _scan(df, FakeData([_bmo(df, N_BASE)]))
    assert len(got) == 1
    c = got[0]
    assert c.details["trigger_type"] == TRIGGER_CONSOLIDATION
    assert c.signal_date == df.index[N_BASE + 4].date()
    assert c.details["stop_anchor_source"] == "consolidation_low"
    assert c.details["stop_anchor"] == pytest.approx(109.6)
    assert c.stop == pytest.approx(109.6 - 0.1 * c.details["atr14"], abs=1e-3)
    assert c.details["surprise_points"] == 0.0 and c.details["surprise_source"] is None


def test_gap_day_close_must_be_in_the_upper_half_of_its_range():
    weak = (108.0, 111.0, 107.0, 108.8, 3_000_000.0)  # close at 45% of the range
    df = _breakout_df(gap=weak)
    assert _scan(df, FakeData([_bmo(df, N_BASE)])) == []


def test_large_gap_survives_the_stop_distance_cap_via_the_tighter_anchor():
    big = (118.0, 121.0, 101.0, 120.0, 4_000_000.0)  # 18% gap, gap-day low far below
    cons = [(120.0, 121.0, 119.6, 120.3, 1_500_000.0), (120.3, 120.9, 119.5, 120.1, 1_400_000.0),
            (120.1, 120.8, 119.7, 120.6, 1_300_000.0)]
    df = _frame([big, *cons, (120.7, 122.0, 120.5, 121.5, 2_000_000.0)])
    got = _scan(df, FakeData([_bmo(df, N_BASE)]))
    assert len(got) == 1
    c = got[0]
    assert (c.entry - c.stop) / c.entry < CFG.modules.stop_distance_pct_max
    # anchored on the gap-day low the stop would have been ~17% away and rejected
    assert (c.entry - 101.0) / c.entry > CFG.modules.stop_distance_pct_max


def _pullback_df():
    bars = [GAP]
    px = 110.5
    for _ in range(8):  # steady rally: the consolidation trigger cannot qualify
        px += 1.5
        bars.append((px - 1.0, px + 0.3, px - 1.2, px, 1_500_000.0))
    for _ in range(4):  # pull back toward the 10-day SMA
        px -= 2.0
        bars.append((px + 1.5, px + 1.8, px - 0.5, px, 1_200_000.0))
    bars.append((px, px + 0.6, px - 0.8, px + 0.2, 1_100_000.0))
    bars.append((px + 0.5, px + 5.5, px + 0.3, px + 5.0, 2_000_000.0))  # reclaim
    bars.append((px + 5.0, px + 6.0, px + 4.5, px + 5.5, 1_500_000.0))
    return _frame(bars)


def test_first_pullback_to_the_sma_then_reclaim_triggers():
    df = _pullback_df()
    got = _scan(df, FakeData([_bmo(df, N_BASE)]))
    pb = [c for c in got if c.details["trigger_type"] == TRIGGER_PULLBACK]
    assert len(pb) == 1, [c.details for c in got]
    c = pb[0]
    t = df.index.get_loc(pd.Timestamp(c.signal_date))
    assert df["close"].iloc[t] > df["high"].iloc[t - 1]
    assert c.details["trigger_close"] > c.details["trigger_sma"]
    assert c.details["pullback_low"] <= c.details["pullback_sma"] * (1 + CFG.modules.b_earnings_gap_drift.pullback_touch_pct)
    assert c.details["stop_anchor_source"] == "pullback_low"
    assert c.details["consolidation_start"] is None
    assert 0 < c.details["pullback_sessions_after_gap"] <= 30


def test_pullback_trigger_can_be_disabled():
    cfg = CFG.model_copy(deep=True)
    cfg.modules.b_earnings_gap_drift.pullback_trigger_enabled = False
    df = _pullback_df()
    assert not [c for c in _scan(df, FakeData([_bmo(df, N_BASE)]), cfg=cfg)
                if c.details["trigger_type"] == TRIGGER_PULLBACK]


def test_8k_accepted_during_market_hours_also_tries_the_acceptance_session():
    df = _breakout_df()
    g = N_BASE
    # 8-K accepted 10:30 on the gap day: edgar_daily maps it to the next session, no timing
    row = {"ticker": "TEST", "event_date": _session(df, g + 1), "timing": None, "source": "8k_2.02",
           "acceptance_datetime": f"{_session(df, g)}T10:30:00"}
    got = _scan(df, FakeData([row]))
    assert [c.details["gap_date"] for c in got] == [_session(df, g)]


def test_calendar_row_defers_to_a_nearby_8k_event():
    df = _breakout_df()
    g = N_BASE
    cal = _bmo(df, g, source="nasdaq_calendar")
    assert len(_scan(df, FakeData([cal]))) == 1
    # the 8-K says the reaction was two sessions later (no gap there): the calendar's guess
    # no longer counts once that 8-K exists
    eightk = _bmo(df, g + 2)
    assert _scan(df, FakeData([cal, eightk])) == []


def test_calendar_row_counts_only_from_its_known_date():
    df = _breakout_df()
    g = N_BASE
    after = (df.index[-1] + pd.Timedelta(days=1)).date().isoformat()  # listed after the trigger
    late = _bmo(df, g, source="nasdaq_calendar", known_date=after)
    assert _scan(df, FakeData([late])) == []
    early = _bmo(df, g, source="nasdaq_calendar", known_date=_session(df, g - 10))
    assert len(_scan(df, FakeData([early]))) == 1


def test_eps_surprise_adjusts_the_score_and_missing_data_is_neutral():
    df = _breakout_df()
    g = N_BASE
    base = _scan(df, FakeData([_bmo(df, g)]))[0]
    beat = _bmo(df, g, source="nasdaq_calendar", eps_actual=1.20, eps_forecast=1.00)
    up = _scan(df, FakeData([_bmo(df, g), beat]))[0]
    assert up.details["surprise_source"] == "nasdaq_eps"
    assert up.details["surprise_value"] == pytest.approx(0.20)
    assert up.details["surprise_points"] == pytest.approx(10.0)  # +20% saturates
    assert up.setup_score == pytest.approx(min(100.0, base.setup_score + 10.0), abs=0.01)
    miss = _bmo(df, g, source="nasdaq_calendar", eps_actual=0.95, eps_forecast=1.00)
    down = _scan(df, FakeData([_bmo(df, g), miss]))[0]
    assert down.details["surprise_points"] == pytest.approx(-2.5)
    # entry/stop never depend on the surprise
    assert (up.entry, up.stop) == (down.entry, down.stop) == (base.entry, base.stop)


def _revenue(df, g, current: float, filed_offset: int = 0) -> pd.DataFrame:
    gap_day = df.index[g]
    rows = []
    # 12 quarters ending ~30 days before the gap; growth +2/quarter-year with noise
    ends = [gap_day - pd.Timedelta(days=30) - pd.DateOffset(months=3 * k) for k in range(12)]
    rng = np.random.default_rng(7)
    for k, pe in enumerate(ends):
        value = 100.0 + 2.0 * (11 - k) / 4 + rng.normal(0, 0.5)
        filed = pe + pd.Timedelta(days=40)
        if k == 0:
            value = current
            filed = gap_day + pd.Timedelta(days=filed_offset)
        rows.append({"metric": "revenue", "period_start": (pe - pd.Timedelta(days=90)).date().isoformat(),
                     "period_end": pe.date().isoformat(), "filed_date": filed.date().isoformat(),
                     "value": value})
    return pd.DataFrame(rows)


def test_revenue_sue_is_used_when_no_eps_and_is_point_in_time():
    df = _breakout_df()
    g = N_BASE
    strong = _scan(df, FakeData([_bmo(df, g)], _revenue(df, g, current=130.0)))[0]
    assert strong.details["surprise_source"] == "xbrl_revenue_sue"
    assert strong.details["surprise_points"] == pytest.approx(10.0)
    # filed after the signal day: not usable -> neutral
    late = _scan(df, FakeData([_bmo(df, g)], _revenue(df, g, current=130.0, filed_offset=30)))[0]
    assert late.details["surprise_source"] is None and late.details["surprise_points"] == 0.0


@pytest.mark.parametrize("builder", [_breakout_df, _pullback_df])
def test_scan_ticker_is_causal_day_by_day(builder):
    """Truncating the history at each day reproduces exactly the full-range candidates
    of that day (the scan == scan_history equivalence, on the new triggers)."""
    df = builder()
    events = [_bmo(df, N_BASE), _bmo(df, N_BASE, source="nasdaq_calendar", eps_actual=1.1, eps_forecast=1.0,
                                     known_date=_session(df, N_BASE - 3))]
    full = _scan(df, FakeData(events))
    assert full
    for t in range(N_BASE, len(df)):
        day = df.index[t].date()
        part = [c for c in _scan(df, FakeData(events), end_pos=t) if c.signal_date == day]
        assert part == [c for c in full if c.signal_date == day]
