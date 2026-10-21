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

Rigor additions (``measurement`` section of config.yaml):

* ``expectancy`` is kept for compatibility (report and dashboard read it) but is by
  definition identical to ``avg_r`` (win_rate * avg_win + loss_rate * avg_loss is the
  mean R), so it is now simply an alias of ``avg_r``.
* Confidence intervals are percentile **cluster bootstraps**: observations are grouped
  by signal week (ISO week, Monday-dated) and whole weeks are resampled with
  replacement, because recommendations of the same week share market moves and are
  not independent. The RNG is seeded (``measurement.bootstrap_seed``) so outputs are
  deterministic. CIs are None for fewer than 2 observations or 2 weeks.
* ``paired_excess_r``: each closed recommendation's R minus the mean R of ITS OWN
  final baselines (recommendations without a final baseline are left out), averaged.
* ``evidence``: "insufficient" until ``min_n_for_evidence`` observations over at least
  ``min_weeks_for_evidence`` signal weeks exist; then "positive" / "negative" when the
  CI excludes zero on that side, else "no_edge". Basis is the paired excess when it has
  enough pairs, otherwise avg_r (``evidence_basis`` says which). The older
  ``warnings`` / ``min_closed_for_confidence`` fields are unchanged.
* ``by_horizon``: fixed-horizon forward returns (``src/forward_returns.py``) per
  primary module and overall, with excess vs SPY / sector / own baselines.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.config import MeasurementConfig, load_config
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


def signal_week(signal_date: Any) -> Optional[str]:
    """ISO date of the Monday of ``signal_date``'s week (the bootstrap cluster key)."""
    if signal_date is None or (isinstance(signal_date, float) and math.isnan(signal_date)):
        return None
    d = pd.Timestamp(signal_date)
    return (d - pd.Timedelta(days=d.weekday())).date().isoformat()


