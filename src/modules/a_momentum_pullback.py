"""Module A -- momentum leader pullback (docs/PLAN.md section 7-A).

Leader (top RS percentile, near its 52-week high, above a rising 50-day SMA) that pulls
back to the 20/50-day SMA on dried-up volume and then closes above the prior day's high.

Interpretation notes (also in docs/status/BOT_4.md):
  * "last 5 sessions" = the 5 sessions ending on the trigger day, inclusive.
  * The pullback runs from the highest high of the 20 sessions *before* the trigger day
    (the swing high) to the day before the trigger; it needs >= 3 sessions.
  * Pullback volume is compared with the 20-day average volume ending the day before
    the trigger, so the trigger day's own volume never contaminates the comparison.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.modules.base import PanelModule, ScanContext, f, param

SWING_LOOKBACK = 20
MIN_PULLBACK_SESSIONS = 3
TOUCH_WINDOW = 5
TOUCH_PCT = 0.02
CLOSE_FLOOR_PCT = 0.97
SMA_SLOPE_SESSIONS = 10
STOP_ATR_BUFFER = 0.1
ATR_DAYS = 14
VOL_AVG_DAYS = 20


class MomentumPullback(PanelModule):
    name = "momentum_pullback"
    needs_rs = True

    def __init__(self, config) -> None:
        super().__init__(config)
        c = config.modules.a_momentum_pullback
        self.rs_lookback = c.rs_lookback_days
        self.rs_skip = c.rs_skip_recent_days
        self.rs_min_pct = 100.0 * (1.0 - c.rs_top_pct)
        self.high_floor = 1.0 - c.pct_of_52w_high_max
        self.sma_fast = c.sma_fast
        self.sma_slow = c.sma_slow
        self.touch_window = param(c, "pullback_touch_window", TOUCH_WINDOW)
        self.touch_pct = param(c, "pullback_touch_pct", TOUCH_PCT)
        self.close_floor = param(c, "pullback_close_floor", CLOSE_FLOOR_PCT)
        self.slope_sessions = param(c, "sma_slope_sessions", SMA_SLOPE_SESSIONS)
        self.stop_atr = param(c, "stop_atr_buffer", STOP_ATR_BUFFER)

    def scan_ticker(self, ticker: str, df: pd.DataFrame, ctx: ScanContext) -> list[Candidate]:
        n = len(df)
        if n < SWING_LOOKBACK + 2 or ticker not in ctx.rs_pct.columns:
            return []
        c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
        sma_f, sma_s = ind.sma(c, self.sma_fast), ind.sma(c, self.sma_slow)
        atr = ind.atr(df, ATR_DAYS)
        hi52 = ind.high_52w(df)
        avgv_prev = ind.avg_volume(df, VOL_AVG_DAYS).shift(1)
        rs_pct = ctx.rs_pct[ticker].reindex(df.index)

        cond = (
            (rs_pct >= self.rs_min_pct)
            & (c >= self.high_floor * hi52)
            & (c > sma_s)
            & (sma_s > sma_s.shift(self.slope_sessions))
        )
        touch = (lo <= (1 + self.touch_pct) * sma_f) | (lo <= (1 + self.touch_pct) * sma_s)
        w = self.touch_window
        touched = touch.astype(float).rolling(w, min_periods=w).max() == 1.0
        broke = (c < self.close_floor * sma_s).astype(float).rolling(w, min_periods=w).max() == 1.0
        trigger = c > h.shift(1)

        # Swing high / pullback structure over the 20 sessions ending the day before t.
        hv, lv, vv = h.to_numpy(), lo.to_numpy(), v.to_numpy()
        hw = ind.sliding(hv, SWING_LOOKBACK)[:-1]  # row i -> t = i + SWING_LOOKBACK
        lw = ind.sliding(lv, SWING_LOOKBACK)[:-1]
        vw = ind.sliding(vv, SWING_LOOKBACK)[:-1]
        bad = np.isnan(hw).any(1) | np.isnan(lw).any(1) | np.isnan(vw).any(1)
        k = np.argmax(np.where(np.isnan(hw), -np.inf, hw), axis=1)
        since = SWING_LOOKBACK - 1 - k  # pullback sessions after the swing high
        after = np.arange(SWING_LOOKBACK)[None, :] > k[:, None]
        with np.errstate(invalid="ignore", divide="ignore"):
            pull_vol = np.where(after, vw, 0.0).sum(1) / since
        pull_low = np.where(after, lw, np.inf).min(1)
        swing_high = hw.max(1)

        def pad(a: np.ndarray, fill: float = np.nan) -> np.ndarray:
            out = np.full(n, fill)
            out[SWING_LOOKBACK:] = a
            return out

        ok_struct = np.zeros(n, dtype=bool)
        ok_struct[SWING_LOOKBACK:] = (~bad) & (since >= MIN_PULLBACK_SESSIONS)
        pull_vol_a, pull_low_a, swing_a = pad(pull_vol), pad(pull_low), pad(swing_high)
        dry = pull_vol_a < avgv_prev.to_numpy()

        sig = (
            cond & touched & ~broke & trigger
            & pd.Series(ok_struct & dry, index=df.index)
        )
        idx = np.flatnonzero(sig.to_numpy() & df.index.isin(ctx.eval_dates))
        out: list[Candidate] = []
        for i in idx:
            entry = float(h.iloc[i])
            atr_i = float(atr.iloc[i])
            stop = float(pull_low_a[i]) - self.stop_atr * atr_i
            if not self.stop_ok(entry, stop):
                continue
            vol_ratio = float(pull_vol_a[i] / avgv_prev.iloc[i])
            depth_atr = float((swing_a[i] - pull_low_a[i]) / atr_i)
            rs = float(rs_pct.iloc[i])
            dry_score = ind.clip01((1.0 - vol_ratio) / 0.5)
            depth_score = ind.clip01(1.0 - (depth_atr - 1.0) / 5.0)
            score = 100.0 * (0.4 * rs / 100.0 + 0.3 * dry_score + 0.3 * depth_score)
            near = "20-day" if lo.iloc[max(0, i - w + 1): i + 1].min() <= (1 + self.touch_pct) * sma_f.iloc[i] else "50-day"
            out.append(
                Candidate(
                    ticker=ticker,
                    module=self.name,
                    signal_date=df.index[i].date(),
                    entry=round(entry, 4),
                    stop=round(stop, 4),
                    setup_score=round(score, 2),
                    rationale=(
                        f"Relative-strength leader (RS percentile {rs:.0f}) pulled back "
                        f"{depth_atr:.1f} ATR to the {near} SMA on {vol_ratio:.0%} of average "
                        f"volume, then closed above the prior day's high."
                    ),
                    details={
                        "rs_pct": f(rs, 2),
                        "pullback_low": f(pull_low_a[i]),
                        "swing_high": f(swing_a[i]),
                        "pullback_depth_atr": f(depth_atr, 2),
                        "pullback_volume_ratio": f(vol_ratio, 3),
                        "atr14": f(atr_i),
                        "sma20": f(sma_f.iloc[i]),
                        "sma50": f(sma_s.iloc[i]),
                        "high_52w": f(hi52.iloc[i]),
                    },
                )
            )
        return out
