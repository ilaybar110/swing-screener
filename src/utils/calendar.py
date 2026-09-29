"""NYSE trading-day calendar helpers, backed by pandas_market_calendars.

All dates in and out are datetime.date. Keep this the single place that knows
about the exchange calendar so point-in-time logic elsewhere stays simple.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal

_CALENDAR_NAME = "NYSE"
_RANGE_START = "1990-01-01"
_RANGE_END = "2035-12-31"


@lru_cache(maxsize=1)
def _schedule() -> pd.DatetimeIndex:
    cal = mcal.get_calendar(_CALENDAR_NAME)
    sched = cal.schedule(start_date=_RANGE_START, end_date=_RANGE_END)
    return pd.DatetimeIndex(sched.index.normalize())


def trading_days(start: date, end: date) -> list[date]:
    """All NYSE trading days in [start, end], inclusive."""
    idx = _schedule()
    mask = (idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))
    return [d.date() for d in idx[mask]]


def is_trading_day(day: date) -> bool:
    idx = _schedule()
    return pd.Timestamp(day) in idx


def add_trading_days(day: date, n: int) -> date:
    """day shifted by n trading days (n may be negative). day itself need not be a
    trading day."""
    idx = _schedule()
    pos = idx.searchsorted(pd.Timestamp(day))
    target = pos + n
    if target < 0 or target >= len(idx):
        raise ValueError(f"add_trading_days({day}, {n}) out of calendar range")
    return idx[target].date()


def previous_trading_day(day: date) -> date:
    idx = _schedule()
    ts = pd.Timestamp(day)
    prior = idx[idx < ts]
    if len(prior) == 0:
        raise ValueError(f"no trading day before {day} in calendar range")
    return prior[-1].date()


def next_trading_day(day: date) -> date:
    idx = _schedule()
    ts = pd.Timestamp(day)
    after = idx[idx > ts]
    if len(after) == 0:
        raise ValueError(f"no trading day after {day} in calendar range")
    return after[0].date()


def latest_trading_day_on_or_before(day: date) -> date:
    if is_trading_day(day):
        return day
    return previous_trading_day(day)
