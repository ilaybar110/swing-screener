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

    def scan_ticker(self, ticker: str, df: pd.DataFrame, ctx: ScanContext) -> list[Candidate]:
        n = len(df)
        if n < self.max_len + ATR_DAYS + 2:
            return []
        c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
        atr = ind.atr(df, ATR_DAYS)
        hi52 = ind.high_52w(df)
        vol_ratio = v / ind.avg_volume(df, VOL_AVG_DAYS).shift(1)
        gate = (c >= self.high_floor * hi52) & (vol_ratio >= self.min_vol_mult)

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
            ok = (ratio < self.contraction) & (c > base_high) & gate
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
                    },
                )
            )
        return out
