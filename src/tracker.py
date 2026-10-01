"""Stateless trade tracker (docs/PLAN.md section 11).

``simulate()`` is a pure, deterministic function: given one recommendation and a
price history it replays the trade from the signal date and reports where it stands.
The live pipeline (``update_all``) and the backtest runner both call it, so live and
backtest statistics are produced by the identical code path.

Bar-level conventions (all documented because daily bars hide intraday order):

* Bars strictly after ``signal_date`` are considered; the signal bar itself is the
  trigger day, the buy-stop is working from the next session.
* PENDING: the first bar with ``high >= entry`` fills (at ``entry``, or at the open
  when the bar gapped above it). The order lives ``entry_validity_trading_days``
  bars; a close below the stop before any fill expires it early.
* A stop touched on the entry bar counts (stop-out). If stop and target are both
  touched on the same bar the stop wins. On the entry bar the stop exits at the stop
  price; on later bars at the open when it gapped below the stop.
* Target fills at the target price (not improved for gaps - conservative). After the
  partial exit the remaining stop moves to the actual fill price, effective from the
  *next* bar (the order within the target bar is unknowable).
* The remainder exits on a close below SMA20 at the *next* open, or at breakeven, or
  at the close of the ``time_stop_trading_days``-th bar after the entry bar.
* If the history ends before an exit is determined the trade stays open.
* Cost: ``round_trip_cost_pct`` of the entry fill, charged once on the whole position.
* R is measured against planned risk ``entry - stop`` (split-adjusted).

``entry_mode="open"`` (used by baselines) fills at the first open after the signal
and derives stop/target from the stop distance percentage of ``rec``.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.config import Config, load_config
from src.contracts import Recommendation, TrackResult
from src.utils.logging import get_logger

log = get_logger(__name__)

FINAL_STATUSES = ("stopped", "target_hit", "time_stop", "expired")
BENCHMARK = "SPY"
# Calendar-day history loaded ahead of the signal date so SMA20 is warm at entry.
_WARMUP_CALENDAR_DAYS = 60


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_date(value: Any) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


class _Bars:
    """Column arrays for one ticker, ascending by date."""

    def __init__(self, df: pd.DataFrame):
        df = df.sort_values("date")
        self.dates: list[date] = [_to_date(d) for d in df["date"]]
        self.o = np.array(df["open"], dtype=float)
        self.h = np.array(df["high"], dtype=float)
        self.l = np.array(df["low"], dtype=float)
        self.c = np.array(df["close"], dtype=float)
        self.n = len(self.dates)

    def index_after(self, d: date) -> int:
        """Index of the first bar strictly after d."""
        return bisect.bisect_right(self.dates, d)

    def index_on_or_after(self, d: date) -> Optional[int]:
        i = bisect.bisect_left(self.dates, d)
        return i if i < self.n else None

    def index_on_or_before(self, d: date) -> Optional[int]:
        i = bisect.bisect_right(self.dates, d) - 1
        return i if i >= 0 else None


def _select(prices_df: Optional[pd.DataFrame], ticker: str, own: str) -> Optional[pd.DataFrame]:
    """Rows of prices_df belonging to ticker. A frame without a 'ticker' column is
    taken to be the recommendation's own ticker (``own``) only."""
    if prices_df is None or len(prices_df) == 0:
        return None
    if "ticker" in prices_df.columns:
        sub = prices_df[prices_df["ticker"] == ticker]
        return sub if len(sub) else None
    return prices_df if ticker == own else None


def _split_factor_and_normalise(
    bars: _Bars, splits: list[tuple[date, float]], signal_date: date
) -> float:
    """Return the cumulative split factor for splits after signal_date and, if the
    history turns out not to be split-adjusted, adjust earlier bars in place.

    Spec: yfinance (auto_adjust=False) returns split-adjusted OHLC, so stored
    entry/stop/target (struck on the signal date's share basis) are divided by the
    cumulative factor. Defensive: if the previous close / split-day open
    discontinuity looks like the split ratio, the history is raw and bars before the
    split are divided by the ratio too."""
    factor = 1.0
    for sd, ratio in sorted(splits):
        if not ratio or ratio <= 0:
            continue
        if sd > signal_date:
            factor *= ratio
        i = bars.index_on_or_after(sd)
        if i is None or i == 0 or bars.o[i] <= 0:
            continue
        jump = bars.c[i - 1] / bars.o[i]
        if abs(jump / ratio - 1.0) < 0.25 and abs(ratio - 1.0) > 0.05:
            for arr in (bars.o, bars.h, bars.l, bars.c):
                arr[:i] = arr[:i] / ratio
    return factor


