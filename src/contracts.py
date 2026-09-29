"""Shared data contracts.

- Dataclasses below are the objects that flow between modules; every bot reads/writes
  these shapes rather than inventing ad-hoc dicts.
- `DataAccess` (thin SQLite reads only, no business logic) lives in src/data_access.py
  and is shared by every bot: point it at either data/run.db or data/research.db.
- `StrategyModule` is the interface every module in src/modules/ implements.
- Function stubs at the bottom (grouped by owning bot) are signatures only —
  docstrings describing the contract. The owning bot implements the body in its own
  file; do not implement business logic here.

See docs/CONTRACTS.md for narrative documentation and docs/OWNERSHIP.md for who owns
what.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from src.data_access import DataAccess


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One module's raw signal for one ticker on one day, before ranking."""

    ticker: str
    module: str
    signal_date: date
    entry: float
    stop: float
    setup_score: float  # 0-100, module-defined
    rationale: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardrailResult:
    status: str  # "pass" | "fail" | "unknown"
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    valuation_info: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackResult:
    """Output of tracker.simulate() for one recommendation as of some as_of date."""

    status: str  # "pending" | "open" | "stopped" | "target_hit" | "time_stop" | "expired"
    entry_date: Optional[date] = None
    entry_fill: Optional[float] = None
    exit_date: Optional[date] = None
    avg_exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    r_multiple: Optional[float] = None
    pct_return: Optional[float] = None
    days_held: Optional[int] = None
    mae_r: Optional[float] = None
    mfe_r: Optional[float] = None
    spy_return: Optional[float] = None
    sector_etf_return: Optional[float] = None
    excess_vs_spy: Optional[float] = None
    excess_vs_sector: Optional[float] = None
    is_final: bool = False


@dataclass
class LLMBrief:
    ticker: str
    summary: str
    upcoming_catalysts: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    recent_positive_events: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass
class Recommendation:
    """Mirrors the `recommendations` table (src/db.py). JSON-typed columns
    (modules, guardrail_reasons, valuation_info, details, llm_brief) are plain
    Python objects here; state_io/db layers handle (de)serialization."""

    id: str  # "YYYY-MM-DD_TICKER"
    source: str  # "live" or a backtest run_id
    signal_date: date
    ticker: str
    modules: list[str]
    primary_module: str
    setup_score: float
    rs_pct: float
    overlap_score: float
    track_score: float
    total_score: float
    rank: Optional[int]
    in_report: bool
    entry: float
    stop: float
    target: float
    valid_until: date
    regime: str
    sector: Optional[str]
    industry: Optional[str]
    earnings_date: Optional[date]
    earnings_in_window: bool
    guardrail_status: str
    guardrail_reasons: list[str]
    valuation_info: dict[str, Any]
    rationale: str
    details: dict[str, Any]
    llm_brief: Optional[dict[str, Any]]
    status: str
    current_r: Optional[float]
    last_updated: str


# ---------------------------------------------------------------------------
# Strategy module interface
# ---------------------------------------------------------------------------


class StrategyModule(Protocol):
    name: str

    def scan(self, as_of: date, data: "DataAccess") -> list[Candidate]:
        """Return candidates signaling as of `as_of` (point-in-time: must not use
        any information dated after as_of)."""
        ...

    def scan_history(
        self, start: date, end: date, data: "DataAccess"
    ) -> list[Candidate]:
        """Vectorized backtest scan over [start, end]. Must return results
        identical to calling scan() once per day in that range."""
        ...


# ---------------------------------------------------------------------------
# Cross-bot function signatures (docstrings only -- implemented by the owning bot)
# ---------------------------------------------------------------------------

# --- Bot 1: src/data/prices.py, src/universe.py, src/regime.py, src/data/earnings.py ---


def _bot1_update_prices(conn, tickers: list[str], start: date, end: date) -> dict:
    """update_prices(conn, tickers, start, end) -> report dict.

    Downloads OHLCV via yfinance for each ticker in [start, end], retries failures
    once after config.prices.retry_pause_seconds, then falls back to the Nasdaq
    historical API for tickers still missing (flag price_source="nasdaq"). Upserts
    into `prices`. Returns
    {"successes": [...], "retried": [...], "nasdaq_filled": [...], "missing": [...]}.
    """
    raise NotImplementedError("owned by Bot 1: src/data/prices.py")