def cluster_bootstrap_ci(
    values: Any,
    clusters: Any,
    iterations: int,
    seed: int,
    level: float = 0.95,
) -> Optional[list[float]]:
    """Percentile CI ``[lo, hi]`` of the mean of ``values`` under a cluster bootstrap:
    whole clusters (e.g. signal weeks) are resampled with replacement and the pooled
    mean of each resample is taken. Deterministic for a given seed. None when there
    are fewer than 2 finite values or 2 clusters."""
    v = pd.to_numeric(pd.Series(list(values), dtype=object), errors="coerce").to_numpy(dtype=float)
    c = pd.Series(list(clusters), dtype=object).astype(str).to_numpy()
    ok = np.isfinite(v)
    v, c = v[ok], c[ok]
    if len(v) < 2:
        return None
    labels, codes = np.unique(c, return_inverse=True)  # sorted labels: order-independent
    k = len(labels)
    if k < 2 or iterations <= 0:
        return None
    sums = np.bincount(codes, weights=v, minlength=k)
    cnts = np.bincount(codes, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    means = np.empty(iterations)
    chunk = max(1, 2_000_000 // k)  # bound memory for many clusters
    for s0 in range(0, iterations, chunk):
        idx = rng.integers(0, k, size=(min(chunk, iterations - s0), k))
        means[s0:s0 + len(idx)] = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(means, [alpha, 1.0 - alpha])
    return [float(lo), float(hi)]


def evidence_label(n: int, n_weeks: int, ci: Optional[list[float]], m: MeasurementConfig) -> str:
    """"insufficient" | "positive" | "negative" | "no_edge" from a CI and sample size."""
    if n < m.min_n_for_evidence or n_weeks < m.min_weeks_for_evidence or ci is None:
        return "insufficient"
    if ci[0] > 0:
        return "positive"
    if ci[1] < 0:
        return "negative"
    return "no_edge"


def _ci(values: pd.Series, weeks: pd.Series, m: MeasurementConfig) -> Optional[list[float]]:
    return cluster_bootstrap_ci(values, weeks, m.bootstrap_iterations, m.bootstrap_seed, m.ci_level)


def _measurement() -> MeasurementConfig:
    return load_config().measurement


def _metrics(
    closed: pd.DataFrame,
    base: pd.DataFrame,
    group: Optional[pd.DataFrame] = None,
    m: Optional[MeasurementConfig] = None,
) -> dict[str, Any]:
    """Statistics for one group of closed trades and its baseline samples.

    ``group``: every recommendation of the group (any status) -- needed for
    ``fill_rate`` / ``expired_share``, which are None without it."""
    m = m or _measurement()
    closed = closed.reindex(columns=closed.columns.union(
        ["r_multiple", "signal_date", "id", "mae_r", "mfe_r", "pct_return",
         "excess_vs_spy", "excess_vs_sector"], sort=False))
    rr = pd.to_numeric(closed["r_multiple"], errors="coerce")
    r = rr.dropna()
    n = len(r)
    weeks = closed["signal_date"].map(signal_week)
    out: dict[str, Any] = {"count": n}
    if n:
        wins, losses = r[r > 0], r[r <= 0]
        win_rate = len(wins) / n
        gross_loss = -float(losses.sum())
        out.update(
            win_rate=win_rate,
            avg_r=float(r.mean()),
            median_r=float(r.median()),
            # == avg_r by definition (win_rate*avg_win + loss_rate*avg_loss); kept as an
            # alias because the report and the dashboard read this key
            expectancy=float(r.mean()),
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

    # --- rigor: CIs, paired excess vs own baselines, MAE/MFE, fill rate, evidence
    valid = rr.notna()
    n_weeks = int(weeks[valid].dropna().nunique())
    out["signal_weeks"] = n_weeks
    out["avg_r_ci"] = _ci(rr[valid], weeks[valid], m) if n else None
    paired_n, paired_mean, paired_ci, paired_weeks = 0, None, None, 0
    if n and len(base):
        bmean = (base.assign(_r=pd.to_numeric(base["r_multiple"], errors="coerce"))
                 .dropna(subset=["_r"]).groupby("parent_rec_id")["_r"].mean())
        own = closed.loc[valid, "id"].map(bmean)
        diff = (rr[valid] - own).dropna()
        paired_n = len(diff)
        if paired_n:
            dw = weeks[valid][diff.index]
            paired_mean = float(diff.mean())
            paired_weeks = int(dw.dropna().nunique())
            paired_ci = _ci(diff, dw, m)
    out["paired_count"] = paired_n
    out["paired_excess_r"] = paired_mean
    out["paired_excess_r_ci"] = paired_ci
    mae = pd.to_numeric(closed["mae_r"], errors="coerce").dropna()
    mfe = pd.to_numeric(closed["mfe_r"], errors="coerce").dropna()
    out["median_mae_r"] = float(mae.median()) if len(mae) else None
    out["median_mfe_r"] = float(mfe.median()) if len(mfe) else None
    out.update(_fill_stats(group))
    if paired_n >= m.min_n_for_evidence:
        out["evidence_basis"] = "paired_excess_r"
        out["evidence"] = evidence_label(paired_n, paired_weeks, paired_ci, m)
    elif n >= m.min_n_for_evidence:
        out["evidence_basis"] = "avg_r"
        out["evidence"] = evidence_label(n, n_weeks, out["avg_r_ci"], m)
    else:
        out["evidence_basis"] = None
        out["evidence"] = "insufficient"
    return out


def _fill_stats(group: Optional[pd.DataFrame]) -> dict[str, Any]:
    """fill_rate = filled / (filled + expired) over recommendations whose fill is
    decided (open or final; still-pending ones excluded); expired_share = expired
    unfilled / all recommendations of the group."""
    if group is None or not len(group) or "status" not in group.columns:
        return {"fill_rate": None, "expired_share": None, "filled": None, "expired_unfilled": None}
    fs = group["final_status"] if "final_status" in group.columns else pd.Series(None, index=group.index)
    ed = group["entry_date"] if "entry_date" in group.columns else pd.Series(None, index=group.index)
    expired = int((fs == "expired").sum())
    filled = int((ed.notna() | ((group["status"] == "open") & fs.isna())).sum())
    decided = filled + expired
    return {
        "fill_rate": filled / decided if decided else None,
        "expired_share": expired / len(group),
        "filled": filled,
        "expired_unfilled": expired,
    }


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
    closed: pd.DataFrame,
    base: pd.DataFrame,
    key: str,
    order: Optional[tuple] = None,
    recs: Optional[pd.DataFrame] = None,
    m: Optional[MeasurementConfig] = None,
) -> dict[str, dict[str, Any]]:
    res: dict[str, dict[str, Any]] = {}
    keys = [k for k in closed[key].dropna().unique()] if len(closed) else []
    if order:
        keys = [k for k in order if k in keys]
    else:
        keys = sorted(keys)
    for k in keys:
        sub = closed[closed[key] == k]
        grp = recs[recs[key] == k] if recs is not None else None
        res[str(k)] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])], grp, m)
    return res


