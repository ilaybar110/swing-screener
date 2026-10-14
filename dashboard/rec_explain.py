"""The "Why this setup" checklist: every rule a module applied, with the actual value and
the threshold (no Streamlit imports: unit-testable). Values come from the module's
``details``; thresholds from the module's own constants and config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from src import indicators as ind
from src.config import Config
from src.modules import a_momentum_pullback as mod_a
from src.modules import c_insider_cluster as mod_c


@dataclass
class Check:
    rule: str
    actual: str
    threshold: str
    ok: Optional[bool]  # None = informational / cannot be evaluated


def _g(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    return float(v) if isinstance(v, (int, float)) else None


def _chk(rule, actual, threshold, ok) -> Check:
    return Check(rule, actual, threshold, None if ok is None else bool(ok))


def stop_check(entry: float, stop: float, config: Config) -> Check:
    lo, hi = config.modules.stop_distance_pct_min, config.modules.stop_distance_pct_max
    d = (entry - stop) / entry
    return _chk("Stop distance", f"{d:.1%}", f"allowed {lo:.0%}-{hi:.0%}", lo <= d <= hi)


def _a(d: dict, config: Config) -> list[Check]:
    c = config.modules.a_momentum_pullback
    rs_min = 100.0 * (1.0 - c.rs_top_pct)
    out = []
    rs = _g(d, "rs_pct")
    out.append(_chk("Relative-strength percentile (6-month vs SPY, last 21 sessions skipped)",
                    "-" if rs is None else f"{rs:.0f}", f">= {rs_min:.0f}", None if rs is None else rs >= rs_min))
    close, hi52 = _g(d, "trigger_close"), _g(d, "high_52w")
    floor = 1.0 - c.pct_of_52w_high_max
    if close is not None and hi52:
        out.append(_chk("Close vs 52-week high", f"{close / hi52:.1%} of high", f">= {floor:.0%}", close / hi52 >= floor))
    sma50, ref = _g(d, "sma50"), _g(d, "sma50_slope_ref")
    if close is not None and sma50:
        out.append(_chk(f"Close above {c.sma_slow}-day SMA", f"{close:.2f} vs {sma50:.2f}", "close > SMA", close > sma50))
    if sma50 is not None and ref is not None:
        out.append(_chk(f"{c.sma_slow}-day SMA rising ({mod_a.SMA_SLOPE_SESSIONS} sessions)",
                        f"{sma50:.2f} vs {ref:.2f} before", "rising", sma50 > ref))
    low, sma20 = _g(d, "touch_window_low"), _g(d, "sma20")
    if low is not None and sma20 and sma50:
        lim20, lim50 = (1 + mod_a.TOUCH_PCT) * sma20, (1 + mod_a.TOUCH_PCT) * sma50
        out.append(_chk(f"Pulled back to the {c.sma_fast}/{c.sma_slow}-day SMA (last {mod_a.TOUCH_WINDOW} sessions)",
                        f"lowest low {low:.2f}",
                        f"<= {lim20:.2f} (SMA{c.sma_fast}+2%) or <= {lim50:.2f} (SMA{c.sma_slow}+2%)",
                        low <= max(lim20, lim50)))
    mc = _g(d, "min_close_vs_sma50")
    if mc is not None:
        out.append(_chk(f"Never closed far below the {c.sma_slow}-day SMA (last {mod_a.TOUCH_WINDOW} sessions)",
                        f"lowest close {mc:.1%} of SMA", f">= {mod_a.CLOSE_FLOOR_PCT:.0%}", mc >= mod_a.CLOSE_FLOOR_PCT))
    n = d.get("pullback_sessions")
    if n is not None:
        out.append(_chk("Pullback length", f"{n} sessions", f">= {mod_a.MIN_PULLBACK_SESSIONS} sessions",
                        n >= mod_a.MIN_PULLBACK_SESSIONS))
    vr = _g(d, "pullback_volume_ratio")
    if vr is not None:
        out.append(_chk("Pullback avg volume vs 20-day avg (volume dry-up)", f"{vr:.2f}x", "< 1.00x", vr < 1.0))
    tc, pdh = _g(d, "trigger_close"), _g(d, "prior_day_high")
    if tc is not None and pdh is not None:
        out.append(_chk("Trigger: close above the prior day's high", f"{tc:.2f} vs {pdh:.2f}", "close > prior high", tc > pdh))
    return out


def _b(d: dict, config: Config) -> list[Check]:
    cfg = config.modules.b_earnings_gap_drift
    gap, vr = _g(d, "gap_pct"), _g(d, "volume_ratio")
    mg = _g(d, "min_gap_pct") if _g(d, "min_gap_pct") is not None else cfg.min_gap_pct
    mv = _g(d, "min_volume_multiple") if _g(d, "min_volume_multiple") is not None else cfg.min_volume_multiple
    hmin = d.get("hold_days_min", cfg.hold_days_min)
    hmax = d.get("hold_days_max", cfg.hold_days_max)
    rm = _g(d, "tight_range_atr_multiple") or 1.5
    out = []
    if gap is not None:
        out.append(_chk("Opening gap (open vs prior close)", f"{gap:+.1%}", f">= {mg:.0%}", gap >= mg))
    if vr is not None:
        out.append(_chk("Gap-day volume vs 50-day average", f"{vr:.2f}x", f">= {mv:.1f}x", vr >= mv))
    n = d.get("consolidation_sessions")
    if n is not None:
        out.append(_chk("Consolidation length", f"{n} sessions", f"{hmin}-{hmax} sessions", hmin <= n <= hmax))
    mcl, gl = _g(d, "min_close_in_consolidation"), _g(d, "gap_day_low")
    if mcl is not None and gl is not None:
        out.append(_chk("Closes hold the gap-day low", f"lowest close {mcl:.2f}", f">= {gl:.2f} (gap-day low)", mcl >= gl))
    rng, atr = _g(d, "consolidation_range"), _g(d, "pre_gap_atr14")
    if rng is not None and atr:
        out.append(_chk("Consolidation range vs pre-gap ATR14", f"{rng / atr:.2f}x ATR", f"<= {rm:.1f}x ATR", rng <= rm * atr))
    tc, ch = _g(d, "trigger_close"), _g(d, "consolidation_high")
    if tc is not None and ch is not None:
        out.append(_chk("Trigger: close above consolidation high", f"{tc:.2f} vs {ch:.2f}", "close > range high", tc > ch))
    return out


def _c(d: dict, config: Config) -> list[Check]:
    trades = d.get("trades") or []
    cfg = config.modules.c_insider_cluster
    n, tot = d.get("insider_count"), _g(d, "cluster_total_value")
    out = []
    if n is not None:
        out.append(_chk("Distinct insiders buying", str(n), f">= {cfg.min_distinct_insiders}", n >= cfg.min_distinct_insiders))
    if tot is not None:
        out.append(_chk("Cluster total value", f"${tot:,.0f}", f">= ${cfg.min_cluster_total_value:,.0f}",
                        tot >= cfg.min_cluster_total_value))
    if trades:
        vals = [t["value"] for t in trades if t.get("value") is not None]
        if vals:
            out.append(_chk("Smallest single purchase", f"${min(vals):,.0f}", f">= ${cfg.min_txn_value:,.0f}",
                            min(vals) >= cfg.min_txn_value))
        dates = pd.to_datetime([t["trade_date"] for t in trades])
        span = (dates.max() - dates.min()).days
        out.append(_chk("Purchases span", f"{span} days", f"<= {cfg.cluster_window_days} days", span <= cfg.cluster_window_days))
    tc, sm = _g(d, "trigger_close"), _g(d, "sma_at_trigger")
    if tc is not None and sm is not None:
        out.append(_chk(f"Trigger: close above {cfg.sma_confirm_days}-day SMA", f"{tc:.2f} vs {sm:.2f}", "close > SMA", tc > sm))
    s = d.get("sessions_since_completion")
    if s is not None:
        cap = mod_c.COMPLETION_WINDOW_SESSIONS - 1
        out.append(_chk("Trigger within sessions of cluster completion", f"{s} sessions", f"<= {cap} sessions", s <= cap))
    return out


def _d(d: dict, config: Config) -> list[Check]:
    cfg = config.modules.d_base_breakout
    out = []
    L = d.get("base_length")
    if L is not None:
        lo, hi = cfg.base_min_weeks * 5, cfg.base_max_weeks * 5
        out.append(_chk("Base length", f"{L} sessions ({L / 5:.0f} weeks)", f"{lo}-{hi} sessions", lo <= L <= hi))
    r = _g(d, "atr_contraction_ratio")
    thr = _g(d, "contraction_threshold") or 0.7
    if r is not None:
        out.append(_chk("ATR14 contraction (base end vs base start)", f"{r:.2f}x", f"< {thr:.2f}x", r < thr))
    tc, bh = _g(d, "trigger_close"), _g(d, "base_high")
    if tc is not None and bh is not None:
        out.append(_chk("Trigger: close above base high", f"{tc:.2f} vs {bh:.2f}", "close > base high", tc > bh))
    vr = _g(d, "volume_ratio")
    mv = _g(d, "min_volume_multiple") or cfg.min_volume_multiple
    if vr is not None:
        out.append(_chk("Breakout volume vs 50-day average", f"{vr:.2f}x", f">= {mv:.1f}x", vr >= mv))
    cf = _g(d, "close_vs_52w_high")
    if cf is not None:
        floor = 1.0 - cfg.pct_of_52w_high_max
        out.append(_chk("Close vs 52-week high", f"{cf:.1%} of high", f">= {floor:.0%}", cf >= floor))
    return out


_BUILDERS = {
    "momentum_pullback": _a, "earnings_gap_drift": _b, "insider_cluster": _c, "base_breakout": _d,
}


def module_checks(module: str, details: dict[str, Any], entry: float, stop: float, config: Config) -> list[Check]:
    """Checklist for one module. ``entry``/``stop`` are that module's own levels."""
    return _BUILDERS[module](details, config) + [stop_check(entry, stop, config)]


