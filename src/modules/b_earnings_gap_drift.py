"""Module B -- earnings gap drift / post-earnings-announcement drift (docs/PLAN.md section 7-B).

An earnings event whose reaction session opened >= min_gap_pct above the prior close on
>= min_volume_multiple x the 50-day average volume and closed in the upper half of its
range, followed by either

  * trigger 1 (``consolidation_breakout``): 2-5 sessions that hold the gap-day low inside
    a tight range, then a close above that range; or
  * trigger 2 (``sma_pullback``): the first pullback to the 10/20-day SMA within
    ``pullback_window_sessions`` of the gap, then a reclaim (close back above the SMA and
    above the prior session's high), closes holding the gap-day low throughout.

This is the price-confirmed form of Bernard & Thomas (1989) post-earnings drift: the gap
on heavy volume stands in for the surprise, and the measured surprise (when available)
adjusts the setup score.

Interpretation notes (also in docs/status/BOT_4.md):
  * Reaction session: events derived from an 8-K (source ``8k_2.02``/``8-K_2.02``) carry
    the reaction session as ``event_date`` already; for calendar events (report date)
    ``before_open``/``BMO``/``during`` -> the event date itself, ``after_close``/``AMC`` ->
    the next session, unknown timing -> whichever of the two qualifies (first one wins).
  * An 8-K accepted *during* market hours has no timing and ``event_date`` = the next
    session (src/data/edgar_daily.py). Companies usually wire the release before the open
    and file the 8-K later, so the acceptance session itself is also a candidate reaction
    session (whichever qualifies).
  * 8-K events are preferred: a calendar row within ``prefer_8k_window_days`` of an 8-K
    event of the same ticker stops counting from that 8-K's event date on (its exact
    acceptance-derived session replaces the calendar's guess). A calendar row only counts
    from its ``known_date`` (point-in-time), an 8-K row from its acceptance date. Both
    checks depend only on the signal day, so ``scan`` and ``scan_history`` agree.
  * Gap = open[g] / close[g-1] - 1 (a true opening gap, not a close-to-close move).
  * Volume baseline = 50-day average ending the session *before* the gap; the "pre-gap
    ATR14" is the ATR ending the same session.
  * Consolidation = the m sessions after the gap day (m in [hold_days_min,
    hold_days_max]); the trigger is the session right after it. Only the first trigger
    of each kind per gap is emitted.
  * Stop = max(gap-day low, consolidation/pullback low) - stop_atr_buffer x ATR14: the
    tighter anchor, so a large gap with a wide gap-day range is not rejected by
    ``stop_distance_pct_max`` when price has based well above the gap-day low.
  * Surprise (score only, never a filter): Nasdaq reported EPS vs the consensus forecast
    when stored, else a revenue SUE (seasonal random walk with drift, Foster-Olsen-Shevlin
    1984 / Jegadeesh-Livnat 2006) from point-in-time XBRL quarterly revenue (EPS is not
    among the stored XBRL metrics). It adds -surprise_max_points..+surprise_max_points to
    the score; no data -> 0 (neutral).
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.modules.base import PanelModule, ScanContext, f, param
from src.utils.logging import get_logger

log = get_logger(__name__)

ATR_DAYS = 14
VOL_AVG_DAYS = 50
TIGHT_RANGE_ATR_MULT = 1.5
STOP_ATR_BUFFER = 0.1
EVENT_LOOKBACK_DAYS = 30
GAP_CLOSE_MIN_RANGE_POSITION = 0.5
PREFER_8K_WINDOW_DAYS = 3
PULLBACK_WINDOW_SESSIONS = 30
PULLBACK_SMA_DAYS = (10, 20)
PULLBACK_TOUCH_PCT = 0.02
SURPRISE_MAX_POINTS = 10.0
SURPRISE_EPS_SATURATION = 0.20
SURPRISE_SUE_SATURATION = 2.0
# revenue SUE: up to 8 prior seasonal changes, at least 4 (Foster-Olsen-Shevlin 1984)
SUE_MAX_HISTORY = 8
SUE_MIN_HISTORY = 4

TRIGGER_CONSOLIDATION = "consolidation_breakout"
TRIGGER_PULLBACK = "sma_pullback"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _is_8k(ev) -> bool:
    return _text(ev.get("source")).lower().startswith(("8k", "8-k"))


def _day(value) -> Optional[pd.Timestamp]:
    """ISO date/datetime text -> normalized Timestamp (None for NULL/blank)."""
    s = _text(value)
    return pd.Timestamp(s[:10]) if s else None


def _num(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


class _Source:
    """One earnings row backing a candidate reaction session."""

    __slots__ = ("ev", "known", "suppressed_from")

    def __init__(
        self, ev: pd.Series, known: Optional[pd.Timestamp], suppressed_from: Optional[pd.Timestamp]
    ) -> None:
        self.ev = ev
        self.known = known
        self.suppressed_from = suppressed_from

    def valid_at(self, ts: pd.Timestamp) -> bool:
        """Usable for a signal on ``ts``: already known, not yet replaced by an 8-K."""
        if self.known is not None and self.known > ts:
            return False
        return self.suppressed_from is None or ts < self.suppressed_from


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
        self.close_pos_min = param(c, "gap_close_min_range_position", GAP_CLOSE_MIN_RANGE_POSITION)
        self.prefer_8k_days = int(param(c, "prefer_8k_window_days", PREFER_8K_WINDOW_DAYS))
        self.pullback_on = bool(param(c, "pullback_trigger_enabled", True))
        self.pb_window = int(param(c, "pullback_window_sessions", PULLBACK_WINDOW_SESSIONS))
        self.pb_smas = tuple(int(k) for k in param(c, "pullback_sma_days", PULLBACK_SMA_DAYS))
        self.pb_touch = param(c, "pullback_touch_pct", PULLBACK_TOUCH_PCT)
        self.surprise_pts = float(param(c, "surprise_max_points", SURPRISE_MAX_POINTS))
        self.eps_sat = param(c, "surprise_eps_saturation", SURPRISE_EPS_SATURATION)
        self.sue_sat = param(c, "surprise_sue_saturation", SURPRISE_SUE_SATURATION)

    # -- event -> reaction sessions ------------------------------------------------

    @staticmethod
    def _event_sessions(ev: pd.Series, cal: pd.DatetimeIndex) -> set[int]:
        """Reaction-session positions (indices into ``cal``) one event can map to."""
        pos = int(cal.searchsorted(pd.Timestamp(ev["event_date"])))
        timing = _text(ev.get("timing")).lower()  # NULL arrives as NaN (a float) from pandas
        on_day = pos < len(cal) and cal[pos] == pd.Timestamp(ev["event_date"])
        if _is_8k(ev):
            # derived from the 8-K acceptance time: event_date is already the reaction session
            out = {pos}
            acc = _day(ev.get("acceptance_datetime"))
            if not timing and acc is not None:
                # accepted during market hours: a release wired before the open gaps on
                # the acceptance session itself
                apos = int(cal.searchsorted(acc))
                if apos < pos and cal[apos] == acc:
                    out.add(apos)
            return out
        if timing in ("after_close", "amc") and on_day:
            return {pos + 1}
        if timing in ("before_open", "during", "bmo") or not on_day:
            return {pos}
        # unknown timing on a trading day: either session may be the reaction
        return {pos, pos + 1}

    def _gap_sources(self, events: pd.DataFrame, cal: pd.DatetimeIndex) -> dict[int, list[_Source]]:
        """Candidate reaction-session positions (indices into ``cal``) -> backing rows,
        8-K rows first."""
        rows = [ev for _, ev in events.iterrows()]
        rows.sort(key=lambda ev: not _is_8k(ev))  # stable: 8-K rows first
        eightk_days = sorted(pd.Timestamp(ev["event_date"]) for ev in rows if _is_8k(ev))
        window = pd.Timedelta(days=self.prefer_8k_days)
        found: dict[int, list[_Source]] = {}
        for ev in rows:
            if _is_8k(ev):
                known, suppressed = _day(ev.get("acceptance_datetime")), None
            else:
                known = _day(ev.get("known_date"))
                day = pd.Timestamp(ev["event_date"])
                near = [d for d in eightk_days if abs(d - day) <= window]
                suppressed = min(near) if near else None
            for p in self._event_sessions(ev, cal):
                if 1 <= p < len(cal):
                    found.setdefault(p, []).append(_Source(ev, known, suppressed))
        return found

    def _gap_sessions(self, events: pd.DataFrame, cal: pd.DatetimeIndex) -> list[int]:
        """Candidate reaction-session positions (indices into ``cal``)."""
        return sorted(self._gap_sources(events, cal))

    # -- scan ------------------------------------------------------------------------

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
        sma_hi = None
        if self.pullback_on and self.pb_smas:
            # the higher SMA; NaN until every SMA exists
            sma_hi = np.vstack([ind.sma(df["close"], k).to_numpy() for k in self.pb_smas]).max(axis=0)
        sources = self._gap_sources(events, df.index)
        out: list[Candidate] = []
        for g in sorted(sources):
            gap = o[g] / c[g - 1] - 1.0
            atr_pre = atr.iloc[g - 1]
            vbase = avgv.iloc[g - 1]
            if not (pd.notna(gap) and pd.notna(atr_pre) and pd.notna(vbase)):
                continue
            vol_ratio = v[g] / vbase
            if gap < self.min_gap or vol_ratio < self.min_vol_mult:
                continue
            day_rng = h[g] - lo[g]
            close_pos = (c[g] - lo[g]) / day_rng if day_rng > 0 else 1.0
            if close_pos < self.close_pos_min:
                continue
            gi = {"g": g, "gap": gap, "vol_ratio": vol_ratio, "atr_pre": atr_pre,
                  "close_pos": close_pos, "sources": sources[g]}
            t_cons = None
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
                    t_cons = t
                    out.extend(self._emit(ticker, df, gi, t, rng, atr, ctx))
                    break
            if sma_hi is not None:
                t_pb, touch = self._pullback_trigger(g, n, h, lo, c, sma_hi)
                if t_pb is not None and t_pb != t_cons:
                    out.extend(self._emit_pullback(ticker, df, gi, touch, t_pb, sma_hi, atr, ctx))
        return out

    def _pullback_trigger(self, g, n, h, lo, c, sma_hi) -> tuple[Optional[int], Optional[int]]:
        """(trigger session, first touch session) of the first SMA pullback after gap g,
        or (None, None). Closes must hold the gap-day low the whole way; the reclaim is a
        later session closing above the SMA and above the prior session's high."""
        touch = None
        last = min(g + self.pb_window, n - 1)
        for k in range(g + 1, last + 1):
            if c[k] < lo[g]:
                return None, None
            s = sma_hi[k]
            if not np.isfinite(s):
                continue
            if touch is None:
                if lo[k] <= s * (1.0 + self.pb_touch):
                    touch = k
                continue
            if c[k] > s and c[k] > h[k - 1]:
                return k, touch
        return None, None

    # -- candidate construction -----------------------------------------------------

    @staticmethod
    def _source_at(gi: dict, ts: pd.Timestamp) -> Optional[_Source]:
        for s in gi["sources"]:
            if s.valid_at(ts):
                return s
        return None

    def _stop(self, gap_low: float, base_low: float, atr_t: float) -> tuple[float, float, bool]:
        """(stop, anchor, anchored_on_base): the tighter (higher) of the two lows."""
        on_base = base_low > gap_low
        anchor = base_low if on_base else gap_low
        return anchor - self.stop_atr * atr_t, anchor, on_base

    def _emit(self, ticker, df, gi, t, rng, atr, ctx) -> list[Candidate]:
        ts = df.index[t]
        if ts not in ctx.eval_dates:
            return []
        src = self._source_at(gi, ts)
        if src is None:
            return []
        g, gap, vol_ratio, atr_pre = gi["g"], gi["gap"], gi["vol_ratio"], gi["atr_pre"]
        cons = slice(g + 1, t)
        entry = float(df["high"].iloc[t])
        atr_t = float(atr.iloc[t])
        stop, anchor, on_base = self._stop(
            float(df["low"].iloc[g]), float(df["low"].iloc[cons].min()), atr_t
        )
        if not self.stop_ok(entry, stop):
            return []
        tight = ind.clip01(1.0 - rng / (self.range_mult * atr_pre))
        base_score = 100.0 * (
            0.4 * ind.clip01((gap - self.min_gap) / 0.10)
            + 0.3 * ind.clip01((vol_ratio - self.min_vol_mult) / 3.0)
            + 0.3 * tight
        )
        surprise = self._surprise(ctx.data, ticker, df.index[g], ts)
        score = min(100.0, max(0.0, base_score + surprise["surprise_points"]))
        sessions = t - g - 1
        details = {
            "gap_date": df.index[g].date().isoformat(),
            "gap_pct": f(gap),
            "volume_ratio": f(vol_ratio, 2),
            "consolidation_sessions": int(sessions),
            "consolidation_range": f(rng),
            "pre_gap_atr14": f(atr_pre),
            "gap_day_low": f(df["low"].iloc[g]),
            "atr14": f(atr_t),
            **self._event_details(src.ev),
            **self._gap_details(df, gi),
            "consolidation_start": df.index[g + 1].date().isoformat(),
            "consolidation_end": df.index[t - 1].date().isoformat(),
            "consolidation_high": f(df["high"].iloc[cons].max()),
            "consolidation_low": f(df["low"].iloc[cons].min()),
            "min_close_in_consolidation": f(df["close"].iloc[cons].min()),
            "trigger_date": ts.date().isoformat(),
            "trigger_close": f(df["close"].iloc[t]),
            "trigger_type": TRIGGER_CONSOLIDATION,
            "stop_anchor": f(anchor),
            "stop_anchor_source": "consolidation_low" if on_base else "gap_day_low",
            "base_score": f(base_score, 2),
            **surprise,
            **self._param_details(),
        }
        return [
            Candidate(
                ticker=ticker,
                module=self.name,
                signal_date=ts.date(),
                entry=round(entry, 4),
                stop=round(stop, 4),
                setup_score=round(score, 2),
                rationale=(
                    f"Earnings gap of {gap:.1%} on {vol_ratio:.1f}x average volume on "
                    f"{df.index[g].date()}, held above the gap-day low in a tight "
                    f"{sessions}-session range, then closed above the range high."
                ),
                details=details,
            )
        ]

    def _emit_pullback(self, ticker, df, gi, touch, t, sma_hi, atr, ctx) -> list[Candidate]:
        ts = df.index[t]
        if ts not in ctx.eval_dates:
            return []
        src = self._source_at(gi, ts)
        if src is None:
            return []
        g, gap, vol_ratio, atr_pre = gi["g"], gi["gap"], gi["vol_ratio"], gi["atr_pre"]
        pb_low = float(df["low"].iloc[touch:t + 1].min())
        entry = float(df["high"].iloc[t])
        atr_t = float(atr.iloc[t])
        stop, anchor, on_base = self._stop(float(df["low"].iloc[g]), pb_low, atr_t)
        if not self.stop_ok(entry, stop):
            return []
        prior_close, gap_open = float(df["close"].iloc[g - 1]), float(df["open"].iloc[g])
        # share of the opening gap still unfilled at the pullback low
        held = ind.clip01((pb_low - prior_close) / (gap_open - prior_close))
        base_score = 100.0 * (
            0.4 * ind.clip01((gap - self.min_gap) / 0.10)
            + 0.3 * ind.clip01((vol_ratio - self.min_vol_mult) / 3.0)
            + 0.3 * held
        )
        surprise = self._surprise(ctx.data, ticker, df.index[g], ts)
        score = min(100.0, max(0.0, base_score + surprise["surprise_points"]))
        details = {
            "gap_date": df.index[g].date().isoformat(),
            "gap_pct": f(gap),
            "volume_ratio": f(vol_ratio, 2),
            "pre_gap_atr14": f(atr_pre),
            "gap_day_low": f(df["low"].iloc[g]),
            "atr14": f(atr_t),
            **self._event_details(src.ev),
            **self._gap_details(df, gi),
            # no consolidation on this trigger (keys kept so the chart sees annotations)
            "consolidation_start": None,
            "consolidation_end": None,
            "pullback_start": df.index[touch].date().isoformat(),
            "pullback_sessions_after_gap": int(touch - g),
            "pullback_low": f(pb_low),
            "pullback_sma": f(sma_hi[touch]),
            "pullback_sma_days": list(self.pb_smas),
            "gap_held_fraction": f(held),
            "min_close_after_gap": f(df["close"].iloc[g + 1:t + 1].min()),
            "trigger_date": ts.date().isoformat(),
            "trigger_close": f(df["close"].iloc[t]),
            "trigger_sma": f(sma_hi[t]),
            "prior_day_high": f(df["high"].iloc[t - 1]),
            "trigger_type": TRIGGER_PULLBACK,
            "stop_anchor": f(anchor),
            "stop_anchor_source": "pullback_low" if on_base else "gap_day_low",
            "base_score": f(base_score, 2),
            **surprise,
            **self._param_details(),
            "pullback_window_sessions": self.pb_window,
            "pullback_touch_pct": f(self.pb_touch),
        }
        sma_txt = "/".join(str(k) for k in self.pb_smas)
        return [
            Candidate(
                ticker=ticker,
                module=self.name,
                signal_date=ts.date(),
                entry=round(entry, 4),
                stop=round(stop, 4),
                setup_score=round(score, 2),
                rationale=(
                    f"Earnings gap of {gap:.1%} on {vol_ratio:.1f}x average volume on "
                    f"{df.index[g].date()}; the first pullback to the {sma_txt}-day SMA "
                    f"({df.index[touch].date()}) held the gap-day low, then price reclaimed "
                    f"the SMA and closed above the prior day's high."
                ),
                details=details,
            )
        ]

    # -- details helpers -------------------------------------------------------------

    @staticmethod
    def _event_details(ev_row: Optional[pd.Series]) -> dict[str, Any]:
        if ev_row is None:
            return {"event_date": None, "event_timing": None, "event_source": None}
        return {
            "event_date": ev_row["event_date"] if isinstance(ev_row["event_date"], str) else None,
            "event_timing": _text(ev_row.get("timing")) or None,
            "event_source": _text(ev_row.get("source")) or None,
        }

    @staticmethod
    def _gap_details(df: pd.DataFrame, gi: dict) -> dict[str, Any]:
        g = gi["g"]
        return {
            "prior_close": f(df["close"].iloc[g - 1]),
            "gap_day_open": f(df["open"].iloc[g]),
            "gap_day_high": f(df["high"].iloc[g]),
            "gap_day_close": f(df["close"].iloc[g]),
            "gap_day_close_position": f(gi["close_pos"], 3),
            "gap_day_volume": f(df["volume"].iloc[g], 0),
            "avg_volume_50": f(df["volume"].iloc[g] / gi["vol_ratio"], 0),
        }

    def _param_details(self) -> dict[str, Any]:
        return {
            "min_gap_pct": f(self.min_gap),
            "min_volume_multiple": f(self.min_vol_mult),
            "tight_range_atr_multiple": f(self.range_mult),
            "hold_days_min": int(self.hold_min),
            "hold_days_max": int(self.hold_max),
            "gap_close_min_range_position": f(self.close_pos_min),
        }

    # -- earnings surprise -----------------------------------------------------------

    def _surprise(self, data, ticker: str, gap_ts: pd.Timestamp, signal_ts: pd.Timestamp) -> dict[str, Any]:
        """Score adjustment from the measured surprise of this event. Never raises and
        never filters: no usable data -> 0 points (neutral)."""
        out: dict[str, Any] = {"surprise_source": None, "surprise_value": None, "surprise_points": 0.0}
        try:
            eps = self._eps_surprise(data, ticker, gap_ts)
            if eps is not None:
                s = ind.clip01(0.5 + eps / (2.0 * self.eps_sat))
                out.update(surprise_source="nasdaq_eps", surprise_value=f(eps))
            else:
                sue = self._revenue_sue(data, ticker, gap_ts, signal_ts)
                if sue is None:
                    return out
                s = ind.clip01(0.5 + sue / (2.0 * self.sue_sat))
                out.update(surprise_source="xbrl_revenue_sue", surprise_value=f(sue, 3))
        except Exception:  # noqa: BLE001 -- surprise data is optional by design
            log.debug("surprise lookup failed for %s %s", ticker, gap_ts.date(), exc_info=True)
            return {"surprise_source": None, "surprise_value": None, "surprise_points": 0.0}
        out["surprise_points"] = round(self.surprise_pts * (2.0 * s - 1.0), 2)
        return out

    def _eps_surprise(self, data, ticker: str, gap_ts: pd.Timestamp) -> Optional[float]:
        """(actual - forecast) / |forecast| (else Nasdaq's own surprise %) from the
        calendar row of this report: report date within ``prefer_8k_window_days`` + 1
        calendar days before (or on) the gap session. Reported EPS is public from the
        release, which precedes the gap session's open."""
        start = (gap_ts - pd.Timedelta(days=self.prefer_8k_days + 1)).date()
        rows = data.get_earnings_events(ticker, start, gap_ts.date())
        if rows.empty or "eps_actual" not in rows:
            return None
        for _, r in rows.sort_values("event_date", ascending=False).iterrows():
            if _is_8k(r):
                continue
            actual, fc = _num(r.get("eps_actual")), _num(r.get("eps_forecast"))
            if actual is not None and fc is not None and abs(fc) >= 0.01:
                return (actual - fc) / abs(fc)
            pct = _num(r.get("eps_surprise_pct"))
            if pct is not None:
                return pct / 100.0
        return None

    @staticmethod
    def _revenue_sue(data, ticker: str, gap_ts: pd.Timestamp, signal_ts: pd.Timestamp) -> Optional[float]:
        """Seasonal-random-walk-with-drift SUE of quarterly revenue, point-in-time
        (``get_fundamentals`` as of the signal day). The latest quarter must have been
        filed around this event (from 7 days before the gap on) and end within 120 days
        before it, so it is *this* report's number; otherwise None."""
        fund = data.get_fundamentals(ticker, signal_ts.date())
        if fund is None or fund.empty:
            return None
        q = fund[fund["metric"] == "revenue"].copy()
        if q.empty:
            return None
        q["ps"] = pd.to_datetime(q["period_start"], errors="coerce")
        q["pe"] = pd.to_datetime(q["period_end"], errors="coerce")
        q["fd"] = pd.to_datetime(q["filed_date"], errors="coerce")
        q = q[(q["pe"] - q["ps"]).dt.days.between(80, 100)].dropna(subset=["pe", "fd", "value"])
        if q.empty:
            return None
        q = q.sort_values("pe").drop_duplicates("pe", keep="last")
        latest = q.iloc[-1]
        if not (gap_ts - pd.Timedelta(days=7) <= latest["fd"] <= signal_ts):
            return None
        if (gap_ts - latest["pe"]).days > 120:
            return None
        values = dict(zip(q["pe"], q["value"].astype(float)))

        def year_ago(pe: pd.Timestamp) -> Optional[float]:
            for k, val in values.items():
                if abs((pe - k).days - 365) <= 20:
                    return val
            return None

        if year_ago(latest["pe"]) is None:
            return None
        changes: list[float] = []
        for pe in sorted(values, reverse=True):
            prev = year_ago(pe)
            if prev is not None:
                changes.append(values[pe] - prev)
        current, history = changes[0], changes[1:1 + SUE_MAX_HISTORY]
        if len(history) < SUE_MIN_HISTORY:
            return None
        sd = float(np.std(history, ddof=1))
        if not np.isfinite(sd) or sd <= 0:
            return None
        return (current - float(np.mean(history))) / sd
