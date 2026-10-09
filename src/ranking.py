"""Ranking (owned by Bot 4) -- docs/PLAN.md section 9.

``rank`` turns a day's raw candidates into ranked ``Recommendation``s. Every
recommendation that survives the fundamental guardrail is returned (all of them are
tracked); ``in_report`` marks the subset surfaced in the report.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from src import indicators as ind
from src.config import load_config
from src.contracts import Candidate, GuardrailResult, Recommendation
from src.repeats import split_repeats
from src.trade_plan import build
from src.utils.logging import get_logger

log = get_logger(__name__)


def _guardrail_stub(ticker: str, as_of: date, data) -> GuardrailResult:
    return GuardrailResult(status="unknown", reasons=["guardrail not available"])


def guardrail_evaluate(ticker: str, as_of: date, data) -> GuardrailResult:
    """Bot 2's ``src.guardrail.evaluate`` when present, else an "unknown" stub. Any
    error resolves to "unknown" (missing data never becomes a fail)."""
    try:
        from src.guardrail import evaluate
    except ImportError:
        return _guardrail_stub(ticker, as_of, data)
    try:
        return evaluate(ticker, as_of, data)
    except Exception as exc:  # noqa: BLE001 - never let one ticker break the run
        log.warning("guardrail failed for %s: %s", ticker, exc)
        return GuardrailResult(status="unknown", reasons=[f"guardrail error: {exc}"])


def _percentile(values: list[float]) -> list[float]:
    """0-100 percentile of each value within the list (top = 100, ties averaged)."""
    return list((pd.Series(values, dtype=float).rank(pct=True, method="average") * 100.0).round(4))


def universe_rs_pct(data, as_of: date, tickers: list[str], rs_lookback: int, rs_skip: int) -> dict[str, float]:
    """RS percentile (0-100, within the as_of universe) for each requested ticker."""
    uni = data.get_universe(as_of)
    universe = sorted(set(uni["ticker"].tolist()) | set(tickers))
    panel = ind.load_panel(data, universe, as_of, as_of)
    if ind.BENCHMARK not in panel:
        return {}
    cal = panel[ind.BENCHMARK].index
    in_uni = set(uni["ticker"].tolist())
    cols = [t for t in universe if t in panel]
    member = pd.DataFrame(
        {t: [t in in_uni] * len(cal) for t in cols}, index=cal
    )
    pct = ind.rs_percentile_panel(panel, member, rs_lookback, rs_skip)
    if pct.empty:
        return {}
    last = pct.iloc[-1]
    # Candidates outside the universe snapshot (should not happen) rank against it too.
    out = {t: float(last[t]) for t in tickers if t in last.index and pd.notna(last[t])}
    return out


def track_scores(conn, as_of: date, config, source: str = "live") -> dict[str, float]:
    """Track-record score (0-100) per module with >= track_record_min_closed_trades
    closed results (trades that actually entered, exited on/before ``as_of``). Score =
    clip(50 + 25 x average R, 0, 100). Modules below the threshold are absent."""
    try:
        rows = conn.execute(
            "SELECT r.primary_module, res.r_multiple FROM recommendation_results res "
            "JOIN recommendations r ON r.id = res.rec_id "
            "WHERE r.source = ? AND res.entry_date IS NOT NULL "
            "AND res.r_multiple IS NOT NULL AND res.exit_date <= ?",
            (source, as_of.isoformat()),
        ).fetchall()
    except Exception as exc:  # table missing/empty DB: no track record yet
        log.info("no track record available (%s)", exc)
        return {}
    by_mod: dict[str, list[float]] = defaultdict(list)
    for mod, r in rows:
        by_mod[mod].append(float(r))
    need = config.ranking.track_record_min_closed_trades
    return {
        m: float(np.clip(50.0 + 25.0 * np.mean(rs), 0.0, 100.0))
        for m, rs in by_mod.items()
        if len(rs) >= need
    }


def _report_slots(regime: Optional[str], config) -> int:
    r = (regime or "").lower()
    if r == "favorable":
        return config.report.top_n_favorable
    if r == "unfavorable":
        return config.report.top_n_unfavorable
    if r != "caution":
        log.warning("unknown regime %r; using the Caution report size", regime)
    return config.report.top_n_caution


def rank(
    candidates: list[Candidate],
    as_of: date,
    data,
    conn,
    config=None,
    source: str = "live",
) -> list[Recommendation]:
    """rank(candidates, as_of, data, conn) -> list[Recommendation].

    0. A ticker that still has a PENDING/ACTIVE recommendation gets no new one: the
       repeat signal is recorded on the existing recommendation (``src.repeats``).
    1. Merge candidates per ticker (``trade_plan.build``).
    2. Run the fundamental guardrail; drop only ``fail`` results.
    3. Score: percentile of setup quality and of relative strength within the day's
       candidates, the overlap score (1/2/3+ modules -> 0/70/100), and -- only for a
       primary module with enough closed live results -- a track-record component.
       The base weights are scaled by (1 - track weight) when the track component applies.
    4. Rank by total score; mark ``in_report`` by regime top-N and the per-industry cap.

    Returns every surviving recommendation, ranked.
    """
    config = config or load_config()
    todays = [c for c in candidates if c.signal_date == as_of]
    if len(todays) != len(candidates):
        log.warning("rank(): ignoring %d candidate(s) not dated %s", len(candidates) - len(todays), as_of)
    if not todays:
        return []

    by_ticker: dict[str, list[Candidate]] = defaultdict(list)
    for c in todays:
        by_ticker[c.ticker].append(c)
    by_ticker = defaultdict(list, split_repeats(by_ticker, conn, as_of, config, source))
    if not by_ticker:
        return []

    a_cfg = config.modules.a_momentum_pullback
    rs_map = universe_rs_pct(data, as_of, sorted(by_ticker), a_cfg.rs_lookback_days, a_cfg.rs_skip_recent_days)
    regime_row = data.get_regime(as_of)
    regime = regime_row["regime"] if regime_row else None

    recs: list[Recommendation] = []
    for ticker in sorted(by_ticker):
        rec = build(
            by_ticker[ticker], data, config, rs_pct=rs_map.get(ticker, 0.0),
            regime=regime or "Unknown", source=source,
        )
        gr = guardrail_evaluate(ticker, as_of, data)
        if gr.status == "fail":
            log.info("guardrail dropped %s: %s", ticker, "; ".join(gr.reasons))
            continue
        rec.guardrail_status = gr.status
        rec.guardrail_reasons = list(gr.reasons)
        rec.valuation_info = dict(gr.valuation_info)
        recs.append(rec)
    if not recs:
        return []

    rk = config.ranking
    overlap_map = {1: rk.overlap_score_1_module, 2: rk.overlap_score_2_modules}
    setup_pct = _percentile([r.setup_score for r in recs])
    rs_pct_c = _percentile([r.rs_pct for r in recs])
    tracks = track_scores(conn, as_of, config, source)
    for r, sp, rp in zip(recs, setup_pct, rs_pct_c):
        r.overlap_score = float(overlap_map.get(len(r.modules), rk.overlap_score_3plus_modules))
        track = tracks.get(r.primary_module)
        if track is not None:
            tw = rk.track_record_weight_once_unlocked
            r.track_score = track
        else:
            tw, r.track_score = 0.0, 0.0
        scale = 1.0 - tw
        r.total_score = round(
            scale * (rk.weight_setup_quality * sp + rk.weight_relative_strength * rp + rk.weight_overlap * r.overlap_score)
            + tw * r.track_score,
            4,
        )
        r.details["score_components"] = {
            "setup_percentile": sp,
            "rs_percentile_in_candidates": rp,
            "overlap_score": r.overlap_score,
            "track_score": r.track_score,
            "track_weight": tw,
        }

    recs.sort(key=lambda r: (-r.total_score, -r.setup_score, r.ticker))
    slots = _report_slots(recs[0].regime, config)
    per_industry: dict[str, int] = defaultdict(int)
    shown = 0
    for i, r in enumerate(recs, start=1):
        r.rank = i
        r.in_report = False
        if shown >= slots:
            continue
        if r.industry:
            if per_industry[r.industry] >= rk.max_per_industry:
                continue
            per_industry[r.industry] += 1
        r.in_report = True
        shown += 1
    log.info("ranked %d recommendation(s) for %s; %d in report", len(recs), as_of, shown)
    return recs
