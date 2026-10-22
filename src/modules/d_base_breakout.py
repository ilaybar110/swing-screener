"""Module D -- base breakout (docs/PLAN.md section 7-D).

Interpretation notes (also in docs/status/BOT_4.md):
  * Base = the L sessions ending the day before the trigger, with L in
    [base_min_weeks*5, base_max_weeks*5] (15-40 with the shipped config).
  * Contracting: ATR14 at the base's last session < 0.7 x ATR14 at the session just
    before the base began.
  * Trigger: close > base high (highest high of the base) on volume >= min_volume_multiple
    x the 50-day average volume ending the day before. Any L that satisfies everything
    qualifies; the longest one is reported.
  * Stop = max(base midpoint, entry - atr_stop_multiple x ATR) i.e. the tighter of the two.
  * Trend template (Minervini-style stage-2 filter): on the trigger day close > SMA150 and
    close > SMA200, and SMA200 is above its value ``trend_sma_slope_sessions`` (20)
    sessions earlier. A ticker without 200 + 20 sessions of history cannot qualify.
  * Base depth = (base high - base low) / base high must be <= ``max_base_depth_pct``
    (0.25), checked per candidate base length L: a deeper "base" is a correction.
  Both filters are practitioner rules fixed a priori, not fitted to results.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.modules.base import PanelModule, ScanContext, f, param

ATR_DAYS = 14
VOL_AVG_DAYS = 50
CONTRACTION_RATIO = 0.7
TREND_TEMPLATE = True
TREND_SMA_MID = 150
TREND_SMA_LONG = 200
TREND_SMA_SLOPE_SESSIONS = 20
MAX_BASE_DEPTH_PCT = 0.25


class BaseBreakout(PanelModule):
    name = "base_breakout"

    def __init__(self, config) -> None:
        super().__init__(config)
        c = config.modules.d_base_breakout
        self.high_floor = 1.0 - c.pct_of_52w_high_max
        self.min_len = c.base_min_weeks * 5
        self.max_len = c.base_max_weeks * 5
        self.min_vol_mult = c.min_volume_multiple
        self.atr_mult = c.atr_stop_multiple
        self.contraction = param(c, "contraction_ratio", CONTRACTION_RATIO)
        self.trend_template = param(c, "trend_template", TREND_TEMPLATE)
        self.sma_mid = param(c, "trend_sma_mid", TREND_SMA_MID)
        self.sma_long = param(c, "trend_sma_long", TREND_SMA_LONG)
        self.slope_sessions = param(c, "trend_sma_slope_sessions", TREND_SMA_SLOPE_SESSIONS)
        self.max_depth = param(c, "max_base_depth_pct", MAX_BASE_DEPTH_PCT)

    def scan_ticker(self, ticker: str, df: pd.DataFrame, ctx: ScanContext) -> list[Candidate]:
        n = len(df)
        if n < self.max_len + ATR_DAYS + 2:
            return []
        c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
        atr = ind.atr(df, ATR_DAYS)
        hi52 = ind.high_52w(df)
        vol_ratio = v / ind.avg_volume(df, VOL_AVG_DAYS).shift(1)
        sma_mid, sma_long = ind.sma(c, self.sma_mid), ind.sma(c, self.sma_long)
        sma_long_ref = sma_long.shift(self.slope_sessions)
        trend = (
            (c > sma_mid) & (c > sma_long) & (sma_long > sma_long_ref)
            if self.trend_template
            else pd.Series(True, index=df.index)
        )
        gate = (c >= self.high_floor * hi52) & (vol_ratio >= self.min_vol_mult) & trend

        best_len = np.zeros(n, dtype=int)
        best_ratio = np.full(n, np.nan)
        best_high = np.full(n, np.nan)
        best_low = np.full(n, np.nan)
        atr_end = atr.shift(1)  # ATR14 at the base's last session (day before trigger)
        for L in range(self.min_len, self.max_len + 1):
            base_high = h.shift(1).rolling(L, min_periods=L).max()
            base_low = lo.shift(1).rolling(L, min_periods=L).min()
            atr_start = atr.shift(L + 1)  # session just before the base began
            ratio = atr_end / atr_start
            shallow = (base_high - base_low) / base_high <= self.max_depth
            ok = (ratio < self.contraction) & (c > base_high) & shallow & gate
            sel = ok.to_numpy()
            best_len[sel] = L
            best_ratio[sel] = ratio.to_numpy()[sel]
            best_high[sel] = base_high.to_numpy()[sel]
            best_low[sel] = base_low.to_numpy()[sel]

        idx = np.flatnonzero((best_len > 0) & df.index.isin(ctx.eval_dates))
        out: list[Candidate] = []
        for i in idx:
            entry = float(h.iloc[i])
            atr_i = float(atr.iloc[i])
            mid = (best_high[i] + best_low[i]) / 2.0
            stop = max(mid, entry - self.atr_mult * atr_i)
            if not self.stop_ok(entry, stop):
                continue
            L, ratio, vr = int(best_len[i]), float(best_ratio[i]), float(vol_ratio.iloc[i])
            avgv_prev = float(v.iloc[i] / vol_ratio.iloc[i])  # 50-day average ending the day before
            score = 100.0 * (
                0.4 * ind.clip01((self.contraction - ratio) / 0.4)
                + 0.3 * ind.clip01((L - self.min_len) / (self.max_len - self.min_len))
                + 0.3 * ind.clip01((vr - self.min_vol_mult) / 2.0)
            )
            out.append(
                Candidate(
                    ticker=ticker,
                    module=self.name,
                    signal_date=df.index[i].date(),
                    entry=round(entry, 4),
                    stop=round(stop, 4),
                    setup_score=round(score, 2),
                    rationale=(
                        f"Broke out above a {L}-session base (volatility contracted to "
                        f"{ratio:.0%} of its starting ATR) on {vr:.1f}x average volume, "
                        f"within {1 - c.iloc[i] / hi52.iloc[i]:.0%} of the 52-week high."
                    ),
                    details={
                        "base_length": L,
                        "base_high": f(best_high[i]),
                        "base_low": f(best_low[i]),
                        "atr_contraction_ratio": f(ratio, 3),
                        "volume_ratio": f(vr, 2),
                        "atr14": f(atr_i),
                        "high_52w": f(hi52.iloc[i]),
                        "base_start": df.index[i - L].date().isoformat(),
                        "base_end": df.index[i - 1].date().isoformat(),
                        "base_mid": f(mid),
                        "atr_start": f(atr.iloc[i - L - 1]),
                        "atr_start_date": df.index[i - L - 1].date().isoformat(),
                        "atr_end": f(atr.iloc[i - 1]),
                        "contraction_threshold": f(self.contraction),
                        "trigger_date": df.index[i].date().isoformat(),
                        "trigger_close": f(c.iloc[i]),
                        "breakout_volume": f(v.iloc[i], 0),
                        "avg_volume_50": f(avgv_prev, 0),
                        "min_volume_multiple": f(self.min_vol_mult),
                        "close_vs_52w_high": f(c.iloc[i] / hi52.iloc[i]),
                        "atr_stop": f(entry - self.atr_mult * atr_i),
                        "atr_stop_multiple": f(self.atr_mult),
                        "min_base_sessions": int(self.min_len),
                        "max_base_sessions": int(self.max_len),
                        "base_depth_pct": f((best_high[i] - best_low[i]) / best_high[i]),
                        "max_base_depth_pct": f(self.max_depth),
                        "sma150": f(sma_mid.iloc[i]),
                        "sma200": f(sma_long.iloc[i]),
                        "sma200_slope_ref": f(sma_long_ref.iloc[i]),
                        "trend_sma_slope_sessions": int(self.slope_sessions),
                    },
                )
            )
        return out
