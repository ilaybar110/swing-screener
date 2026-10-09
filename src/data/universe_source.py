"""Universe membership source data: Nasdaq's stock screener API, with SEC's
company_tickers_exchange.json as a fallback when Nasdaq is unreachable
(docs/PLAN.md section 4.3).

Exposes a normalized DataFrame (columns: ticker, name, exchange, sector, industry,
sic, market_cap, price) regardless of source, plus `upsert_tickers` to persist the
CIK/sector/industry/sector_etf mapping into the shared `tickers` table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.data import raw_cache
from src.data.edgar_client import EdgarClient
from src.utils.http import get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

_SCREENER_COLUMNS = ["ticker", "name", "exchange", "sector", "industry", "sic", "market_cap", "price"]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if text in ("", "N/A", "n/a"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_nasdaq_screener(raw_cache_dir: Path) -> pd.DataFrame:
    """Fetch the full US stock screener from Nasdaq (tableonly=true&download=true
    returns every row in one response, no pagination needed)."""
    session = get_session(browser_like=True)
    resp = session.get(
        NASDAQ_SCREENER_URL,
        params={"tableonly": "true", "download": "true"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    raw_cache.put(raw_cache_dir, NASDAQ_SCREENER_URL, payload)

    rows = ((payload or {}).get("data") or {}).get("rows") or []
    records = []
    for row in rows:
        ticker = (row.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        records.append({
            "ticker": ticker,
            "name": row.get("name"),
            "exchange": row.get("exchange"),
            "sector": row.get("sector") or None,
            "industry": row.get("industry") or None,
            "sic": None,
            "market_cap": _to_float(row.get("marketCap")),
            "price": _to_float(row.get("lastsale")),
        })
    return pd.DataFrame(records, columns=_SCREENER_COLUMNS)


def fetch_sec_fallback(edgar_client: EdgarClient) -> pd.DataFrame:
    """Fallback source when Nasdaq's screener is unreachable. SEC's
    company_tickers_exchange.json has no sector/industry/market-cap data -- those
    columns come back None so refresh_universe can still apply the filters it can
    (price/history from our own price DB), and callers know CIK/name/exchange are
    the only reliable fields from this path."""
    payload = edgar_client.get_json(SEC_TICKERS_EXCHANGE_URL)
    fields = payload.get("fields", ["cik", "name", "ticker", "exchange"])
    data = payload.get("data", [])
    records = []
    for entry in data:
        row = dict(zip(fields, entry))
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        cik = row.get("cik")
        records.append({
            "ticker": ticker,
            "name": row.get("name"),
            "exchange": row.get("exchange"),
            "sector": None,
            "industry": None,
            "sic": None,
            "market_cap": None,
            "price": None,
            "cik": str(cik).zfill(10) if cik is not None else None,
        })
    return pd.DataFrame(records)


def get_universe_source(
    raw_cache_dir: Path, edgar_client: EdgarClient
) -> tuple[pd.DataFrame, str]:
    """Try the Nasdaq screener first; fall back to SEC's ticker/exchange file if
    that request fails for any reason. Returns (dataframe, source_name)."""
    try:
        df = fetch_nasdaq_screener(raw_cache_dir)
        if df.empty:
            raise ValueError("Nasdaq screener returned no rows")
        return df, "nasdaq"
    except Exception as exc:  # noqa: BLE001 -- any failure falls back to SEC
        log.warning("Nasdaq screener fetch failed, falling back to SEC: %s", exc)
        df = fetch_sec_fallback(edgar_client)
        return df, "sec"


# Nasdaq's screener uses its own sector labels; the rest of the system (sector-ETF map in
# config.tracking.sector_etfs, the guardrail's Financials exception) uses GICS-style names.
NASDAQ_SECTOR_ALIASES = {
    "Finance": "Financials",
    "Basic Materials": "Materials",
    "Telecommunications": "Communication Services",
}


def normalize_sector(sector: Any) -> Optional[str]:
    """Map a Nasdaq screener sector label to the config/GICS spelling (None if blank/NaN)."""
    if sector is None or (isinstance(sector, float) and sector != sector):
        return None
    s = str(sector).strip()
    return NASDAQ_SECTOR_ALIASES.get(s, s) or None


def _lookup_cik(cik_map: dict[str, str], ticker: str) -> Optional[str]:
    """SEC spells share classes with a dash ('BRK-B'); Nasdaq uses '/' (and sometimes '.')."""
    for key in (ticker, ticker.replace("/", "-"), ticker.replace(".", "-")):
        if key in cik_map:
            return cik_map[key]
    return None


def upsert_tickers(
    conn: sqlite3.Connection, df: pd.DataFrame, edgar_client: EdgarClient
) -> dict[str, Any]:
    """Upsert ticker -> CIK/name/exchange/sector/industry/sector_etf into `tickers`.

    CIK comes from df["cik"] when present (SEC fallback path already has it),
    otherwise from edgar_client.get_cik_map(). Sector -> sector_etf mapping comes
    from config.tracking.sector_etfs.
    """
    from datetime import date

    from src.config import load_config

    cfg = load_config()
    sector_etf_map = cfg.tracking.sector_etfs

    cik_map: dict[str, str] = {}
    if "cik" not in df.columns:
        cik_map = edgar_client.get_cik_map()

    now = date.today().isoformat()
    rows = []
    for _, r in df.iterrows():
        ticker = str(r["ticker"]).upper()
        cik = r["cik"] if "cik" in df.columns and pd.notna(r.get("cik")) else _lookup_cik(cik_map, ticker)
        sector = normalize_sector(r.get("sector"))
        sector_etf = sector_etf_map.get(sector) if sector else None
        rows.append((
            ticker,
            cik,
            r.get("name"),
            r.get("exchange"),
            sector,
            r.get("industry") or None,
            r.get("sic") or None,
            sector_etf,
            1,
            now,
        ))

    conn.executemany(
        "INSERT INTO tickers (ticker, cik, name, exchange, sector, industry, sic, "
        "sector_etf, is_active, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker) DO UPDATE SET cik=excluded.cik, name=excluded.name, "
        "exchange=excluded.exchange, sector=excluded.sector, industry=excluded.industry, "
        "sic=excluded.sic, sector_etf=excluded.sector_etf, is_active=excluded.is_active, "
        "updated_at=excluded.updated_at",
        rows,
    )
    conn.commit()
    log.info("upsert_tickers: upserted %d rows", len(rows))
    return {"count": len(rows)}