def score_components(module: str, details: dict[str, Any], config: Config) -> list[tuple[str, str, float]]:
    """(label, how it is measured, points) pieces of the module's 0-100 setup_score,
    rebuilt with the module's own formulas."""
    clip = ind.clip01
    if module == "momentum_pullback":
        rs, vr, dep = _g(details, "rs_pct"), _g(details, "pullback_volume_ratio"), _g(details, "pullback_depth_atr")
        if None in (rs, vr, dep):
            return []
        return [("Relative strength", f"RS percentile {rs:.0f}; weight 40", 40 * rs / 100),
                ("Volume dry-up", f"pullback volume {vr:.2f}x; weight 30", 30 * clip((1 - vr) / 0.5)),
                ("Shallow pullback", f"depth {dep:.1f} ATR; weight 30", 30 * clip(1 - (dep - 1) / 5))]
    if module == "earnings_gap_drift":
        c = config.modules.b_earnings_gap_drift
        gap, vr, rng, atr = (_g(details, k) for k in ("gap_pct", "volume_ratio", "consolidation_range", "pre_gap_atr14"))
        if None in (gap, vr, rng, atr) or not atr:
            return []
        rm = _g(details, "tight_range_atr_multiple") or 1.5
        return [("Gap size", f"{gap:+.1%}; weight 40", 40 * clip((gap - c.min_gap_pct) / 0.10)),
                ("Gap-day volume", f"{vr:.1f}x; weight 30", 30 * clip((vr - c.min_volume_multiple) / 3.0)),
                ("Tight consolidation", f"{rng / atr:.2f}x ATR; weight 30", 30 * clip(1 - rng / (rm * atr)))]
    if module == "insider_cluster":
        c = config.modules.c_insider_cluster
        tot, n, sen = _g(details, "cluster_total_value"), details.get("insider_count"), _g(details, "seniority")
        if None in (tot, n, sen):
            return []
        span = 1_000_000 - c.min_cluster_total_value
        return [("Cluster size ($)", f"${tot:,.0f}; weight 40", 40 * clip((tot - c.min_cluster_total_value) / span)),
                ("Number of insiders", f"{n} insiders; weight 30", 30 * clip((n - c.min_distinct_insiders) / 3.0)),
                ("Seniority", f"{sen:.1f}; weight 30", 30 * sen)]
    if module == "base_breakout":
        c = config.modules.d_base_breakout
        r, L, vr = _g(details, "atr_contraction_ratio"), details.get("base_length"), _g(details, "volume_ratio")
        if None in (r, L, vr):
            return []
        contr = _g(details, "contraction_threshold") or 0.7
        lo, hi = c.base_min_weeks * 5, c.base_max_weeks * 5
        return [("ATR contraction", f"{r:.2f}x; weight 40", 40 * clip((contr - r) / 0.4)),
                ("Base length", f"{L} sessions; weight 30", 30 * clip((L - lo) / (hi - lo))),
                ("Breakout volume", f"{vr:.1f}x; weight 30", 30 * clip((vr - c.min_volume_multiple) / 2.0))]
    return []
