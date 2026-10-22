"""Recommendation-detail chart (Bot 10): details carry every annotation field, the
point-in-time recompute reproduces them, and the figure/checklist build for each module."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from dashboard import rec_chart, rec_data, rec_explain
from src import indicators as ind
from src.config import load_config
from src.data_access import DataAccess
from tests.test_modules_scan import _patch_gap_opens

CFG = load_config()  # Module A's pullback-quality rules relaxed: MOMA1 predates them
CFG.modules.a_momentum_pullback.max_pullback_volume_ratio = 1.0
CFG.modules.a_momentum_pullback.max_pullback_depth_atr = float("inf")
CFG.modules.a_momentum_pullback.min_close_location = 0.0
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "golden_candidates.json").read_text())["gap_patched"]
CASES = [
    ("momentum_pullback", "MOMA1"), ("earnings_gap_drift", "MOMB1"),
    ("insider_cluster", "MOMC1"), ("base_breakout", "MOMD1"),
]


@pytest.fixture()
def data(writable_fixture_conn):
    _patch_gap_opens(writable_fixture_conn)
    return DataAccess(writable_fixture_conn)


def _case(data, module, ticker):
    old = next(r for r in GOLDEN[module] if r["ticker"] == ticker)
    signal = date.fromisoformat(old["signal_date"])
    px = ind.load_prices(data, ticker, date(2020, 1, 1), date(2030, 1, 1))
    spy = ind.load_prices(data, "SPY", date(2020, 1, 1), date(2030, 1, 1))
    return old, signal, px, spy


@pytest.mark.parametrize("module,ticker", CASES)
def test_recompute_reproduces_original_details_and_adds_annotations(data, module, ticker):
    old, signal, px, spy = _case(data, module, ticker)
    got = rec_data.recompute_details(module, ticker, signal, px, spy, old["details"], data, CFG)
    assert got is not None
    for k, v in old["details"].items():
        assert got[k] == v, k
    assert rec_data.has_annotations(module, got)
    assert not rec_data.has_annotations(module, old["details"])  # golden = pre-annotation shape


@pytest.mark.parametrize("module,ticker", CASES)
def test_recompute_never_looks_past_the_signal_date(data, module, ticker):
    old, signal, px, spy = _case(data, module, ticker)
    clean = rec_data.recompute_details(module, ticker, signal, px, spy, old["details"], data, CFG)
    # bars after the signal (the fixture may end on it, so append absurd ones) must be ignored
    later = px[px.index > pd.Timestamp(signal)].copy()
    future = pd.DataFrame(
        {"open": 900.0, "high": 990.0, "low": 9.0, "close": 500.0, "volume": 1e12},
        index=pd.bdate_range(px.index[-1] + pd.Timedelta(days=1), periods=15))
    poisoned = pd.concat([px, future])
    poisoned.loc[later.index, ["open", "high", "low", "close"]] *= 7.0
    again = rec_data.recompute_details(module, ticker, signal, poisoned, spy, old["details"], data, CFG)
    assert again == clean


def _row(old, signal, module, ticker, details):
    entry, stop = old["entry"], old["stop"]
    return pd.Series({
        "id": f"{signal}_{ticker}", "ticker": ticker, "signal_date": signal.isoformat(), "status": "pending",
        "entry": entry, "stop": stop, "target": entry + 2 * (entry - stop),
        "valid_until": (signal + timedelta(days=7)).isoformat(), "earnings_date": None,
        "modules": [module], "primary_module": module, "setup_score": old["setup_score"], "rs_pct": 1.0,
        "overlap_score": 0.0, "track_score": 0.0, "total_score": 0.0, "rank": 1, "in_report": 0,
        "regime": "Favorable", "sector": None, "industry": None, "earnings_in_window": 0,
        "guardrail_status": "pass", "guardrail_reasons": [], "valuation_info": {}, "rationale": "",
        "llm_brief": None, "current_r": None, "last_updated": "x", "source": "live",
        "details": {"by_module": {module: {"entry": entry, "stop": stop, "setup_score": old["setup_score"],
                                           "details": details}}, "primary": details},
    })


@pytest.mark.parametrize("module,ticker", CASES)
def test_ensure_annotations_stored_vs_recomputed_and_checklist_passes(data, module, ticker):
    old, signal, px, spy = _case(data, module, ticker)
    legacy = _row(old, signal, module, ticker, old["details"])
    det, status = rec_data.ensure_annotations(module, legacy, px, spy, data, CFG)
    assert status == "recomputed"
    stored = _row(old, signal, module, ticker, det)
    det2, status2 = rec_data.ensure_annotations(module, stored, px, spy, data, CFG)
    assert status2 == "stored" and det2 == det
    # a split after the signal restates prices: stored (pre-split) levels must not be reused
    _, status3 = rec_data.ensure_annotations(module, stored, px, spy, data, CFG, force_recompute=True)
    assert status3 == "recomputed"
    checks = rec_explain.module_checks(module, det, old["entry"], old["stop"], CFG)
    assert len(checks) >= 5
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    comps = rec_explain.score_components(module, det, CFG)
    assert sum(p for _, _, p in comps) == pytest.approx(old["setup_score"], abs=0.05)


@pytest.mark.parametrize("module,ticker", CASES)
def test_figure_builds_with_module_toggle_and_outcome(data, module, ticker):
    old, signal, px, spy = _case(data, module, ticker)
    row = _row(old, signal, module, ticker, old["details"])
    det, _ = rec_data.ensure_annotations(module, row, px, spy, data, CFG)
    oc = rec_data.outcome_events(row, px, 1.0, CFG)
    rec = {"entry": row["entry"], "stop": row["stop"], "target": row["target"], "signal_date": signal,
           "valid_until": row["valid_until"], "earnings_date": signal + timedelta(days=15)}
    base = rec_chart.build_figure(rec, px, spy, {module: det}, [], oc)
    full = rec_chart.build_figure(rec, px, spy, {module: det}, [module], oc)
    names = {t.name for t in full.data}
    assert {"Entry", "Stop", "Target", "Signal date", "Valid until", "Next earnings"} <= names
    assert any(n and n.startswith(rec_chart.MODULE_LABEL[module][0] + " ") for n in names)
    assert len(full.data) > len(base.data)  # toggling the module on adds its annotations
    assert not any(n and n.startswith(rec_chart.MODULE_LABEL[module][0] + " ") for n in {t.name for t in base.data})
    assert all(t.hovertemplate for t in full.data if t.type == "scatter")  # every annotation explains itself
    json.loads(full.to_json())  # serialisable for Streamlit
