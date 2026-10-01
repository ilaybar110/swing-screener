"""Informational valuation context (docs/PLAN.md section 8).

EV/FCF and its percentile versus the company's sector come from the weekly
``fundamentals_summary`` snapshot. Purely informational -- never a pass/fail input.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.data_access import DataAccess


def valuation_info(ticker: str, as_of: date, data: DataAccess) -> dict[str, Any]:
    """{"ev_fcf", "ev_fcf_sector_pct", "summary_as_of"} from the latest summary
    row known on ``as_of``; empty dict when there is none."""
    row = data.get_fundamentals_summary(ticker, as_of)
    if not row:
        return {}
    info: dict[str, Any] = {}
    if row.get("ev_fcf") is not None:
        info["ev_fcf"] = float(row["ev_fcf"])
    if row.get("ev_fcf_sector_pct") is not None:
        info["ev_fcf_sector_pct"] = float(row["ev_fcf_sector_pct"])
    if info:
        info["summary_as_of"] = row.get("as_of")
    return info
