"""Grouped performance statistics (docs/PLAN.md section 14).

``summary(conn, source)`` returns one JSON-friendly dict used by the report and the
dashboard. "Closed trade" = a recommendation whose entry filled and which reached a
final state (stopped / target_hit / time_stop); expired-unfilled recommendations are
counted separately and never enter the R statistics.

Groups: primary module, "contains" module (any contributing module), rank bucket,
regime, sector, earnings_in_window. Each group reports count, win rate, average and
median R, expectancy, profit factor, average excess return vs SPY / sector and the
comparison with the baseline (random-ticker control) of the same recommendations.
The equity curve is "equal-risk": every trade risks 1R, so it is cumulative R by exit
date.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.config import load_config
from src.utils.logging import get_logger

log = get_logger(__name__)

RANK_BUCKETS = ("1-10", "11-20", "21-50", "51+", "unranked")
NOT_ENOUGH_DATA = "not enough data yet"


def rank_bucket(rank: Any) -> str:
    if rank is None or (isinstance(rank, float) and math.isnan(rank)):
        return "unranked"
    r = int(rank)
    if r <= 10:
        return "1-10"
    if r <= 20:
        return "11-20"
    if r <= 50:
        return "21-50"
    return "51+"


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _mean(s: pd.Series) -> Optional[float]:
    s = s.dropna()
    return _num(s.mean()) if len(s) else None


def _metrics(closed: pd.DataFrame, base: pd.DataFrame) -> dict[str, Any]:
    """Statistics for one group of closed trades and its baseline samples."""
    r = closed["r_multiple"].dropna()
    n = len(r)
    out: dict[str, Any] = {"count": n}
    if n:
        wins, losses = r[r > 0], r[r <= 0]
        win_rate = len(wins) / n
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        gross_loss = -float(losses.sum())
        out.update(
            win_rate=win_rate,
            avg_r=float(r.mean()),
            median_r=float(r.median()),
            expectancy=win_rate * avg_win + (1 - win_rate) * avg_loss,
            profit_factor=(float(wins.sum()) / gross_loss) if gross_loss > 0 else None,
            avg_pct_return=_mean(closed["pct_return"]),
            avg_excess_vs_spy=_mean(closed["excess_vs_spy"]),
            avg_excess_vs_sector=_mean(closed["excess_vs_sector"]),
        )
    else:
        out.update(win_rate=None, avg_r=None, median_r=None, expectancy=None,
                   profit_factor=None, avg_pct_return=None, avg_excess_vs_spy=None,
                   avg_excess_vs_sector=None)
    br = base["r_multiple"].dropna() if len(base) else pd.Series(dtype=float)
    out["baseline_count"] = len(br)
    out["baseline_win_rate"] = float((br > 0).mean()) if len(br) else None
    out["baseline_avg_r"] = float(br.mean()) if len(br) else None
    out["baseline_avg_pct_return"] = _mean(base["pct_return"]) if len(base) else None
    out["excess_vs_baseline_r"] = (
        out["avg_r"] - out["baseline_avg_r"]
        if out["avg_r"] is not None and out["baseline_avg_r"] is not None else None
    )
    pr, bp = out["avg_pct_return"], out["baseline_avg_pct_return"]
    out["excess_vs_baseline_pct"] = pr - bp if pr is not None and bp is not None else None
    return out


def _equity_curve(closed: pd.DataFrame) -> list[dict[str, Any]]:
    if closed.empty:
        return []
    by_day = closed.dropna(subset=["exit_date", "r_multiple"]).groupby("exit_date")["r_multiple"].agg(["sum", "count"])
    by_day = by_day.sort_index()
    cum = by_day["sum"].cumsum()
    return [
        {"date": d, "r": float(by_day.loc[d, "sum"]), "trades": int(by_day.loc[d, "count"]),
         "cum_r": float(cum.loc[d])}
        for d in by_day.index
    ]


def load_frames(conn: sqlite3.Connection, source: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(recommendations joined with results, final baselines joined to parents'
    group columns) for one source."""
    recs = pd.read_sql_query(
        "SELECT r.id, r.signal_date, r.ticker, r.modules, r.primary_module, r.rank, "
        "r.regime, r.sector, r.earnings_in_window, r.status, r.current_r, r.total_score, "
        "x.final_status, x.entry_date, x.exit_date, x.exit_reason, x.r_multiple, "
        "x.pct_return, x.days_held, x.mae_r, x.mfe_r, x.spy_return, x.sector_etf_return, "
        "x.excess_vs_spy, x.excess_vs_sector "
        "FROM recommendations r LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = ?",
        conn, params=(source,),
    )
    base = pd.read_sql_query(
        "SELECT id, parent_rec_id, status, r_multiple, pct_return, exit_date FROM baselines "
        "WHERE source = ? AND status IN ('stopped','target_hit','time_stop')",
        conn, params=(source,),
    )
    if len(recs):
        recs["module_list"] = recs["modules"].map(_parse_modules)
        recs["rank_bucket"] = recs["rank"].map(rank_bucket)
        recs["earn_label"] = recs["earnings_in_window"].map(
            lambda v: "unknown" if v is None or (isinstance(v, float) and math.isnan(v))
            else ("yes" if int(v) else "no"))
    return recs, base


