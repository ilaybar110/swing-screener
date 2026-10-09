"""Module B -- earnings gap drift (docs/PLAN.md section 7-B).

An earnings event whose reaction session opened >= min_gap_pct above the prior close on
>= min_volume_multiple x the 50-day average volume, followed by 2-5 sessions that hold
the gap-day low inside a tight range, then a close above that range.

Interpretation notes (also in docs/status/BOT_4.md):
  * Reaction session: events derived from an 8-K (source ``8k_2.02``/``8-K_2.02``) carry
    the reaction session as ``event_date`` already; for calendar events (report date)
    ``before_open``/``BMO``/``during`` -> the event date itself, ``after_close``/``AMC`` ->
    the next session, unknown timing -> whichever of the two qualifies (first one wins).
    Events from every source in ``earnings_dates`` count.
  * Gap = open[g] / close[g-1] - 1 (a true opening gap, not a close-to-close move).
  * Volume baseline = 50-day average ending the session *before* the gap; the "pre-gap
    ATR14" is the ATR ending the same session.
  * Consolidation = the m sessions after the gap day (m in [hold_days_min,
    hold_days_max]); the trigger is the session right after it. Only the first trigger
    per gap is emitted.
"""

from __future__ import annotations

import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.modules.base import PanelModule, ScanContext, f, param

ATR_DAYS = 14
VOL_AVG_DAYS = 50
TIGHT_RANGE_ATR_MULT = 1.5
STOP_ATR_BUFFER = 0.1
EVENT_LOOKBACK_DAYS = 30


def _text(value) -> str:
    return value if isinstance(value, str) else ""


class EarningsGapDrift(PanelModule):
    name = "earnings_gap_drift"

    def __init__(self, config) -> None:
        super().__init__(config)
        c = config.modules.b_earnings_gap_drift
        self.min_gap = c.min_gap_pct
        self.min_vol_mult = c.min_volume_multiple
        self.hold_min = c.hold_days_min
        self.hold_max = c.hold_days_max
        self.range_mult = param(c, "tight_range_atr_multiple", TIGHT_RANGE_ATR_MULT)
        self.stop_atr = param(c, "stop_atr_buffer", STOP_ATR_BUFFER)

    def _gap_sessions(self, events: pd.DataFrame, cal: pd.DatetimeIndex) -> list[int]:
        """Candidate reaction-session positions (indices into ``cal``)."""
        found: set[int] = set()
        for _, ev in events.iterrows():
            pos = int(cal.searchsorted(pd.Timestamp(ev["event_date"])))
            timing = _text(ev.get("timing")).lower()  # NULL arrives as NaN (a float) from pandas
            source = _text(ev.get("source")).lower()
            on_day = pos < len(cal) and cal[pos] == pd.Timestamp(ev["event_date"])
            if source.startswith(("8k", "8-k")):
                # derived from the 8-K acceptance time: event_date is already the reaction session
                found.add(pos)
            elif timing in ("after_close", "amc") and on_day:
                found.add(pos + 1)
            elif timing in ("before_open", "during", "bmo") or not on_day:
                found.add(pos)
            else:  # unknown timing on a trading day: either session may be the reaction
                found.update((pos, pos + 1))
        return sorted(p for p in found if 1 <= p < len(cal))

    def scan_ticker(self, ticker: str, df: pd.DataFrame, ctx: ScanContext) -> list[Candidate]:
        n = len(df)
        if n < VOL_AVG_DAYS + 2:
            return []
        first = df.index[0].date()
        events = ctx.data.get_earnings_events(
            ticker, first, ctx.end
        )
        if events.empty:
            return []
        o, h, lo, c, v = (df[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
        atr = ind.atr(df, ATR_DAYS)
        avgv = ind.avg_volume(df, VOL_AVG_DAYS)
        out: list[Candidate] = []
        used_gaps: set[int] = set()
        for g in self._gap_sessions(events, df.index):
            gap = o[g] / c[g - 1] - 1.0
            atr_pre = atr.iloc[g - 1]
            vbase = avgv.iloc[g - 1]
            if not (pd.notna(gap) and pd.notna(atr_pre) and pd.notna(vbase)):
                continue
            vol_ratio = v[g] / vbase
            if gap < self.min_gap or vol_ratio < self.min_vol_mult or g in used_gaps:
                continue
            used_gaps.add(g)
            for m in range(1, self.hold_max + 2):
                t = g + m
                if t >= n:
                    break
                # consolidation is sessions g+1 .. t-1 (m-1 sessions); trigger is t
                mm = m - 1
                if mm < self.hold_min:
                    # still building the minimum consolidation; it must already hold
                    if c[t] < lo[g]:
                        break
                    continue
                cons = slice(g + 1, t)
                if (c[cons] < lo[g]).any():
                    break
                rng = h[cons].max() - lo[cons].min()
                if rng > self.range_mult * atr_pre:
                    break
                cons_high = h[cons].max()
                if c[t] > cons_high:
                    out.extend(
                        self._emit(ticker, df, g, t, gap, vol_ratio, rng, atr_pre, atr, ctx)
                    )
                    break
        return out

    def _emit(self, ticker, df, g, t, gap, vol_ratio, rng, atr_pre, atr, ctx) -> list[Candidate]:
        if df.index[t] not in ctx.eval_dates:
            return []
        entry = float(df["high"].iloc[t])
        atr_t = float(atr.iloc[t])
        stop = float(df["low"].iloc[g]) - self.stop_atr * atr_t
        if not self.stop_ok(entry, stop):
            return []
        tight = ind.clip01(1.0 - rng / (self.range_mult * atr_pre))
        score = 100.0 * (
            0.4 * ind.clip01((gap - self.min_gap) / 0.10)
            + 0.3 * ind.clip01((vol_ratio - self.min_vol_mult) / 3.0)
            + 0.3 * tight
        )
        sessions = t - g - 1
        return [
            Candidate(
                ticker=ticker,
                module=self.name,
                signal_date=df.index[t].date(),
                entry=round(entry, 4),
                stop=round(stop, 4),
                setup_score=round(score, 2),
                rationale=(
                    f"Earnings gap of {gap:.1%} on {vol_ratio:.1f}x average volume on "
                    f"{df.index[g].date()}, held above the gap-day low in a tight "
                    f"{sessions}-session range, then closed above the range high."
                ),
                details={
                    "gap_date": df.index[g].date().isoformat(),
                    "gap_pct": f(gap),
                    "volume_ratio": f(vol_ratio, 2),
                    "consolidation_sessions": int(sessions),
                    "consolidation_range": f(rng),
                    "pre_gap_atr14": f(atr_pre),
                    "gap_day_low": f(df["low"].iloc[g]),
                    "atr14": f(atr_t),
                },
            )
        ]