def _bot1_update_splits(conn, tickers: list[str]) -> None:
    """update_splits(conn, tickers) -> None. Pulls split actions from yfinance and
    upserts into `splits`."""
    raise NotImplementedError("owned by Bot 1: src/data/prices.py")


def _bot1_refresh_universe(conn, as_of: date) -> None:
    """refresh_universe(conn, as_of) -> None. Applies docs/PLAN.md section 5 filters
    using Nasdaq screener data (SEC company_tickers_exchange.json fallback) and
    writes one row per ticker into universe_snapshots for as_of."""
    raise NotImplementedError("owned by Bot 1: src/universe.py")


def _bot1_build_historical_universe(data: DataAccess, as_of: date) -> pd.DataFrame:
    """build_historical_universe(data, as_of) -> DataFrame. Reconstructs the point-
    in-time universe for backtesting from research.db history."""
    raise NotImplementedError("owned by Bot 1: src/universe.py")


def _bot1_compute_regime(conn, as_of: date) -> dict:
    """compute_regime(conn, as_of) -> dict. Computes SPY-vs-200SMA and breadth per
    docs/PLAN.md section 6, upserts into regime_log, returns the row."""
    raise NotImplementedError("owned by Bot 1: src/regime.py")


def _bot1_update_upcoming_earnings(conn, as_of: date, days_ahead: int) -> None:
    """update_upcoming_earnings(conn, as_of, days_ahead) -> None. Pulls the Nasdaq
    earnings calendar for [as_of, as_of + days_ahead] and upserts into
    earnings_dates with source="nasdaq_calendar"."""
    raise NotImplementedError("owned by Bot 1: src/data/earnings.py")


# --- Bot 2: src/data/fundamentals.py, src/guardrail.py, src/valuation.py ---


def _bot2_fetch_company(conn, cik: str) -> None:
    """fetch_company(conn, cik) -> None. Fetches companyfacts for one CIK on
    demand and upserts rows into `fundamentals`."""
    raise NotImplementedError("owned by Bot 2: src/data/fundamentals.py")


def _bot2_backfill_bulk(conn) -> None:
    """backfill_bulk(conn) -> None. Local-only: loads companyfacts.zip bulk file
    into `fundamentals`. Never called from the cloud routine."""
    raise NotImplementedError("owned by Bot 2: src/data/fundamentals.py")


def _bot2_update_summary(conn, as_of: date) -> None:
    """update_summary(conn, as_of) -> None. Weekly job: pulls the XBRL frames API
    for the universe and writes `fundamentals_summary` rows for as_of."""
    raise NotImplementedError("owned by Bot 2: src/data/fundamentals.py")


def _bot2_guardrail_evaluate(ticker: str, as_of: date, data: DataAccess) -> GuardrailResult:
    """evaluate(ticker, as_of, data) -> GuardrailResult. Implements docs/PLAN.md
    section 8."""
    raise NotImplementedError("owned by Bot 2: src/guardrail.py")


# --- Bot 3: src/data/edgar_daily.py, edgar_bulk.py, form4.py, texts.py, news.py ---


def _bot3_process_day(conn, date_: date) -> None:
    """process_day(conn, date_) -> None. Walks that day's EDGAR daily index for
    Form 4 and 8-K filings belonging to universe CIKs, parses Form 4 XML into
    insider_trades, records 8-K items, and derives earnings events from Item 2.02
    acceptance timestamps."""
    raise NotImplementedError("owned by Bot 3: src/data/edgar_daily.py")


def _bot3_backfill(conn, start_year: int) -> None:
    """backfill(conn, start_year) -> None. Local-only: loads EDGAR bulk
    submissions/insider datasets from start_year onward."""
    raise NotImplementedError("owned by Bot 3: src/data/edgar_bulk.py")


def _bot3_get_8k_texts(conn, ticker: str, as_of: date, days: int = 30) -> list[dict]:
    """get_8k_texts(conn, ticker, as_of, days=30) -> list of {accession, items,
    filed_date, text} for 8-Ks filed in [as_of - days, as_of]."""
    raise NotImplementedError("owned by Bot 3: src/data/texts.py")


def _bot3_get_news(ticker: str, company_name: str, as_of: date, days: int = 14) -> list[dict]:
    """get_news(ticker, company_name, as_of, days=14) -> list of {title, link,
    published, source} from Google News RSS."""
    raise NotImplementedError("owned by Bot 3: src/data/news.py")


# --- Bot 4: src/modules/*, src/ranking.py, src/trade_plan.py ---


