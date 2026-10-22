"""Module A -- momentum leader pullback (docs/PLAN.md section 7-A).

Leader (top RS percentile, near its 52-week high, above a rising 50-day SMA) that pulls
back to the 20/50-day SMA on dried-up volume and then closes above the prior day's high.

Interpretation notes (also in docs/status/BOT_4.md):
  * "last 5 sessions" = the 5 sessions ending on the trigger day, inclusive.
  * The pullback runs from the highest high of the 20 sessions *before* the trigger day
    (the swing high) to the day before the trigger; it needs >= 3 sessions.
  * Pullback volume is compared with the 20-day average volume ending the day before
    the trigger, so the trigger day's own volume never contaminates the comparison.
    "Dried up" means the pullback's average volume is below ``max_pullback_volume_ratio``
    (0.8) of that average -- clearly below average, not merely a hair under it.
  * Pullback depth (swing high - pullback low, in trigger-day ATR14) is capped at
    ``max_pullback_depth_atr`` (3.5): a deeper drop is a breakdown, not an orderly pullback.
  * The trigger day must close in the upper part of its own range: close location
    ``(close - low) / (high - low) >= min_close_location`` (0.7). A zero-range day closes
    at its high and counts as 1.0.
  * ``setup_score`` keeps its RS component (it is the module's own quality read-out), but
    ``details["setup_score_ex_rs"]`` carries the same score without RS; ranking uses that
    one, because ranking already weighs relative strength separately (src/ranking.py).
  These three thresholds are practitioner rules of thumb fixed a priori, not fitted.
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
MAX_PULLBACK_VOLUME_RATIO = 0.8
MAX_PULLBACK_DEPTH_ATR = 3.5
MIN_CLOSE_LOCATION = 0.7


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
        self.max_vol_ratio = param(c, "max_pullback_volume_ratio", MAX_PULLBACK_VOLUME_RATIO)
        self.max_depth_atr = param(c, "max_pullback_depth_atr", MAX_PULLBACK_DEPTH_ATR)
        self.min_close_loc = param(c, "min_close_location", MIN_CLOSE_LOCATION)

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
        rng = h - lo
        close_loc = ((c - lo) / rng.where(rng > 0)).fillna(1.0).where(c.notna())
        trigger = (c > h.shift(1)) & (close_loc >= self.min_close_loc)

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
        with np.errstate(invalid="ignore"):
            dry = pull_vol_a < self.max_vol_ratio * avgv_prev.to_numpy()
            shallow = (swing_a - pull_low_a) <= self.max_depth_atr * atr.to_numpy()
        rs_line = ind.rs_raw(c, ctx.spy["close"], self.rs_lookback, self.rs_skip) if len(ctx.spy) else None

        sig = (
            cond & touched & ~broke & trigger
            & pd.Series(ok_struct & dry & shallow, index=df.index)
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
            score_ex_rs = 100.0 * (0.5 * dry_score + 0.5 * depth_score)
            # chart annotations (details only; nothing below feeds entry/stop/score)
            s_idx = i - SWING_LOOKBACK + int(k[i - SWING_LOOKBACK])  # swing-high session
            low_idx = s_idx + 1 + int(np.argmin(lv[s_idx + 1: i]))  # first pullback-low session
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
                        "swing_high_date": df.index[s_idx].date().isoformat(),
                        "pullback_low_date": df.index[low_idx].date().isoformat(),
                        "pullback_start": df.index[s_idx + 1].date().isoformat(),
                        "pullback_end": df.index[i - 1].date().isoformat(),
                        "pullback_sessions": int(i - 1 - s_idx),
                        "pullback_avg_volume": f(pull_vol_a[i], 0),
                        "avg_volume_20": f(avgv_prev.iloc[i], 0),
                        "trigger_date": df.index[i].date().isoformat(),
                        "trigger_close": f(c.iloc[i]),
                        "prior_day_high": f(h.iloc[i - 1]),
                        "close_vs_52w_high": f(c.iloc[i] / hi52.iloc[i]),
                        "sma50_slope_ref": f(sma_s.iloc[i - self.slope_sessions]),
                        "touched_sma": near,
                        "touch_window_low": f(lo.iloc[max(0, i - w + 1): i + 1].min()),
                        "min_close_vs_sma50": f((c.iloc[max(0, i - w + 1): i + 1] / sma_s.iloc[max(0, i - w + 1): i + 1]).min()),
                        "rs_raw": f(rs_line.iloc[i]) if rs_line is not None else None,
                        "rs_lookback": int(self.rs_lookback),
                        "rs_skip": int(self.rs_skip),
                        "close_location": f(close_loc.iloc[i], 3),
                        "min_close_location": f(self.min_close_loc),
                        "max_pullback_volume_ratio": f(self.max_vol_ratio),
                        "max_pullback_depth_atr": f(self.max_depth_atr),
                        "setup_score_ex_rs": f(score_ex_rs, 2),
                    },
                )
            )
        return out
