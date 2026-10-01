"""Fundamental guardrail (docs/PLAN.md section 8), evaluated for candidates only.

Result is ``pass`` / ``fail`` / ``unknown``:
- fail if TTM operating income AND TTM free cash flow are both negative;
- fail if shares outstanding grew more than ``guardrail.shares_growth_yoy_max`` YoY;
- fail if total debt > ``guardrail.debt_to_op_income_max`` x TTM operating income
  (skipped for Financials and whenever operating income <= 0).
Missing data never becomes a fail: a check that cannot be evaluated is skipped
(listed in ``metrics["unevaluated"]``); if no check can be evaluated at all the
result is ``unknown``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from src.config import load_config
from src.contracts import GuardrailResult
from src.data_access import DataAccess
from src.utils.logging import get_logger
from src.valuation import valuation_info

log = get_logger(__name__)

_FINANCIALS = "financials"


def _ttm(df: pd.DataFrame, metric: str, as_of: date) -> Optional[float]:
    """Sum of the latest 4 quarterly rows (period_end <= as_of) of ``metric``;
    None if fewer than 4 are known."""
    sub = df[(df["metric"] == metric) & (df["period_end"] <= as_of.isoformat())]
    sub = sub.sort_values("period_end")
    if "period_type" in sub:
        sub = sub[sub["period_type"] != "instant"]
    if len(sub) < 4:
        return None
    return float(sub["value"].tail(4).sum())


def _instants(df: pd.DataFrame, metric: str, as_of: date) -> pd.DataFrame:
    sub = df[(df["metric"] == metric) & (df["period_end"] <= as_of.isoformat())]
    return sub.sort_values("period_end")


def _default_fetch(conn, cik: str) -> None:
    from src.data import fundamentals

    fundamentals.fetch_company(conn, cik)


def evaluate(
    ticker: str,
    as_of: date,
    data: DataAccess,
    fetch_fn: Optional[Callable[[Any, str], Any]] = None,
) -> GuardrailResult:
    """Evaluate the guardrail for ``ticker`` as of ``as_of`` (point-in-time by
    filing date). If the DB has no fundamentals for the ticker, ``fetch_fn(conn,
    cik)`` is called once (default: on-demand companyfacts fetch); backtests pass a
    no-op so replay never touches the network."""
    cfg = load_config().guardrail
    conn = data.conn

    df = data.get_fundamentals(ticker, as_of)
    row = conn.execute("SELECT cik, sector FROM tickers WHERE ticker = ?", (ticker,)).fetchone()
    cik = row["cik"] if row else None
    sector = (row["sector"] or "") if row else ""
    if df.empty and cik:
        fetch = fetch_fn or _default_fetch
        try:
            fetch(conn, cik)
        except Exception as exc:  # noqa: BLE001 - missing data is "unknown", not an error
            log.warning("fundamentals fetch failed for %s: %s", ticker, exc)
        df = data.get_fundamentals(ticker, as_of)

    val = valuation_info(ticker, as_of, data)
    if df.empty:
        return GuardrailResult("unknown", ["no fundamentals available"], {}, val)

    fails: list[str] = []
    missing: list[str] = []
    evaluated = 0
    metrics: dict[str, Any] = {}

    op = _ttm(df, "operating_income", as_of)
    ocf = _ttm(df, "operating_cash_flow", as_of)
    capex = _ttm(df, "capex", as_of)
    fcf = ocf - capex if ocf is not None and capex is not None else None
    metrics.update({"op_income_ttm": op, "fcf_ttm": fcf})

    if op is None or fcf is None:
        missing.append("TTM operating income / free cash flow unavailable")
    else:
        evaluated += 1
    if op is not None and fcf is not None and op < 0 and fcf < 0:
        fails.append(
            f"TTM operating income ({op:,.0f}) and free cash flow ({fcf:,.0f}) are both negative"
        )

    shares = _instants(df, "shares_outstanding", as_of)
    growth: Optional[float] = None
    if len(shares) >= 2:
        latest = shares.iloc[-1]
        target = date.fromisoformat(latest["period_end"]) - timedelta(days=365)
        prior = shares[shares["period_end"].map(date.fromisoformat) <= target + timedelta(days=45)]
        if not prior.empty and float(prior.iloc[-1]["value"]) > 0 and prior.iloc[-1]["period_end"] != latest["period_end"]:
            growth = float(latest["value"]) / float(prior.iloc[-1]["value"]) - 1.0
    metrics["shares_growth_yoy"] = growth
    if growth is None:
        missing.append("shares outstanding history unavailable")
    else:
        evaluated += 1
    if growth is not None and growth > cfg.shares_growth_yoy_max:
        fails.append(
            f"shares outstanding grew {growth * 100:+.1f}% year over year "
            f"(limit {cfg.shares_growth_yoy_max * 100:.0f}%)"
        )

    debt_rows = _instants(df, "total_debt", as_of)
    debt = float(debt_rows.iloc[-1]["value"]) if not debt_rows.empty else None
    metrics["total_debt"] = debt
    if sector.lower() == _FINANCIALS or (op is not None and op <= 0):
        pass  # check not applicable
    elif debt is None or op is None:
        missing.append("total debt unavailable")
    else:
        evaluated += 1
        ratio = debt / op
        metrics["debt_to_op_income"] = ratio
        if ratio > cfg.debt_to_op_income_max:
            fails.append(
                f"total debt is {ratio:.1f}x TTM operating income (limit {cfg.debt_to_op_income_max:g}x)"
            )

    if fails:
        return GuardrailResult("fail", fails, metrics, val)
    if evaluated == 0:
        return GuardrailResult("unknown", missing, metrics, val)
    metrics["unevaluated"] = missing
    return GuardrailResult("pass", [], metrics, val)