def _horizon_metrics(
    rec_fr: pd.DataFrame, base_mean: pd.Series, m: MeasurementConfig
) -> dict[str, Any]:
    """One horizon's statistics for one group of recommendations' forward returns.
    ``rec_fr``: rows of kind "rec" (one per recommendation); ``base_mean``: mean
    forward return of each recommendation's own baselines, indexed by rec id."""
    ret = pd.to_numeric(rec_fr["ret"], errors="coerce")
    weeks = rec_fr["signal_date"].map(signal_week)
    n = int(ret.notna().sum())
    out: dict[str, Any] = {"n": n}
    out["mean_ret"] = _mean(ret)
    out["median_ret"] = _num(ret.median()) if n else None
    out["win_rate"] = float((ret.dropna() > 0).mean()) if n else None
    es = pd.to_numeric(rec_fr["excess_vs_spy"], errors="coerce")
    esec = pd.to_numeric(rec_fr["excess_vs_sector"], errors="coerce")
    out["n_spy"] = int(es.notna().sum())
    out["mean_excess_vs_spy"] = _mean(es)
    out["excess_vs_spy_ci"] = _ci(es[es.notna()], weeks[es.notna()], m)
    out["n_sector"] = int(esec.notna().sum())
    out["mean_excess_vs_sector"] = _mean(esec)
    out["excess_vs_sector_ci"] = _ci(esec[esec.notna()], weeks[esec.notna()], m)
    own = rec_fr["subject_id"].map(base_mean)
    diff = (ret - own).dropna()
    dw = weeks[diff.index]
    out["n_paired"] = len(diff)
    out["baseline_mean_ret"] = _mean(own[diff.index])
    out["mean_excess_vs_baseline"] = _num(diff.mean()) if len(diff) else None
    out["excess_vs_baseline_ci"] = _ci(diff, dw, m)
    out["evidence"] = evidence_label(len(diff), int(dw.dropna().nunique()),
                                     out["excess_vs_baseline_ci"], m)
    return out


def _by_horizon(conn: sqlite3.Connection, source: str, recs: pd.DataFrame,
                m: MeasurementConfig) -> dict[str, Any]:
    """{"horizons": [5, ...], "overall": {"5d": metrics, ...},
    "by_module": {primary_module: {"5d": metrics, ...}}} from forward_returns."""
    from src.forward_returns import load as load_forward  # local: avoids import cycle at module load

    horizons = sorted(set(int(h) for h in m.forward_horizons if int(h) > 0))
    fr = load_forward(conn, source)
    mods = sorted(recs["primary_module"].dropna().unique()) if len(recs) else []
    out: dict[str, Any] = {"horizons": horizons, "overall": {}, "by_module": {md: {} for md in mods}}
    pm = recs.set_index("id")["primary_module"] if len(recs) else pd.Series(dtype=object)
    for h in horizons:
        hf = fr[pd.to_numeric(fr["horizon"], errors="coerce") == h]
        rec_fr = hf[hf["kind"] == "rec"]
        rec_fr = rec_fr[rec_fr["subject_id"].isin(pm.index)]
        b = hf[hf["kind"] == "baseline"]
        base_mean = (b.assign(_r=pd.to_numeric(b["ret"], errors="coerce"))
                     .dropna(subset=["_r"]).groupby("parent_rec_id")["_r"].mean())
        key = f"{h}d"
        out["overall"][key] = _horizon_metrics(rec_fr, base_mean, m)
        mod_of = rec_fr["subject_id"].map(pm)
        for md in mods:
            out["by_module"][md][key] = _horizon_metrics(rec_fr[mod_of == md], base_mean, m)
    return out