def _parse_modules(v: Any) -> list[str]:
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return [str(m) for m in out] if isinstance(out, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _group(
    closed: pd.DataFrame, base: pd.DataFrame, key: str, order: Optional[tuple] = None
) -> dict[str, dict[str, Any]]:
    res: dict[str, dict[str, Any]] = {}
    keys = [k for k in closed[key].dropna().unique()] if len(closed) else []
    if order:
        keys = [k for k in order if k in keys]
    else:
        keys = sorted(keys)
    for k in keys:
        sub = closed[closed[key] == k]
        res[str(k)] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])])
    return res


def summary(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    """Full statistics for one source ("live" or a backtest run_id)."""
    min_closed = load_config().ranking.track_record_min_closed_trades
    recs, base = load_frames(conn, source)
    out: dict[str, Any] = {"source": source, "min_closed_for_confidence": min_closed}
    if recs.empty:
        out.update(counts={"recommendations": 0, "open": 0, "pending": 0, "closed": 0,
                           "expired": 0}, overall=_metrics(recs.reindex(columns=["r_multiple"]), base),
                   by_module_primary={}, by_module_contains={}, by_rank_bucket={},
                   by_regime={}, by_sector={}, by_earnings_in_window={}, equity_curve=[],
                   equity_curve_by_module={}, warnings={})
        return out

    closed = recs[recs["entry_date"].notna() & recs["final_status"].notna()].copy()
    out["counts"] = {
        "recommendations": len(recs),
        "open": int((recs["status"] == "open").sum()),
        "pending": int((recs["status"] == "pending").sum()),
        "closed": len(closed),
        "expired": int((recs["final_status"] == "expired").sum()),
    }
    out["overall"] = _metrics(closed, base)
    out["by_module_primary"] = _group(closed, base, "primary_module")

    contains: dict[str, dict[str, Any]] = {}
    mods = sorted({m for lst in recs["module_list"] for m in lst})
    for m in mods:
        sub = closed[closed["module_list"].map(lambda lst, m=m: m in lst)]
        contains[m] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])])
    out["by_module_contains"] = contains

    out["by_rank_bucket"] = _group(closed, base, "rank_bucket", RANK_BUCKETS)
    out["by_regime"] = _group(closed, base, "regime")
    out["by_sector"] = _group(closed, base, "sector")
    out["by_earnings_in_window"] = _group(closed, base, "earn_label", ("yes", "no", "unknown"))

    out["equity_curve"] = _equity_curve(closed)
    out["equity_curve_by_module"] = {
        m: _equity_curve(closed[closed["primary_module"] == m])
        for m in sorted(closed["primary_module"].dropna().unique())
    }
    # Sample-size warnings, one per module that has recommendations at all.
    warnings: dict[str, str] = {}
    for m in sorted({*recs["primary_module"].dropna(), *mods}):
        n_closed = out["by_module_primary"].get(m, {}).get("count", 0)
        if n_closed < min_closed:
            warnings[m] = f"{NOT_ENOUGH_DATA} ({n_closed}/{min_closed} closed)"
    out["warnings"] = warnings
    return _clean(out)


BACKTEST_PERIODS: tuple[tuple[str, str, str], ...] = (
    ("2015-2019", "2015-01-01", "2019-12-31"),
    ("2020-2022", "2020-01-01", "2022-12-31"),
    ("2023+", "2023-01-01", "9999-12-31"),
)


def summary_by_period(
    conn: sqlite3.Connection,
    source: str,
    periods: tuple[tuple[str, str, str], ...] = BACKTEST_PERIODS,
) -> dict[str, dict[str, dict[str, Any]]]:
    """{period label: {"ALL": metrics, <primary module>: metrics, ...}} with trades
    assigned to a period by signal date (inclusive ISO date bounds). Periods with no
    recommendations are omitted."""
    recs, base = load_frames(conn, source)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if recs.empty:
        return out
    closed_all = recs[recs["entry_date"].notna() & recs["final_status"].notna()]
    for label, lo, hi in periods:
        in_period = recs[(recs["signal_date"] >= lo) & (recs["signal_date"] <= hi)]
        if in_period.empty:
            continue
        closed = closed_all[closed_all["id"].isin(in_period["id"])]
        res = {"ALL": _metrics(closed, base[base["parent_rec_id"].isin(closed["id"])])}
        res["ALL"]["recommendations"] = len(in_period)
        for m in sorted(in_period["primary_module"].dropna().unique()):
            sub = closed[closed["primary_module"] == m]
            res[m] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])])
            res[m]["recommendations"] = int((in_period["primary_module"] == m).sum())
        out[label] = _clean(res)
    return out


def _clean(obj: Any) -> Any:
    """NaN/inf -> None and numpy scalars -> Python so the dict is JSON-safe."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return _num(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def group_table(summary_dict: dict[str, Any], section: str) -> pd.DataFrame:
    """One summary section (e.g. "by_rank_bucket") as a DataFrame, one row per group.
    Convenience for the report and the dashboard."""
    rows = [{"group": k, **v} for k, v in summary_dict.get(section, {}).items()]
    return pd.DataFrame(rows)
