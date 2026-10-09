"""Module C -- insider cluster buying (docs/PLAN.md section 7-C).

Interpretation notes (also in docs/status/BOT_4.md):
  * Only open-market purchases (code ``P``) by officers/directors who are not flagged as
    10% owners count; each trade must be >= min_txn_value.
  * Point-in-time by *filing* date: a trade is invisible until its Form 4 is filed.
  * A cluster "completes" on the filing date F where, using trades filed <= F, some
    set of qualifying trades whose trade dates span <= cluster_window_days has >= 2
    distinct insiders and total >= min_cluster_total_value, and that was not already true
    using trades filed < F. Later trades topping up the same cluster do not re-complete it.
  * The first session on/after F is the completion session. The trigger is the first
    session p >= completion with close > SMA20; it must fall within 20 sessions of the
    completion. Entry = trigger day's high.
  * Stop = lowest low of the last 10 sessions, but never more than atr_stop_multiple ATR
    below entry.
"""

from __future__ import annotations

import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.modules.base import PanelModule, ScanContext, f, param

ATR_DAYS = 14
COMPLETION_WINDOW_SESSIONS = 20
STOP_LOOKBACK_SESSIONS = 10
TRADE_HISTORY_DAYS = 120
SENIOR_TITLES = ("chief executive", "ceo", "chief financial", "cfo", "president", "chairman")


def _seniority(row: pd.Series) -> float:
    title = row.get("officer_title")
    title = title.lower() if isinstance(title, str) else ""
    if any(s in title for s in SENIOR_TITLES):
        return 1.0
    if row.get("is_officer"):
        return 0.6
    return 0.4


class InsiderCluster(PanelModule):
    name = "insider_cluster"

    def __init__(self, config) -> None:
        super().__init__(config)
        c = config.modules.c_insider_cluster
        self.min_insiders = c.min_distinct_insiders
        self.min_txn = c.min_txn_value
        self.min_total = c.min_cluster_total_value
        self.window_days = c.cluster_window_days
        self.sma_days = c.sma_confirm_days
        self.atr_mult = c.atr_stop_multiple
        self.completion_sessions = param(c, "completion_window_sessions", COMPLETION_WINDOW_SESSIONS)
        self.stop_lookback = param(c, "stop_lookback_sessions", STOP_LOOKBACK_SESSIONS)
        self._trades: dict[str, pd.DataFrame] = {}

    def prepare(self, tickers: list[str], ctx: ScanContext) -> None:
        lo = (ctx.eval_dates[0] - pd.Timedelta(days=TRADE_HISTORY_DAYS)).date()
        # Nothing filed after ctx.end is visible; per-day filing cut-offs are applied in
        # _completions so scan_history cannot leak later filings into earlier days.
        trades = ctx.data.get_insider_trades(lo, ctx.end, ctx.end)
        self._trades = {}
        if trades.empty:
            return
        trades = trades[trades["trans_code"] == "P"].copy()
        officer = trades["is_officer"].fillna(0).astype(int) == 1
        director = trades["is_director"].fillna(0).astype(int) == 1
        ten = trades["is_ten_pct_owner"].fillna(0).astype(int) == 1
        value = trades["value"].fillna(trades["shares"] * trades["price"])
        trades = trades[(officer | director) & ~ten & (value >= self.min_txn)].copy()
        trades["value"] = value[trades.index]
        trades["trans_date"] = pd.to_datetime(trades["trans_date"])
        trades["filed_date"] = pd.to_datetime(trades["filed_date"])
        trades["insider"] = trades["insider_cik"].fillna(trades["insider_name"])
        for t, g in trades.groupby("ticker"):
            self._trades[t] = g.sort_values(["filed_date", "trans_date"]).reset_index(drop=True)

    def _cluster(self, g: pd.DataFrame) -> dict | None:
        """Best qualifying cluster within ``g`` (trades visible at some filing cut-off),
        or None. Windows end at each trade date and span window_days."""
        best = None
        for end_dt in g["trans_date"].unique():
            w = g[(g["trans_date"] <= end_dt) & (g["trans_date"] >= end_dt - pd.Timedelta(days=self.window_days))]
            n = w["insider"].nunique()
            tot = float(w["value"].sum())
            if n >= self.min_insiders and tot >= self.min_total:
                cand = {"n": int(n), "total": tot, "window": w}
                if best is None or tot > best["total"]:
                    best = cand
        return best

    def _completions(self, ticker: str) -> list[tuple[pd.Timestamp, dict]]:
        tr = self._trades.get(ticker)
        if tr is None or tr.empty:
            return []
        out = []
        for F in sorted(tr["filed_date"].unique()):
            now = self._cluster(tr[tr["filed_date"] <= F])
            if now is None:
                continue
            before = self._cluster(tr[tr["filed_date"] < F])
            if before is None:
                out.append((pd.Timestamp(F), now))
        return out

    def scan_ticker(self, ticker: str, df: pd.DataFrame, ctx: ScanContext) -> list[Candidate]:
        comps = self._completions(ticker)
        if not comps or len(df) < self.sma_days + 2:
            return []
        sma = ind.sma(df["close"], self.sma_days)
        atr = ind.atr(df, ATR_DAYS)
        above = (df["close"] > sma).to_numpy()
        out: list[Candidate] = []
        for F, cl in comps:
            comp = int(df.index.searchsorted(F))
            if comp >= len(df):
                continue
            for p in range(comp, min(comp + self.completion_sessions, len(df))):
                if above[p]:
                    if df.index[p] in ctx.eval_dates:
                        cand = self._emit(ticker, df, p, comp, F, cl, atr)
                        if cand is not None:
                            out.append(cand)
                    break
        return out

    def _emit(self, ticker, df, t, comp, F, cl, atr) -> Candidate | None:
        atr_t = float(atr.iloc[t])
        if pd.isna(atr_t):
            return None
        entry = float(df["high"].iloc[t])
        swing_low = float(df["low"].iloc[max(0, t - self.stop_lookback + 1): t + 1].min())
        stop = max(swing_low, entry - self.atr_mult * atr_t)
        if not self.stop_ok(entry, stop):
            return None
        w = cl["window"]
        insiders = w.drop_duplicates("insider")
        senior = max(_seniority(r) for _, r in w.iterrows())
        score = 100.0 * (
            0.4 * ind.clip01((cl["total"] - self.min_total) / (1_000_000 - self.min_total))
            + 0.3 * ind.clip01((cl["n"] - self.min_insiders) / 3.0)
            + 0.3 * senior
        )
        return Candidate(
            ticker=ticker,
            module=self.name,
            signal_date=df.index[t].date(),
            entry=round(entry, 4),
            stop=round(stop, 4),
            setup_score=round(score, 2),
            rationale=(
                f"{cl['n']} officers/directors bought ${cl['total']:,.0f} of stock on the open "
                f"market (cluster completed {F.date()}); price has now closed above its "
                f"{self.sma_days}-day SMA."
            ),
            details={
                "cluster_completed": F.date().isoformat(),
                "insider_count": cl["n"],
                "cluster_total_value": f(cl["total"], 2),
                "insiders": sorted(str(x) for x in insiders["insider_name"].dropna()),
                "seniority": f(senior, 2),
                "sessions_since_completion": int(t - comp),
                "atr14": f(atr_t),
            },
        )
