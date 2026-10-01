"""Daily prices and splits (docs/PLAN.md sections 4.1 and 4.2).

``update_prices`` downloads split-adjusted OHLC plus a separate ``adj_close`` via
yfinance (``auto_adjust=False``), retries failed tickers once after a pause, then
fills still-missing tickers from Nasdaq's historical API (flagged
``price_source="nasdaq"``). Stooq is deliberately not used (blocked in the cloud).

``update_splits`` pulls split actions from yfinance and upserts into ``splits``.

Everything is idempotent: rows are upserted by (ticker, date).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from src.config import load_config
from src.utils.http import get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

NASDAQ_HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"

_PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------


def yahoo_symbol(ticker: str) -> str:
    """Nasdaq spells share classes 'BRK/B'; Yahoo wants 'BRK-B'."""
    return ticker.replace("/", "-").replace(".", "-")


def _yf_download(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Thin wrapper around yfinance.download (patched in tests)."""
    import yfinance as yf

    return yf.download(
        tickers=tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),  # yfinance `end` is exclusive
        auto_adjust=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )


def _frame_for(raw: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame:
    """Extract one ticker's OHLCV frame from a yfinance download result."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = raw.columns.get_level_values(0)
        if ticker in level0:
            df = raw[ticker]
        else:
            # newer yfinance layouts can put the ticker on the second level
            level1 = raw.columns.get_level_values(1)
            if ticker not in level1:
                return pd.DataFrame()
            df = raw.xs(ticker, axis=1, level=1)
    elif single:
        df = raw
    else:
        return pd.DataFrame()
    df = df.dropna(subset=["Close"]) if "Close" in df.columns else pd.DataFrame()
    return df


def _rows_from_yf(df: pd.DataFrame, ticker: str, source: str = "yfinance") -> list[tuple]:
    rows: list[tuple] = []
    if df.empty:
        return rows
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    cols = {c: i for i, c in enumerate(df.columns)}
    values = df.to_numpy()

    def get(i: int, name: str) -> Optional[float]:
        j = cols.get(name)
        if j is None:
            return None
        v = values[i][j]
        return None if pd.isna(v) else float(v)

    for i, ts in enumerate(idx):
        close = get(i, "Close")
        if close is None:
            continue
        adj = get(i, "Adj Close")
        vol = get(i, "Volume")
        rows.append((
            ticker, ts.date().isoformat(), get(i, "Open"), get(i, "High"), get(i, "Low"),
            close, adj if adj is not None else close, int(vol) if vol is not None else None, source,
        ))
    return rows


# ---------------------------------------------------------------------------
# Nasdaq fallback
# ---------------------------------------------------------------------------


def _to_float(text: Any) -> Optional[float]:
    if text is None:
        return None
    s = str(text).replace("$", "").replace(",", "").strip()
    if s in ("", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _nasdaq_history(ticker: str, start: date, end: date) -> list[tuple]:
    """Fetch daily bars for one ticker from Nasdaq's historical API."""
    session = get_session(browser_like=True)
    resp = session.get(
        NASDAQ_HISTORICAL_URL.format(symbol=ticker),
        params={
            "assetclass": "stocks",
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": "9999",
        },
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    out: list[tuple] = []
    for r in rows:
        try:
            m, d, y = str(r["date"]).split("/")
            day = date(int(y), int(m), int(d))
        except (KeyError, ValueError):
            continue
        close = _to_float(r.get("close"))
        if close is None:
            continue
        volume = _to_float(r.get("volume"))
        out.append((
            ticker, day.isoformat(), _to_float(r.get("open")), _to_float(r.get("high")),
            _to_float(r.get("low")), close, close, int(volume) if volume is not None else None,
            "nasdaq",
        ))
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def _upsert_prices(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO prices (ticker, date, open, high, low, close, adj_close, "
        "volume, price_source) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def _download_chunk(tickers: list[str], start: date, end: date) -> tuple[dict[str, list[tuple]], list[str]]:
    """Download one chunk; returns (rows by ticker, tickers with no data / failed)."""
    got: dict[str, list[tuple]] = {}
    ysym = {t: yahoo_symbol(t) for t in tickers}  # rows are stored under our own symbol
    try:
        raw = _yf_download(sorted(set(ysym.values())), start, end)
    except Exception as exc:  # noqa: BLE001 - a failed chunk is retried per ticker
        log.warning("yfinance chunk of %d tickers failed: %s", len(tickers), exc)
        return got, list(tickers)
    for t in tickers:
        rows = _rows_from_yf(_frame_for(raw, ysym[t], single=len(set(ysym.values())) == 1), t)
        if rows:
            got[t] = rows
    return got, [t for t in tickers if t not in got]


def update_prices(
    conn: sqlite3.Connection,
    tickers: list[str],
    start: date,
    end: date,
    *,
    chunk_size: Optional[int] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Download [start, end] for every ticker and upsert into ``prices``.

    Returns {"successes": [...], "retried": [...], "nasdaq_filled": [...],
    "missing": [...]} -- ``retried`` are tickers recovered by the retry pass.
    ``progress(done, total)`` is called after each chunk.
    """
    cfg = load_config()
    chunk = chunk_size or cfg.prices.chunk_size
    pause = cfg.prices.retry_pause_seconds
    tickers = sorted(set(t.upper() for t in tickers))

    successes: list[str] = []
    failed: list[str] = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        got, bad = _download_chunk(part, start, end)
        for t, rows in got.items():
            _upsert_prices(conn, rows)
            successes.append(t)
        failed.extend(bad)
        if progress:
            progress(min(i + chunk, len(tickers)), len(tickers))

    retried: list[str] = []
    still: list[str] = []
    if failed:
        log.info("retrying %d ticker(s) after %ss pause", len(failed), pause)
        sleep(pause)
        for i in range(0, len(failed), chunk):
            got, bad = _download_chunk(failed[i:i + chunk], start, end)
            for t, rows in got.items():
                _upsert_prices(conn, rows)
                retried.append(t)
            still.extend(bad)

    nasdaq_filled: list[str] = []
    missing: list[str] = []
    for t in still:
        try:
            rows = _nasdaq_history(t, start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("nasdaq fallback failed for %s: %s", t, exc)
            rows = []
        if rows:
            _upsert_prices(conn, rows)
            nasdaq_filled.append(t)
        else:
            missing.append(t)

    log.info(
        "update_prices: %d requested, %d ok, %d retried, %d nasdaq, %d missing",
        len(tickers), len(successes), len(retried), len(nasdaq_filled), len(missing),
    )
    return {
        "successes": sorted(successes),
        "retried": sorted(retried),
        "nasdaq_filled": sorted(nasdaq_filled),
        "missing": sorted(missing),
    }


def _yf_splits(ticker: str) -> pd.Series:
    import yfinance as yf

    return yf.Ticker(ticker).splits


def update_splits(conn: sqlite3.Connection, tickers: Iterable[str]) -> int:
    """Pull split actions from yfinance and upsert into ``splits``. A ticker whose
    lookup fails is logged and skipped. Returns the number of split rows written."""
    written = 0
    for t in sorted(set(x.upper() for x in tickers)):
        try:
            series = _yf_splits(yahoo_symbol(t))
        except Exception as exc:  # noqa: BLE001
            log.warning("split lookup failed for %s: %s", t, exc)
            continue
        if series is None or len(series) == 0:
            continue
        idx = pd.DatetimeIndex(series.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        rows = [
            (t, ts.date().isoformat(), float(v))
            for ts, v in zip(idx, series.to_numpy())
            if v and not pd.isna(v) and float(v) > 0
        ]
        conn.executemany("INSERT OR REPLACE INTO splits (ticker, date, ratio) VALUES (?,?,?)", rows)
        written += len(rows)
    conn.commit()
    return written