def _sma(close: np.ndarray, window: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if window <= 0 or len(close) < window:
        return out
    cs = np.cumsum(np.insert(close, 0, 0.0))
    out[window - 1:] = (cs[window:] - cs[:-window]) / window
    return out


def _bench_return(
    series: Optional[_Bars], entry_date: date, exit_date: date, exit_at_open: bool
) -> Optional[float]:
    if series is None:
        return None
    i0 = series.index_on_or_after(entry_date)
    i1 = series.index_on_or_before(exit_date)
    if i0 is None or i1 is None or series.dates[i0] > exit_date or series.dates[i1] < entry_date:
        return None
    start = series.o[i0]
    end = series.o[i1] if exit_at_open else series.c[i1]
    if not start or math.isnan(start) or math.isnan(end):
        return None
    return float(end / start - 1.0)


# ---------------------------------------------------------------------------
# simulate
# ---------------------------------------------------------------------------


def simulate(
    rec: Recommendation,
    prices_df: pd.DataFrame,
    splits_df: Optional[pd.DataFrame],
    config: Config,
    entry_mode: str = "stop",
) -> TrackResult:
    """Replay ``rec`` on ``prices_df`` and return its TrackResult.

    ``prices_df``: columns date, open, high, low, close; optionally ``ticker`` so the
    same frame can also carry SPY and the sector ETF (needed for excess returns; they
    are None otherwise). Without a ``ticker`` column the frame is the rec's ticker.
    Include some history before the signal date (>= exit_sma_days bars) so the SMA is
    warm. ``splits_df``: columns date, ratio (and optionally ticker).
    ``entry_mode``: "stop" (buy-stop, the real trade plan) or "open" (baseline).
    """
    tp = config.trade_plan
    cost_pct = config.tracking.round_trip_cost_pct
    signal_date = _to_date(rec.signal_date)

    stock_df = _select(prices_df, rec.ticker, rec.ticker)
    if stock_df is None:
        return TrackResult(status="pending")
    bars = _Bars(stock_df.copy())

    splits: list[tuple[date, float]] = []
    if splits_df is not None and len(splits_df):
        sdf = splits_df
        if "ticker" in sdf.columns:
            sdf = sdf[sdf["ticker"] == rec.ticker]
        splits = [(_to_date(d), float(r)) for d, r in zip(sdf["date"], sdf["ratio"])]
    factor = _split_factor_and_normalise(bars, splits, signal_date)

    entry = rec.entry / factor
    stop = rec.stop / factor
    target = rec.target / factor
    if entry_mode == "open":
        stop_pct = (rec.entry - rec.stop) / rec.entry  # ratio: split-invariant
    sma = _sma(bars.c, tp.exit_sma_days)

    n = bars.n
    start = bars.index_after(signal_date)

    # ---- entry -----------------------------------------------------------
    i0: Optional[int] = None
    if entry_mode == "open":
        if start >= n:
            return TrackResult(status="pending")
        i0 = start
        fill = float(bars.o[i0])
        entry = fill
        stop = fill * (1.0 - stop_pct)
        target = fill + tp.target_r_multiple * (fill - stop)
    else:
        for k in range(tp.entry_validity_trading_days):
            i = start + k
            if i >= n:
                return TrackResult(status="pending")
            if bars.h[i] >= entry:
                i0 = i
                break
            if bars.c[i] < stop:
                return TrackResult(
                    status="expired", exit_date=bars.dates[i],
                    exit_reason="setup_broken", is_final=True,
                )
        if i0 is None:
            return TrackResult(
                status="expired", exit_date=bars.dates[start + tp.entry_validity_trading_days - 1],
                exit_reason="entry_window_elapsed", is_final=True,
            )
        fill = float(max(entry, bars.o[i0]))

    risk = entry - stop
    if risk <= 0:
        raise ValueError(f"{rec.id}: non-positive risk (entry={entry}, stop={stop})")
    frac = tp.partial_exit_fraction

    # ---- trade life ------------------------------------------------------
    partial = False
    cur_stop = stop
    sma_pending = False
    exit_idx: Optional[int] = None
    exit_px = 0.0
    reason = ""
    for i in range(i0, n):
        first = i == i0
        if sma_pending:  # close < SMA20 on the previous bar -> sell this open
            exit_idx, exit_px, reason = i, float(bars.o[i]), "sma20_exit"
            break
        if bars.l[i] <= cur_stop:
            gap_below = (not first) and bars.o[i] < cur_stop
            exit_idx, exit_px = i, float(bars.o[i] if gap_below else cur_stop)
            reason = "breakeven" if partial else "stop"
            break
        if not partial and bars.h[i] >= target:
            partial = True
            cur_stop = fill  # effective from the next bar
        if i - i0 >= tp.time_stop_trading_days:
            exit_idx, exit_px, reason = i, float(bars.c[i]), "time_stop"
            break
        if partial and not math.isnan(sma[i]) and bars.c[i] < sma[i]:
            sma_pending = True

    closed = exit_idx is not None
    last = exit_idx if closed else n - 1

    def blended(rem_px: float) -> float:
        return frac * target + (1.0 - frac) * rem_px if partial else rem_px

    def r_of(avg_px: float) -> float:
        return (avg_px - fill) / risk - cost_pct * fill / risk

    # MAE / MFE in R over the bars the position was actually held
    mfe = 0.0
    mae = 0.0
    for j in range(i0, last + 1):
        post_fill = (entry_mode == "open") or bars.o[j] >= entry or j > i0
        hi: Optional[float] = float(bars.h[j])
        lo: Optional[float] = float(bars.l[j]) if post_fill else None
        if closed and j == exit_idx:
            if reason in ("stop", "breakeven"):
                hi = float(bars.o[j]) if j != i0 else fill
                lo = exit_px  # out at the stop/gap price; later lows are irrelevant
            elif reason == "sma20_exit":
                hi = lo = float(bars.o[j])
        if hi is not None:
            mfe = max(mfe, (hi - fill) / risk)
        if lo is not None:
            mae = min(mae, (lo - fill) / risk)

    if not closed:
        return _open_result(bars, i0, fill, r_of(blended(float(bars.c[n - 1]))), mae, mfe)

    avg_exit = blended(exit_px)
    if reason == "time_stop":
        status = "time_stop"
    elif partial:
        status = "target_hit"
    else:
        status = "stopped"
    exit_date = bars.dates[exit_idx]
    entry_date = bars.dates[i0]
    pct = avg_exit / fill - 1.0 - cost_pct

    spy = _bench_return(
        _maybe_bars(prices_df, BENCHMARK), entry_date, exit_date, reason == "sma20_exit"
    )
    etf = config.tracking.sector_etfs.get(rec.sector) if rec.sector else None
    sec = _bench_return(
        _maybe_bars(prices_df, etf), entry_date, exit_date, reason == "sma20_exit"
    ) if etf else None

    return TrackResult(
        status=status, entry_date=entry_date, entry_fill=fill, exit_date=exit_date,
        avg_exit_price=avg_exit, exit_reason=reason, r_multiple=r_of(avg_exit),
        pct_return=pct, days_held=exit_idx - i0, mae_r=mae, mfe_r=mfe,
        spy_return=spy, sector_etf_return=sec,
        excess_vs_spy=None if spy is None else pct - spy,
        excess_vs_sector=None if sec is None else pct - sec,
        is_final=True,
    )


def _maybe_bars(prices_df: Optional[pd.DataFrame], ticker: Optional[str]) -> Optional[_Bars]:
    if prices_df is None or ticker is None or "ticker" not in prices_df.columns:
        return None
    sub = prices_df[prices_df["ticker"] == ticker]
    return _Bars(sub) if len(sub) else None


def _open_result(
    bars: _Bars, i0: int, fill: float, current_r: float, mae: float, mfe: float
) -> TrackResult:
    """TrackResult for a filled, unfinished trade. current_r rides in r_multiple
    (documented in docs/status/BOT_5.md); is_final stays False."""
    return TrackResult(
        status="open", entry_date=bars.dates[i0], entry_fill=fill,
        r_multiple=current_r, mae_r=mae, mfe_r=mfe, is_final=False,
    )


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

_JSON_FIELDS = ("modules", "guardrail_reasons", "valuation_info", "details", "llm_brief")


def row_to_rec(row: sqlite3.Row | dict) -> Recommendation:
    """Build a Recommendation from a `recommendations` table row."""
    d = dict(row)
    for f in _JSON_FIELDS:
        v = d.get(f)
        if isinstance(v, str) and v != "":
            try:
                d[f] = json.loads(v)
            except json.JSONDecodeError:
                d[f] = None
        elif v == "":
            d[f] = None
    d["modules"] = d.get("modules") or []
    d["guardrail_reasons"] = d.get("guardrail_reasons") or []
    d["valuation_info"] = d.get("valuation_info") or {}
    d["details"] = d.get("details") or {}
    for f in ("signal_date", "valid_until", "earnings_date"):
        d[f] = _to_date(d[f]) if d.get(f) else None
    d["in_report"] = bool(d.get("in_report"))
    d["earnings_in_window"] = bool(d.get("earnings_in_window"))
    for f in ("setup_score", "rs_pct", "overlap_score", "track_score", "total_score"):
        d[f] = d[f] if d.get(f) is not None else 0.0
    d["last_updated"] = d.get("last_updated") or ""
    fields = Recommendation.__dataclass_fields__
    return Recommendation(**{k: d.get(k) for k in fields})


class PriceCache:
    """Loads each ticker's price history from the DB once per update run."""

    def __init__(self, conn: sqlite3.Connection, start: date, end: date):
        self.conn, self.start, self.end = conn, start, end
        self._frames: dict[str, pd.DataFrame] = {}

    def get(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._frames:
            df = pd.read_sql_query(
                "SELECT date, open, high, low, close FROM prices "
                "WHERE ticker = ? AND date >= ? AND date <= ? ORDER BY date",
                self.conn, params=(ticker, self.start.isoformat(), self.end.isoformat()),
            )
            df["ticker"] = ticker
            self._frames[ticker] = df
        return self._frames[ticker]

    def frame_for(self, tickers: list[str], signal_date: date) -> pd.DataFrame:
        lo = (signal_date - timedelta(days=_WARMUP_CALENDAR_DAYS)).isoformat()
        parts = []
        for t in dict.fromkeys(tickers):
            f = self.get(t)
            if len(f):
                parts.append(f[f["date"] >= lo])
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "ticker"]
        )