def _bot4_get_enabled_modules(config) -> list[StrategyModule]:
    """get_enabled_modules(config) -> list[StrategyModule]. Instantiates the
    modules whose config.modules.<name>.enabled is true."""
    raise NotImplementedError("owned by Bot 4: src/modules/__init__.py")


def _bot4_rank(
    candidates: list[Candidate], as_of: date, data: DataAccess, conn
) -> list[Recommendation]:
    """rank(candidates, as_of, data, conn) -> list[Recommendation]. Implements
    docs/PLAN.md section 9 (percentile scoring, dedup by ticker+day, industry cap
    applied at report time not here)."""
    raise NotImplementedError("owned by Bot 4: src/ranking.py")


def _bot4_trade_plan_build(candidate: Candidate, data: DataAccess, config) -> dict:
    """build(candidate, data, config) -> dict with entry/stop/target/valid_until per
    docs/PLAN.md section 10."""
    raise NotImplementedError("owned by Bot 4: src/trade_plan.py")


# --- Bot 5: src/tracker.py, src/baseline.py, src/stats.py, backtest/* ---


def _bot5_simulate(rec: Recommendation, prices_df: pd.DataFrame, splits_df: pd.DataFrame, config) -> TrackResult:
    """simulate(rec, prices_df, splits_df, config) -> TrackResult. Pure function,
    no DB/network access: implements docs/PLAN.md section 11 exactly, must be
    callable identically from the live pipeline and from backtest/runner.py."""
    raise NotImplementedError("owned by Bot 5: src/tracker.py")


def _bot5_update_all(conn, as_of: date, source: str) -> None:
    """update_all(conn, as_of, source) -> None. Re-simulates every non-final
    recommendation with source=source using fresh prices, writes
    recommendation_results for newly-final ones."""
    raise NotImplementedError("owned by Bot 5: src/tracker.py")


def _bot5_create_for(conn, recs: list[Recommendation], as_of: date) -> None:
    """create_for(conn, recs, as_of) -> None. Draws config.baseline.samples_per_recommendation
    seeded random tickers per rec from that day's universe and inserts baselines rows."""
    raise NotImplementedError("owned by Bot 5: src/baseline.py")


def _bot5_stats_summary(conn, source: str) -> dict:
    """summary(conn, source) -> dict. Implements docs/PLAN.md section 14 grouped
    statistics."""
    raise NotImplementedError("owned by Bot 5: src/stats.py")


def _bot5_backtest_run(start: date, end: date, notes: str) -> str:
    """run(start, end, notes) -> run_id. Executes the full pipeline against
    research.db over [start, end] using point-in-time data only, writes a
    backtest_runs row and recommendations/results with source=run_id."""
    raise NotImplementedError("owned by Bot 5: backtest/runner.py")


# --- Bot 6: src/llm/*, src/report.py, notify/telegram.py ---


def _bot6_write_inputs(conn, recs: list[Recommendation], as_of: date, out_dir) -> None:
    """write_inputs(conn, recs, as_of, out_dir) -> None. Writes one input file per
    stock (filtered 8-K items + Exhibit 99.1 text + 14 days of news, capped per
    config.llm.max_input_tokens_per_stock) for the routine agent to read."""
    raise NotImplementedError("owned by Bot 6: src/llm/brief_io.py")


def _bot6_merge_outputs(conn, in_dir) -> dict:
    """merge_outputs(conn, in_dir) -> merge report dict. Validates each JSON brief
    against LLMBrief, merges valid ones into recommendations.llm_brief."""
    raise NotImplementedError("owned by Bot 6: src/llm/brief_io.py")


def _bot6_report_build(conn, as_of: date) -> tuple[str, str]:
    """build(conn, as_of) -> (html_path, md_path). Implements docs/PLAN.md
    section 12."""
    raise NotImplementedError("owned by Bot 6: src/report.py")


def _bot6_telegram_send_report(html_path: str, summary_text: str) -> None:
    """send_report(html_path, summary_text) -> None. No-op (logs and returns) if
    Telegram secrets are not configured."""
    raise NotImplementedError("owned by Bot 6: notify/telegram.py")


def _bot6_telegram_send_alert(text: str) -> None:
    """send_alert(text) -> None. Same no-op-if-unconfigured behavior."""
    raise NotImplementedError("owned by Bot 6: notify/telegram.py")


# --- Bot 7: run_daily.py, run_weekly.py, run_backfill.py ---

# These are entrypoint scripts, not importable functions; see docs/OWNERSHIP.md.
