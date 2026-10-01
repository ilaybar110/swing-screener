"""Shared scaffolding for the strategy modules (owned by Bot 4).

A module implements ``scan_ticker`` once, vectorized over a ticker's whole price
history. ``scan_history(start, end)`` runs it over [start, end]; ``scan(as_of)`` is the
same computation with the data loaded only up to ``as_of`` and the evaluation range
collapsed to that single day. Because every indicator is causal (see
``src/indicators.py``) and no price row after the evaluation end is ever loaded, a day's
result cannot depend on later data, and scan/scan_history agree by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import numpy as np
import pandas as pd

from src import indicators as ind
from src.contracts import Candidate
from src.utils.logging import get_logger

log = get_logger(__name__)


def param(cfg_obj: Any, name: str, default: Any) -> Any:
    """Read ``name`` from a config section, falling back to a documented default for
    parameters that config.yaml does not carry yet (see docs/CHANGE_REQUESTS.md)."""
    return getattr(cfg_obj, name, default)


@dataclass
class ScanContext:
    """Everything a module needs to evaluate one ticker over the evaluation range."""

    data: Any
    eval_dates: pd.DatetimeIndex
    rs_pct: pd.DataFrame  # dates x tickers, within-universe percentile (0-100)
    start: date
    end: date
    spy: pd.DataFrame = field(default_factory=pd.DataFrame)


class PanelModule:
    """Base class implementing scan/scan_history on top of ``scan_ticker``."""

    name: str = ""
    needs_rs: bool = False
    rs_lookback: int = 126
    rs_skip: int = 21

    def __init__(self, config) -> None:
        self.config = config
        self.stop_min = config.modules.stop_distance_pct_min
        self.stop_max = config.modules.stop_distance_pct_max

    # -- StrategyModule interface -------------------------------------------------

    def scan(self, as_of: date, data) -> list[Candidate]:
        return self._run(as_of, as_of, data)

    def scan_history(self, start: date, end: date, data) -> list[Candidate]:
        return self._run(start, end, data)

    # -- to be provided by subclasses ----------------------------------------------

    def scan_ticker(
        self, ticker: str, df: pd.DataFrame, ctx: ScanContext
    ) -> list[Candidate]:
        """Candidates for one ticker; ``signal_date`` may be any session in
        ``ctx.eval_dates`` (others are discarded by the caller)."""
        raise NotImplementedError

    def prepare(self, tickers: list[str], ctx: ScanContext) -> None:
        """Optional hook to bulk-load per-run data (e.g. insider trades)."""

    # -- machinery ------------------------------------------------------------------

    def _run(self, start: date, end: date, data) -> list[Candidate]:
        from src.utils.calendar import trading_days

        days = trading_days(start, end)
        if not days:
            return []
        member_by_date = ind.universe_membership(
            data, pd.DatetimeIndex([pd.Timestamp(d) for d in days])
        )
        tickers = sorted(set().union(*member_by_date.values())) if member_by_date else []
        if not tickers:
            return []
        panel = ind.load_panel(data, tickers, start, end)
        if ind.BENCHMARK not in panel:
            log.warning("%s: no %s prices; nothing to scan", self.name, ind.BENCHMARK)
            return []
        cal = panel[ind.BENCHMARK].index
        eval_dates = pd.DatetimeIndex([pd.Timestamp(d) for d in days]).intersection(cal)
        if len(eval_dates) == 0:
            return []
        tickers = [t for t in tickers if t in panel]
        member = ind.membership_frame(member_by_date, cal, tickers)
        rs_pct = (
            ind.rs_percentile_panel(panel, member, self.rs_lookback, self.rs_skip)
            if self.needs_rs
            else pd.DataFrame(index=cal)
        )
        ctx = ScanContext(
            data=data, eval_dates=eval_dates, rs_pct=rs_pct, start=start, end=end,
            spy=panel[ind.BENCHMARK],
        )
        self.prepare(tickers, ctx)
        out: list[Candidate] = []
        for t in tickers:
            for cand in self.scan_ticker(t, panel[t], ctx):
                ts = pd.Timestamp(cand.signal_date)
                if ts in eval_dates and bool(member.at[ts, t]):
                    out.append(cand)
        out.sort(key=lambda c: (c.signal_date, c.ticker, c.module))
        log.info("%s: %d candidate(s) for %s..%s", self.name, len(out), start, end)
        return out

    def stop_ok(self, entry: float, stop: float) -> bool:
        return ind.stop_distance_ok(entry, stop, self.stop_min, self.stop_max)


def f(x: Any, nd: int = 4) -> Optional[float]:
    """JSON-friendly rounded float (None for NaN/inf)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if np.isfinite(v) else None