def summary(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    """Full statistics for one source ("live" or a backtest run_id)."""
    cfg = load_config()
    min_closed = cfg.ranking.track_record_min_closed_trades
    m = cfg.measurement
    recs, base = load_frames(conn, source)
    out: dict[str, Any] = {
        "source": source, "min_closed_for_confidence": min_closed,
        "ci_level": m.ci_level,
        "bootstrap": {"iterations": m.bootstrap_iterations, "seed": m.bootstrap_seed,
                      "cluster": "signal_week"},
    }
    if recs.empty:
        out.update(counts={"recommendations": 0, "open": 0, "pending": 0, "closed": 0,
                           "expired": 0}, overall=_metrics(recs.reindex(columns=["r_multiple"]), base, None, m),
                   by_module_primary={}, by_module_contains={}, by_rank_bucket={},
                   by_regime={}, by_sector={}, by_earnings_in_window={}, equity_curve=[],
                   equity_curve_by_module={}, warnings={}, evidence_by_module={},
                   by_horizon=_by_horizon(conn, source, recs, m))
        return _clean(out)

    closed = recs[recs["entry_date"].notna() & recs["final_status"].notna()].copy()
    out["counts"] = {
        "recommendations": len(recs),
        "open": int((recs["status"] == "open").sum()),
        "pending": int((recs["status"] == "pending").sum()),
        "closed": len(closed),
        "expired": int((recs["final_status"] == "expired").sum()),
    }
    out["overall"] = _metrics(closed, base, recs, m)
    out["by_module_primary"] = _group(closed, base, "primary_module", recs=recs, m=m)

    contains: dict[str, dict[str, Any]] = {}
    mods = sorted({m for lst in recs["module_list"] for m in lst})
    for md in mods:
        # .loc + astype(bool): an empty `closed` (nothing has closed yet -- the normal state
        # in the first weeks of live running) must keep its columns
        sub = closed.loc[closed["module_list"].map(lambda lst, md=md: md in lst).astype(bool)]
        grp = recs.loc[recs["module_list"].map(lambda lst, md=md: md in lst).astype(bool)]
        contains[md] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])], grp, m)
    out["by_module_contains"] = contains

    out["by_rank_bucket"] = _group(closed, base, "rank_bucket", RANK_BUCKETS, recs, m)
    out["by_regime"] = _group(closed, base, "regime", recs=recs, m=m)
    out["by_sector"] = _group(closed, base, "sector", recs=recs, m=m)
    out["by_earnings_in_window"] = _group(closed, base, "earn_label", ("yes", "no", "unknown"),
                                          recs, m)

    out["equity_curve"] = _equity_curve(closed)
    out["equity_curve_by_module"] = {
        md: _equity_curve(closed[closed["primary_module"] == md])
        for md in sorted(closed["primary_module"].dropna().unique())
    }
    # Sample-size warnings, one per module that has recommendations at all (legacy
    # flat threshold, kept for compatibility; ``evidence`` is the CI-based successor).
    warnings: dict[str, str] = {}
    evidence: dict[str, str] = {}
    for md in sorted({*recs["primary_module"].dropna(), *mods}):
        n_closed = out["by_module_primary"].get(md, {}).get("count", 0)
        if n_closed < min_closed:
            warnings[md] = f"{NOT_ENOUGH_DATA} ({n_closed}/{min_closed} closed)"
        evidence[md] = out["by_module_primary"].get(md, {}).get("evidence", "insufficient")
    out["warnings"] = warnings
    out["evidence_by_module"] = evidence
    out["by_horizon"] = _by_horizon(conn, source, recs, m)
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
    meas = _measurement()
    closed_all = recs[recs["entry_date"].notna() & recs["final_status"].notna()]
    for label, lo, hi in periods:
        in_period = recs[(recs["signal_date"] >= lo) & (recs["signal_date"] <= hi)]
        if in_period.empty:
            continue
        closed = closed_all[closed_all["id"].isin(in_period["id"])]
        res = {"ALL": _metrics(closed, base[base["parent_rec_id"].isin(closed["id"])],
                               in_period, meas)}
        res["ALL"]["recommendations"] = len(in_period)
        for m in sorted(in_period["primary_module"].dropna().unique()):
            sub = closed[closed["primary_module"] == m]
            res[m] = _metrics(sub, base[base["parent_rec_id"].isin(sub["id"])],
                              in_period[in_period["primary_module"] == m], meas)
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
