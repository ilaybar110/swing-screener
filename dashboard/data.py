"""Data access for the local dashboard (no Streamlit imports, so it is unit-testable).

Live data comes from the git-committed ``state/`` CSVs, loaded through
``state_io.import_state`` into an in-memory SQLite DB. Backtest runs are read from
``data/research.db`` (read-only) when it exists. Neither path writes anything.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from src.config import data_root, load_config
from src.db import init_db
from src.state_io import import_state

LIVE = "live"


def state_dir() -> Path:
    cfg = load_config()
    p = Path(cfg.paths.state_dir)
    return p if p.is_absolute() else data_root() / p


def research_db_path() -> Path:
    cfg = load_config()
    p = Path(cfg.paths.research_db)
    return p if p.is_absolute() else data_root() / p


def connect_live(directory: Optional[Path] = None) -> sqlite3.Connection:
    """Fresh in-memory DB populated from the state/ CSVs."""
    conn = init_db(":memory:")
    import_state(conn, directory or state_dir())
    return conn


def connect_research(path: Optional[Path] = None) -> Optional[sqlite3.Connection]:
    """Read-only connection to research.db, or None when it does not exist."""
    path = path or research_db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_backtest_runs(conn: Optional[sqlite3.Connection]) -> pd.DataFrame:
    """Backtest runs newest first (empty frame without a research DB)."""
    cols = ["run_id", "created_at", "start_date", "end_date", "config_hash", "notes"]
    if conn is None:
        return pd.DataFrame(columns=cols)
    try:
        return pd.read_sql_query(
            "SELECT run_id, created_at, start_date, end_date, config_hash, notes "
            "FROM backtest_runs ORDER BY created_at DESC", conn)
    except sqlite3.OperationalError:
        return pd.DataFrame(columns=cols)


def source_options(research: Optional[sqlite3.Connection]) -> dict[str, str]:
    """{label: source id}; "Live" first, then each backtest run."""
    options = {"Live": LIVE}
    for r in list_backtest_runs(research).itertuples():
        note = f" - {r.notes}" if r.notes else ""
        options[f"Backtest {r.run_id} ({r.start_date}..{r.end_date}){note}"] = r.run_id
    return options


def _loads(v: Any) -> Any:
    if isinstance(v, str) and v:
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return None


def recommendations_table(conn: sqlite3.Connection, source: str) -> pd.DataFrame:
    """Every recommendation of a source joined with its final result (if any).
    JSON columns are decoded; ``modules_str`` is a display-friendly module list."""
    df = pd.read_sql_query(
        "SELECT r.*, x.final_status, x.entry_date, x.entry_fill, x.exit_date, "
        "x.avg_exit_price, x.exit_reason, x.r_multiple, x.pct_return, x.days_held, "
        "x.mae_r, x.mfe_r, x.spy_return, x.sector_etf_return, x.excess_vs_spy, "
        "x.excess_vs_sector FROM recommendations r "
        "LEFT JOIN recommendation_results x ON x.rec_id = r.id "
        "WHERE r.source = ? ORDER BY r.signal_date DESC, r.rank", conn, params=(source,))
    if df.empty:
        df["modules_str"] = pd.Series(dtype=str)
        return df
    for col in ("modules", "guardrail_reasons", "valuation_info", "details", "llm_brief"):
        df[col] = df[col].map(_loads)
    df["modules_str"] = df["modules"].map(lambda m: ", ".join(m) if isinstance(m, list) else "")
    # one number for "how is it doing": final R when closed, else the live mark
    df["result_r"] = df["r_multiple"].where(df["r_multiple"].notna(), df["current_r"])
    return df


def regime_history(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT date, spy_close, spy_sma200, breadth_pct, regime FROM regime_log ORDER BY date", conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def regime_runs(hist: pd.DataFrame) -> pd.DataFrame:
    """Collapse the daily regime log into contiguous (regime, from, to, days) spans."""
    if hist.empty:
        return pd.DataFrame(columns=["regime", "from", "to", "days"])
    block = (hist["regime"] != hist["regime"].shift()).cumsum()
    g = hist.groupby(block)
    return pd.DataFrame({
        "regime": g["regime"].first(), "from": g["date"].first().dt.date,
        "to": g["date"].last().dt.date, "days": g.size(),
    }).reset_index(drop=True)


def brief_sections(llm_brief: Any) -> dict[str, Any]:
    """Normalise a stored LLM briefing; empty dict when missing or malformed."""
    if not isinstance(llm_brief, dict) or not llm_brief.get("summary"):
        return {}
    return {
        "summary": llm_brief.get("summary", ""),
        "upcoming_catalysts": list(llm_brief.get("upcoming_catalysts") or []),
        "red_flags": list(llm_brief.get("red_flags") or []),
        "recent_positive_events": list(llm_brief.get("recent_positive_events") or []),
        "sources": list(llm_brief.get("sources") or []),
    }


def chart_window(row: pd.Series, today: Optional[date] = None) -> tuple[date, date]:
    """Date range for a recommendation's price chart: 30 days of context before the
    signal until 20 days after the exit (or today when still open)."""
    today = today or date.today()
    start = date.fromisoformat(str(row["signal_date"])[:10]) - timedelta(days=45)
    exit_d = row.get("exit_date")
    end = (date.fromisoformat(str(exit_d)[:10]) + timedelta(days=20)
           if isinstance(exit_d, str) and exit_d else today)
    return start, min(max(end, start + timedelta(days=60)), today + timedelta(days=1))


def fetch_prices(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Split-adjusted OHLC from yfinance, fetched on demand. Raises on failure."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    df = t.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"no price data returned for {ticker}")
    df = df.reset_index().rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df[["date", "open", "high", "low", "close"]]


def split_factor_after(ticker: str, signal_date: date) -> float:
    """Cumulative split ratio of splits dated after signal_date (1.0 if none/unknown)."""
    try:
        import yfinance as yf

        s = yf.Ticker(ticker).splits
        factor = 1.0
        for d, ratio in s.items():
            if pd.Timestamp(d).tz_localize(None).date() > signal_date and ratio:
                factor *= float(ratio)
        return factor
    except Exception:  # noqa: BLE001 - chart levels just stay unadjusted
        return 1.0