def _load_splits(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, ratio FROM splits WHERE ticker = ? ORDER BY date", conn, params=(ticker,)
    )


def _write_result(conn: sqlite3.Connection, rec_id: str, res: TrackResult) -> None:
    # INSERT OR IGNORE: a final result is written once and never modified.
    conn.execute(
        "INSERT OR IGNORE INTO recommendation_results (rec_id, final_status, entry_date, "
        "entry_fill, exit_date, avg_exit_price, exit_reason, r_multiple, pct_return, "
        "days_held, mae_r, mfe_r, spy_return, sector_etf_return, excess_vs_spy, "
        "excess_vs_sector) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec_id, res.status,
            res.entry_date.isoformat() if res.entry_date else None, res.entry_fill,
            res.exit_date.isoformat() if res.exit_date else None, res.avg_exit_price,
            res.exit_reason, res.r_multiple, res.pct_return, res.days_held, res.mae_r,
            res.mfe_r, res.spy_return, res.sector_etf_return, res.excess_vs_spy,
            res.excess_vs_sector,
        ),
    )


def update_all(
    conn: sqlite3.Connection, as_of: date, source: str, config: Optional[Config] = None
) -> dict[str, int]:
    """Re-simulate every non-final recommendation and baseline of ``source`` on
    prices up to ``as_of`` (point-in-time), update status/current_r/last_updated and
    write each newly final result exactly once. Idempotent.

    Split adjustment of stored entry/stop/target is applied for ``source == "live"``
    only: backtest recommendations are struck from the same (already adjusted)
    research.db history they are simulated on. Returns counts for logging."""
    config = config or load_config()
    conn.row_factory = sqlite3.Row
    as_of_s = as_of.isoformat()

    rows = conn.execute(
        "SELECT r.* FROM recommendations r "
        "LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = ? AND r.signal_date <= ? AND x.rec_id IS NULL "
        "AND COALESCE(r.status, '') NOT IN ('stopped','target_hit','time_stop','expired') "
        "ORDER BY r.signal_date, r.id",
        (source, as_of_s),
    ).fetchall()
    counts = {"simulated": 0, "finalised": 0, "baselines": 0, "baselines_finalised": 0}
    if rows:
        first = min(_to_date(r["signal_date"]) for r in rows)
        cache = PriceCache(conn, first - timedelta(days=_WARMUP_CALENDAR_DAYS), as_of)
        for row in rows:
            rec = row_to_rec(row)
            etf = config.tracking.sector_etfs.get(rec.sector) if rec.sector else None
            frame = cache.frame_for([rec.ticker, BENCHMARK] + ([etf] if etf else []), rec.signal_date)
            splits = _load_splits(conn, rec.ticker) if source == "live" else None
            res = simulate(rec, frame, splits, config)
            counts["simulated"] += 1
            status = res.status
            current_r = res.r_multiple if res.status == "open" else None
            conn.execute(
                "UPDATE recommendations SET status = ?, current_r = ?, last_updated = ? WHERE id = ?",
                (status, current_r, as_of_s, rec.id),
            )
            if res.is_final:
                _write_result(conn, rec.id, res)
                counts["finalised"] += 1
        conn.commit()

    from src.baseline import update_baselines  # local import: baseline imports tracker

    b = update_baselines(conn, as_of, source, config)
    counts["baselines"], counts["baselines_finalised"] = b
    log.info("update_all(%s, %s): %s", source, as_of_s, counts)
    return counts
