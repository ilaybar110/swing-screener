"""Plotly chart that explains a recommendation (no Streamlit imports: unit-testable).

Base layer (every recommendation): candlesticks + volume, entry/stop/target lines, signal /
valid-until / earnings vertical lines, outcome markers, and a visible split between what
the module saw (up to the signal) and what happened afterwards.  On top of that each
module draws the exact levels, dates and values stored in its ``details``.

Colours: support green, resistance red, entry blue, stop red dashed, target green dashed.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src import indicators as ind

ENTRY, STOP, TARGET = "#4C9AFF", "#EF5350", "#2FBF71"
SUPPORT, RESIST = "#26A69A", "#D62828"
AMBER, VIOLET, GOLD, GRAY, PINK = "#F2B134", "#8B7CF6", "#FFD60A", "#8A94A8", "#E56BC2"
UP, DOWN = "#26A69A", "#EF5350"

MODULE_LABEL = {
    "momentum_pullback": "A Momentum pullback",
    "earnings_gap_drift": "B Earnings gap",
    "insider_cluster": "C Insider cluster",
    "base_breakout": "D Base breakout",
}
TIMING = {
    "before_open": "before the open (BMO)", "bmo": "before the open (BMO)",
    "after_close": "after the close (AMC)", "amc": "after the close (AMC)",
    "during": "during market hours",
}
EXIT_TEXT = {
    "stop": "stopped out at the stop price",
    "breakeven": "remaining half stopped at breakeven after the 2R partial",
    "sma20_exit": "remaining half sold at the next open after a close below the 20-day SMA",
    "time_stop": "time stop: sold at the close of the 20th session after entry",
}


def _ts(s: Any) -> pd.Timestamp:
    return pd.Timestamp(s).normalize()


def _has(v: Any) -> bool:
    return v is not None and not (isinstance(v, float) and np.isnan(v)) and v != ""


def _num(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    return float(v) if isinstance(v, (int, float)) else None


def _fmt_vol(v: float) -> str:
    return f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.0f}K"


class _Chart:
    def __init__(self, rec: dict, prices: pd.DataFrame, spy: pd.DataFrame, enabled: list[str],
                 annots: dict[str, dict], today: pd.Timestamp):
        self.rec, self.full, self.spy = rec, prices, spy
        self.signal = _ts(rec["signal_date"])
        self.start = self.signal - pd.Timedelta(days=183)
        self.px = prices[prices.index >= self.start]
        extra = [_ts(rec[k]) for k in ("valid_until", "earnings_date") if _has(rec.get(k))]
        right = max([self.px.index[-1], *extra, today])
        self.x_end = right + pd.Timedelta(days=max(12, (right - self.start).days // 9))
        self.enabled = [m for m in enabled if m in annots]
        self.annots = annots
        self.row_rs = self.row_atr = None
        n = 2
        if "momentum_pullback" in self.enabled:
            n += 1
            self.row_rs = n
        if "base_breakout" in self.enabled:
            n += 1
            self.row_atr = n
        heights = [0.58, 0.14] + [0.14] * (n - 2)
        heights = [h / sum(heights) for h in heights]
        self.heights = heights
        self.fig = make_subplots(rows=n, cols=1, shared_xaxes=True, vertical_spacing=0.025, row_heights=heights)
        self.height = 640 + 150 * (n - 2)

    # -- primitives ---------------------------------------------------------------

    def hline(self, y, x0, x1, name, color, label, hover, dash="solid", row=1, group=None, width=1.4,
              legend=True, label_at="start", below=False):
        xs = pd.date_range(_ts(x0), _ts(x1), periods=28)
        self.fig.add_trace(go.Scatter(
            x=xs, y=[y] * len(xs), mode="lines", name=name, legendgroup=group or name, showlegend=legend,
            line=dict(color=color, width=width, dash=dash), hovertemplate=f"{hover}<extra>{name}</extra>"),
            row=row, col=1)
        if label:
            self.fig.add_annotation(
                x=_ts(x1) if label_at == "end" else _ts(x0), y=y, text=label, showarrow=False,
                xanchor="left", yanchor="top" if below else "bottom", font=dict(size=10, color=color),
                row=row, col=1)

    def vline(self, x, name, color, hover, dash="dot", label=None, y_label=1.075):
        x = _ts(x)
        self.fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, xref="x", yref="paper",
                           line=dict(color=color, width=1.3, dash=dash))
        lo, hi = float(self.px["low"].min()), float(self.px["high"].max())
        ys = np.linspace(lo, hi, 14)
        self.fig.add_trace(go.Scatter(
            x=[x] * len(ys), y=ys, mode="markers", marker=dict(size=9, opacity=0, color=color), name=name,
            legendgroup=name, showlegend=False, hovertemplate=f"{hover}<extra>{name}</extra>"), row=1, col=1)
        self.fig.add_annotation(x=x, y=y_label, yref="paper", text=label or name, showarrow=False,
                                font=dict(size=10, color=color), xanchor="left")

    def box(self, x0, x1, y0, y1, color, fill, label, hover, name, row=1, group=None):
        x0, x1 = _ts(x0) - pd.Timedelta(hours=12), _ts(x1) + pd.Timedelta(hours=12)
        self.fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=fill,
                           line=dict(color=color, width=1.2, dash="dash"), layer="below", row=row, col=1)
        xs = pd.date_range(x0, x1, periods=8)
        for ys in ((y0, y0), (y1, y1), ((y0 + y1) / 2,) * 2):  # hover anchors inside the box
            self.fig.add_trace(go.Scatter(
                x=[xs[0], xs[-1]], y=list(ys), mode="markers", marker=dict(opacity=0, size=14), name=name,
                legendgroup=group or name, showlegend=False, hovertemplate=f"{hover}<extra>{name}</extra>"),
                row=row, col=1)
        self.fig.add_annotation(x=x0, y=y1, text=label, showarrow=False, xanchor="left", yanchor="bottom",
                                font=dict(size=10, color=color), row=row, col=1)

    def marker(self, x, y, symbol, color, name, hover, size=13, row=1, group=None, text=None, legend=True):
        self.fig.add_trace(go.Scatter(
            x=[_ts(x)], y=[y], mode="markers+text" if text else "markers", text=[text] if text else None,
            textposition="top center", textfont=dict(size=10, color=color),
            marker=dict(symbol=symbol, size=size, color=color, line=dict(color="white", width=1)),
            name=name, legendgroup=group or name, showlegend=legend,
            hovertemplate=f"{hover}<extra>{name}</extra>"), row=row, col=1)

    def line(self, series: pd.Series, name, color, hover, row=1, group=None, width=1.3, dash="solid"):
        s = series[series.index >= self.start].dropna()
        self.fig.add_trace(go.Scatter(
            x=s.index, y=s.values, mode="lines", name=name, legendgroup=group or name,
            line=dict(color=color, width=width, dash=dash),
            hovertemplate=f"%{{x|%b %d}}: %{{y:.2f}}<br>{hover}<extra>{name}</extra>"), row=row, col=1)

    # -- base layer -----------------------------------------------------------------

    def base(self, outcome: Optional[dict]):
        rec, px, sig = self.rec, self.px, self.signal
        pre, post = px[px.index <= sig], px[px.index > sig]
        for row in range(1, len(self.heights) + 1):
            self.fig.add_vrect(x0=sig + pd.Timedelta(hours=12), x1=self.x_end, fillcolor="rgba(138,148,168,0.12)",
                               line_width=0, layer="below", row=row, col=1, exclude_empty_subplots=False)
        self.fig.add_annotation(x=sig, y=1.0, yref="paper", xanchor="right", yanchor="bottom", showarrow=False,
                                text="what the module saw ", font=dict(size=10, color=GRAY))
        self.fig.add_annotation(x=sig, y=1.0, yref="paper", xanchor="left", yanchor="bottom", showarrow=False,
                                text=" after the signal", font=dict(size=10, color=GRAY))
        for part, name, alpha in ((pre, "Price (up to signal)", 1.0), (post, "Price (after signal)", 0.45)):
            if part.empty:
                continue
            self.fig.add_trace(go.Candlestick(
                x=part.index, open=part["open"], high=part["high"], low=part["low"], close=part["close"],
                name=name, legendgroup="price",
                increasing=dict(line=dict(color=UP), fillcolor=f"rgba(38,166,154,{alpha})"),
                decreasing=dict(line=dict(color=DOWN), fillcolor=f"rgba(239,83,80,{alpha})")), row=1, col=1)
        for part, a in ((pre, 0.85), (post, 0.4)):
            if part.empty:
                continue
            colors = np.where(part["close"] >= part["open"], f"rgba(38,166,154,{a})", f"rgba(239,83,80,{a})")
            self.fig.add_trace(go.Bar(
                x=part.index, y=part["volume"], marker_color=colors, name="Volume", legendgroup="volume",
                showlegend=part is pre, hovertemplate="%{x|%b %d}: %{y:,.0f} shares<extra>Volume</extra>"),
                row=2, col=1)
        self.line(ind.avg_volume(self.full, 20), "20-day avg volume", GRAY, "20-day average daily volume",
                  row=2, width=1.0, dash="dot")

        entry, stop, target = rec["entry"], rec["stop"], rec["target"]
        risk = entry - stop
        pstop, ptgt = stop / entry - 1, target / entry - 1
        r_t = (target - entry) / risk
        x0 = sig
        self.hline(entry, x0, self.x_end, "Entry", ENTRY, f"Entry {entry:.2f} (0R)",
                   f"Buy-stop at {entry:.2f}: just above the trigger day's high. Risk per share = {risk:.2f}.",
                   label_at="end")
        self.hline(stop, x0, self.x_end, "Stop", STOP, f"Stop {stop:.2f} ({pstop:+.1%}, -1R)",
                   f"Stop {stop:.2f} ({pstop:+.1%} from entry = -1R). A stop-out loses about 1R.",
                   dash="dash", label_at="end")
        self.hline(target, x0, self.x_end, "Target", TARGET, f"Target {target:.2f} ({ptgt:+.1%}, +{r_t:.0f}R)",
                   f"Target {target:.2f} ({ptgt:+.1%} = +{r_t:.0f}R). Half the position is sold here and the "
                   "stop on the rest moves to breakeven.", dash="dash", label_at="end")
        self.vline(sig, "Signal date", GRAY,
                   f"Signal {sig.date()}: the trigger day. Everything the module used is on or before this day.",
                   dash="solid", label="Signal", y_label=1.075)
        if _has(rec.get("valid_until")):
            self.vline(rec["valid_until"], "Valid until", AMBER,
                       "Last day the buy-stop order is valid (5 trading days after the signal). "
                       "If it has not filled by then the idea expires.", label="Valid until", y_label=1.11)
        if _has(rec.get("earnings_date")):
            self.vline(rec["earnings_date"], "Next earnings", PINK,
                       "Upcoming earnings report: gap risk for anyone holding through it.",
                       label="Earnings", y_label=1.145)
        if outcome:
            self.outcome(outcome)

    def outcome(self, oc: dict):
        res = oc.get("result")
        if "expired" in oc:
            d, why = oc["expired"]
            self.marker(d, self.rec["entry"], "x", GRAY, "Expired",
                        f"Expired {d.date()}: {why}. No trade was taken.", size=14)
            return
        if "entry" in oc:
            d, px = oc["entry"]
            self.marker(d, px, "triangle-up", ENTRY, "Entry fill",
                        f"Entry filled {d.date()} at {px:.2f} (the buy-stop triggered; gap-ups fill at the open).",
                        size=14)
        if "partial" in oc:
            d, px = oc["partial"]
            self.marker(d, px, "diamond", TARGET, "2R partial exit",
                        f"{d.date()}: price reached the {px:.2f} target (+2R). Half sold; "
                        "stop on the other half moves to the entry fill.", size=13)
        if "exit" in oc:
            d, px, reason = oc["exit"]
            good = res is not None and (res.r_multiple or 0) > 0
            self.marker(d, px, "x", TARGET if good else STOP, f"Final exit ({reason})",
                        f"Final exit {d.date()} at about {px:.2f}: {EXIT_TEXT.get(reason, reason)}. "
                        f"Trade result {res.r_multiple:+.2f}R (net of costs).", size=14)

    # -- module layers --------------------------------------------------------------

    def module_a(self, d: dict, g: str):
        full, px = self.full, self.px
        self.line(ind.sma(full["close"], 20), "A SMA20", AMBER, "20-day SMA: the short-term trend the pullback came back to", group=g)
        self.line(ind.sma(full["close"], 50), "A SMA50", VIOLET, "50-day SMA: the trend filter; it must be rising and the close above it", group=g)
        hi = _num(d, "high_52w")
        if hi:
            self.hline(hi, self.start, self.x_end, "A 52-week high", RESIST, f"52w high {hi:.2f}",
                       f"52-week high {hi:.2f}: resistance overhead. The leader must trade within 15% of it.",
                       dash="dot", group=g, label_at="end")
        sh, shd = _num(d, "swing_high"), d.get("swing_high_date")
        if sh and shd:
            self.marker(shd, sh, "triangle-down", RESIST, "A Swing high",
                        f"Swing high {sh:.2f} on {shd}: highest high of the 20 sessions before the trigger. "
                        "The pullback starts the next day.", group=g)
        pl, pld = _num(d, "pullback_low"), d.get("pullback_low_date")
        if pl and pld:
            self.marker(pld, pl, "triangle-up", SUPPORT, "A Pullback low",
                        f"Pullback low {pl:.2f} on {pld}: lowest low of the pullback. The stop sits just below it "
                        "(minus 0.1 ATR).", group=g)
        ps, pe = d.get("pullback_start"), d.get("pullback_end")
        if ps and pe:
            vr, depth = _num(d, "pullback_volume_ratio"), _num(d, "pullback_depth_atr")
            for row in (1, 2):
                self.fig.add_vrect(x0=_ts(ps) - pd.Timedelta(hours=12), x1=_ts(pe) + pd.Timedelta(hours=12),
                                   fillcolor="rgba(242,177,52,0.13)", line_width=0, layer="below", row=row, col=1, exclude_empty_subplots=False)
            self.fig.add_annotation(
                x=_ts(ps), y=pl or sh or hi, yshift=-14, text=f"Pullback: {d.get('pullback_sessions')} sessions, {depth:.1f} ATR deep"
                if depth else "Pullback", showarrow=False, xanchor="left", yanchor="top",
                font=dict(size=10, color=AMBER), row=1, col=1)
            av20, pav = _num(d, "avg_volume_20"), _num(d, "pullback_avg_volume")
            if av20 and pav:
                end = d.get("trigger_date") or pe
                self.hline(av20, ps, end, "A 20d avg vol (pre-trigger)", GRAY,
                           f"20d avg {_fmt_vol(av20)}", f"20-day average volume ending the day before the trigger: {av20:,.0f}",
                           dash="dash", row=2, group=g, width=1.2, label_at="end")
                self.hline(pav, ps, pe, "A Pullback avg vol", AMBER,
                           f"pullback avg {_fmt_vol(pav)} = {vr:.2f}x" if vr else None,
                           f"Average volume during the pullback: {pav:,.0f} = {pav / av20:.2f}x the 20-day average. "
                           "Below 1.0x means sellers were drying up (volume dry-up).",
                           row=2, group=g, width=2.2, below=True)
        td, tc, pdh = d.get("trigger_date"), _num(d, "trigger_close"), _num(d, "prior_day_high")
        if td and tc:
            self.marker(td, tc, "star", GOLD, "A Trigger day",
                        f"Trigger {td}: closed {tc:.2f}, above the prior day's high {pdh:.2f}. "
                        f"The buy-stop is placed at this day's high {self.rec['entry']:.2f}.", size=16, group=g)
        # relative strength subpanel
        row = self.row_rs
        if row:
            rs = (full["close"] / self.spy["close"].reindex(full.index)).dropna()
            rs = rs[rs.index >= self.start]
            rs = 100 * rs / rs.iloc[0]
            self.line(rs, "A RS line vs SPY", VIOLET, "Stock price / SPY price, rebased to 100 at the left edge. "
                      "Rising = outperforming the market.", row=row, group=g, width=1.6)
            lb, sk = int(d.get("rs_lookback", 126)), int(d.get("rs_skip", 21))
            pos = full.index.get_indexer([self.signal])[0]
            if pos >= lb:
                a, b = full.index[pos - lb], full.index[pos - sk]
                self.fig.add_vrect(x0=a, x1=b, fillcolor="rgba(139,124,246,0.10)", line_width=0, layer="below",
                                   row=row, col=1)
                self.fig.add_annotation(x=a, y=float(rs.max()), xanchor="left", yanchor="top", showarrow=False,
                                        text=f"RS window: {lb} sessions back to {sk} sessions ago",
                                        font=dict(size=10, color=VIOLET), row=row, col=1)
            pct, raw = _num(d, "rs_pct"), _num(d, "rs_raw")
            if pct is not None and self.signal in rs.index:
                txt = f"RS percentile {pct:.0f}" + (f" ({raw:+.0%} vs SPY over the window)" if raw is not None else "")
                self.marker(self.signal, float(rs.loc[self.signal]), "circle", VIOLET, "A RS percentile",
                            f"{txt}. Needs to be in the top 20% (>= 80) of the universe.", row=row, group=g,
                            text=txt, legend=False)

    def module_b(self, d: dict, g: str):
        px = self.px
        gd = d.get("gap_date")
        gap, op, pc = _num(d, "gap_pct"), _num(d, "gap_day_open"), _num(d, "prior_close")
        if gd:
            gts = _ts(gd)
            timing = TIMING.get(str(d.get("event_timing") or "").lower())
            src = str(d.get("event_source") or "")
            how = timing or ("8-K filing (reaction session derived from acceptance time)" if src.lower().startswith(("8k", "8-k")) else "timing unknown")
            ev = d.get("event_date") or gd
            tag = {"before the open (BMO)": "BMO", "after the close (AMC)": "AMC"}.get(timing, "")
            hi = _num(d, "gap_day_high") or float(px["high"].max())
            self.marker(gts, hi * 1.01, "diamond", PINK, "B Earnings event",
                        f"Earnings reported {ev}, {how}. {gd} was the first session that reacted.",
                        size=13, group=g, text=f"Earnings{' ' + tag if tag else ''}")
            before = self.full.index[self.full.index < gts]
            if gap is not None and op and pc and len(before):
                x0 = before[-1]
                self.fig.add_shape(type="rect", x0=x0, x1=gts, y0=pc, y1=op, fillcolor="rgba(242,177,52,0.35)",
                                   line=dict(color=AMBER, width=1), row=1, col=1)
                self.fig.add_trace(go.Scatter(
                    x=[x0 + (gts - x0) / 2], y=[(pc + op) / 2], mode="markers+text", text=[f"gap {gap:+.1%}"],
                    textposition="middle left", textfont=dict(size=10, color=AMBER), marker=dict(opacity=0, size=18),
                    name="B Gap", legendgroup=g, showlegend=True,
                    hovertemplate=f"Opening gap {gap:+.1%}: opened {op:.2f} vs prior close {pc:.2f}<extra>Gap</extra>"),
                    row=1, col=1)
            gl = _num(d, "gap_day_low")
            if gl:
                self.hline(gl, gts, self.x_end, "B Gap-day low (support)", SUPPORT, f"Gap-day low {gl:.2f}",
                           f"Gap-day low {gl:.2f}: support. Closes must stay above it and the stop sits just below it.",
                           group=g, width=1.8)
            vr, gv, av = _num(d, "volume_ratio"), _num(d, "gap_day_volume"), _num(d, "avg_volume_50")
            if gv and av and gd in [str(i.date()) for i in px.index]:
                self.fig.add_trace(go.Bar(
                    x=[gts], y=[gv], marker_color=GOLD, name="B Gap-day volume", legendgroup=g,
                    hovertemplate=f"Gap-day volume {gv:,.0f} = {vr:.2f}x the 50-day average ({av:,.0f}); "
                                  f"needs >= {d.get('min_volume_multiple', 1.5)}x<extra>Gap-day volume</extra>"),
                    row=2, col=1)
                self.hline(av, gts - pd.Timedelta(days=25), _ts(d.get("trigger_date") or gd),
                           "B 50d avg vol (pre-gap)", GRAY, f"50d avg {_fmt_vol(av)}",
                           f"50-day average volume before the gap: {av:,.0f}", dash="dash", row=2, group=g, width=1.2)
                self.fig.add_annotation(x=gts, y=gv, text=f"{vr:.1f}x avg", showarrow=False, yanchor="bottom",
                                        font=dict(size=10, color=GOLD), row=2, col=1)
        cs, ce = d.get("consolidation_start"), d.get("consolidation_end")
        ch, cl = _num(d, "consolidation_high"), _num(d, "consolidation_low")
        if cs and ce and ch and cl:
            n = d.get("consolidation_sessions")
            self.box(cs, ce, cl, ch, GRAY, "rgba(138,148,168,0.12)", f"Consolidation {n}d ({cl:.2f}-{ch:.2f})",
                     f"Consolidation {cs} to {ce} ({n} sessions): range {cl:.2f} to {ch:.2f}, "
                     f"{(ch - cl) / _num(d, 'pre_gap_atr14'):.2f}x the pre-gap ATR (must be tight).",
                     "B Consolidation", group=g)
        td, tc = d.get("trigger_date"), _num(d, "trigger_close")
        if td and tc:
            self.marker(td, tc, "star", GOLD, "B Breakout trigger",
                        f"Trigger {td}: closed {tc:.2f}, above the consolidation high {ch:.2f}. "
                        f"Buy-stop at this day's high {self.rec['entry']:.2f}.", size=16, group=g)

    def module_c(self, d: dict, g: str):
        n = _num(d, "sma_days") or 20
        self.line(ind.sma(self.full["close"], int(n)), f"C SMA{int(n)}", AMBER,
                  f"{int(n)}-day SMA: price must close above it after the cluster to confirm", group=g)
        trades = d.get("trades") or []
        for i, t in enumerate(trades):
            val = t.get("value") or 0
            who = t.get("insider", "?") + (f", {t['title']}" if t.get("title") else "")
            y, note = t["price"], ""
            day = self.full.loc[self.full.index == _ts(t["trade_date"])]
            if len(day) and not (day["low"].iloc[0] * 0.9 <= y <= day["high"].iloc[0] * 1.1):
                y = float(day["close"].iloc[0])  # reported price is off-market: draw at the close, say so
                note = f"<br>(reported price {t['price']:.2f} is outside that day's range; drawn at the close)"
            self.marker(t["trade_date"], y, "diamond", GOLD, "C Insider purchase",
                        f"<b>{who}</b><br>Bought {t['shares']:,.0f} sh @ {t['price']:.2f} = ${val:,.0f}<br>"
                        f"Trade date {t['trade_date']}, Form 4 filed {t['filed_date']}{note}",
                        size=float(np.clip(9 + val / 60_000, 10, 24)), group=g, legend=i == 0)
        tot = _num(d, "cluster_total_value")
        if trades and tot:
            first = min(t["trade_date"] for t in trades)
            top = max(self.full.loc[self.full.index == _ts(t["trade_date"]), "high"].max() for t in trades)
            self.fig.add_annotation(x=_ts(first), y=top, yshift=26, showarrow=False, xanchor="left",
                                    text=f"Cluster: {d.get('insider_count')} insiders, ${tot:,.0f}",
                                    font=dict(size=11, color=GOLD), row=1, col=1)
        cd = d.get("completion_date")
        if cd:
            self.vline(cd, "C Cluster complete", GOLD,
                       "Cluster complete: the Form 4 that made the buying qualify (2+ insiders, enough dollars) was filed.",
                       label="Cluster complete", y_label=1.18)
        sl, sld = _num(d, "swing_low"), d.get("swing_low_date")
        if sl and sld:
            basis = d.get("stop_basis")
            self.hline(sl, d.get("swing_low_window_start") or sld, d.get("trigger_date") or sld, "C 10-session swing low",
                       SUPPORT, None, f"Lowest low of the last 10 sessions: {sl:.2f}", dash="dot", group=g)
            self.marker(sld, sl, "triangle-up", SUPPORT, "C Swing low (stop)",
                        f"Swing low {sl:.2f} on {sld}. Stop = the higher of this and entry - 2 ATR "
                        f"({_num(d, 'atr_stop'):.2f}); used here: "
                        f"{'the swing low' if basis == 'swing_low' else 'the 2-ATR cap'}.", group=g)
        td, tc = d.get("trigger_date"), _num(d, "trigger_close")
        if td and tc:
            self.marker(td, tc, "star", GOLD, "C Trigger day",
                        f"Trigger {td}: first close above the {int(n)}-day SMA ({_num(d, 'sma_at_trigger'):.2f}) after "
                        f"the cluster. Close {tc:.2f}; buy-stop at the day's high {self.rec['entry']:.2f}.",
                        size=16, group=g)

    def module_d(self, d: dict, g: str):
        bs, be = d.get("base_start"), d.get("base_end")
        bh, bl, bm = _num(d, "base_high"), _num(d, "base_low"), _num(d, "base_mid")
        td, tc = d.get("trigger_date"), _num(d, "trigger_close")
        if bs and be and bh and bl:
            self.box(bs, be, bl, bh, VIOLET, "rgba(139,124,246,0.10)",
                     f"Base {d.get('base_length')} sessions: high {bh:.2f}, mid {bm:.2f}, low {bl:.2f}",
                     f"Base {bs} to {be} ({d.get('base_length')} sessions): high {bh:.2f}, low {bl:.2f}.", "D Base", group=g)
            self.hline(bh, bs, self.x_end, "D Base high (resistance)", RESIST, None,
                       f"Base high {bh:.2f}: resistance the breakout must close above.", group=g, width=1.6)
            if bm:
                self.hline(bm, bs, self.x_end, "D Base midpoint", GRAY, None,
                           f"Base midpoint {bm:.2f}: the stop is the higher of this and entry - 2 ATR "
                           f"({_num(d, 'atr_stop'):.2f}).", dash="dot", group=g)
        if td and tc:
            self.marker(td, tc, "star", GOLD, "D Breakout",
                        f"Breakout {td}: closed {tc:.2f} above the base high {bh:.2f} on "
                        f"{_num(d, 'volume_ratio'):.2f}x average volume.", size=16, group=g)
            bv, av, mv = _num(d, "breakout_volume"), _num(d, "avg_volume_50"), _num(d, "min_volume_multiple") or 1.5
            if bv and av:
                self.fig.add_trace(go.Bar(
                    x=[_ts(td)], y=[bv], marker_color=GOLD, name="D Breakout volume", legendgroup=g,
                    hovertemplate=f"Breakout-day volume {bv:,.0f} = {bv / av:.2f}x the 50-day average "
                                  f"({av:,.0f}); needs >= {mv}x<extra>Breakout volume</extra>"), row=2, col=1)
                self.hline(mv * av, _ts(td) - pd.Timedelta(days=20), _ts(td) + pd.Timedelta(days=10),
                           f"D {mv}x 50d avg volume", GOLD, f"{mv}x avg = {_fmt_vol(mv * av)}",
                           f"Volume threshold: {mv} x 50-day average ({av:,.0f}) = {mv * av:,.0f}",
                           dash="dash", row=2, group=g, width=1.3)
        row = self.row_atr
        if row:
            self.line(ind.atr(self.full, 14), "D ATR14", VIOLET, "14-day average true range: typical daily range", row=row,
                      group=g, width=1.6)
            a0, a1 = _num(d, "atr_start"), _num(d, "atr_end")
            ratio = _num(d, "atr_contraction_ratio")
            thr = _num(d, "contraction_threshold") or 0.7
            if a0 and a1 and d.get("atr_start_date") and be:
                self.marker(d["atr_start_date"], a0, "circle", GRAY, "D ATR at base start",
                            f"ATR14 on {d['atr_start_date']} (session before the base): {a0:.3f}", row=row, group=g,
                            legend=False)
                self.marker(be, a1, "circle", GOLD, "D ATR at base end",
                            f"ATR14 on {be} (last base session): {a1:.3f} = {ratio:.2f}x the start. Needs < {thr:.2f}x.",
                            row=row, group=g, legend=False,
                            text=f"{a0:.2f} -> {a1:.2f} = {ratio:.2f}x (needs < {thr:.2f}x)")
                self.hline(a0 * thr, d["atr_start_date"], be, "D ATR limit", GRAY, None,
                           f"Contraction limit {thr:.2f} x {a0:.3f} = {a0 * thr:.3f}", dash="dot", row=row, group=g,
                           width=1.0, legend=False)

    # -- assemble -------------------------------------------------------------------

    def finish(self):
        f = self.fig
        px = self.px
        have = set(px.index)
        missing = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(px.index[0], px.index[-1]) if d not in have]
        f.update_xaxes(range=[px.index[0] - pd.Timedelta(days=2), self.x_end],
                       rangebreaks=[dict(bounds=["sat", "mon"]), dict(values=missing)], rangeslider_visible=False,
                       showspikes=True, spikemode="across", spikethickness=1)
        lo, hi = float(px["low"].min()), float(px["high"].max())
        lo = min(lo, self.rec["stop"]) if self.rec["stop"] > 0 else lo
        hi = max(hi, self.rec["target"])
        pad = (hi - lo) * 0.04
        f.update_yaxes(range=[lo - pad, hi + pad], title_text="Price", row=1, col=1)
        f.update_yaxes(title_text="Volume", row=2, col=1)
        if self.row_rs:
            f.update_yaxes(title_text="RS vs SPY", row=self.row_rs, col=1)
        if self.row_atr:
            f.update_yaxes(title_text="ATR14", row=self.row_atr, col=1)
        f.update_layout(
            height=self.height, barmode="overlay", hovermode="closest", dragmode="pan",
            margin=dict(l=50, r=170, t=130, b=30), xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", y=-0.03, yanchor="top", font=dict(size=10)),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        f.update_xaxes(showgrid=False)
        f.update_yaxes(gridcolor="rgba(138,148,168,0.15)")
        return f


def build_figure(rec: dict, prices: pd.DataFrame, spy: pd.DataFrame, annots: dict[str, dict],
                 enabled: list[str], outcome: Optional[dict] = None, today: Optional[pd.Timestamp] = None) -> go.Figure:
    """``rec``: entry, stop, target, signal_date, valid_until, earnings_date. ``prices``/``spy``:
    OHLCV indexed by session date (``prices`` must reach back far enough for the indicators)."""
    c = _Chart(rec, prices, spy, enabled, annots, today or pd.Timestamp.today().normalize())
    c.base(outcome)
    fns = {"momentum_pullback": c.module_a, "earnings_gap_drift": c.module_b,
           "insider_cluster": c.module_c, "base_breakout": c.module_d}
    for m in c.enabled:
        fns[m](c.annots[m], m)
    return c.finish()
