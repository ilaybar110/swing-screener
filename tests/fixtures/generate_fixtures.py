"""Generates the synthetic fixture data every bot's tests run against.

Produces:
  - tests/fixtures/fixture.db   (SQLite, same schema as production)
  - tests/fixtures/state/       (a matching state/ CSV export)

Deterministic: same output every run (fixed RNG seed). Re-run this script whenever
the scenario set changes; commit both fixture.db and fixture/state/ afterward.

See docs/CONTRACTS.md "Fixture tickers" section for exactly which ticker/date
combination is meant to trigger which module, and which are deliberate near-misses
(control group) that must NOT trigger.

Run: python -m tests.fixtures.generate_fixtures
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.db import init_db  # noqa: E402
from src.state_io import export_state  # noqa: E402
from src.utils.calendar import trading_days  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent
DB_PATH = FIXTURES_DIR / "fixture.db"
STATE_DIR = FIXTURES_DIR / "state"

SEED = 424242
START = date(2022, 1, 3)
END = date(2024, 12, 31)

SECTOR_ETFS = {
    "Technology": "XLK", "Financials": "XLF", "Health Care": "XLV",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
SECTORS = list(SECTOR_ETFS.keys())


@dataclass
class TickerSpec:
    ticker: str
    sector: str
    scenario: str  # documented in docs/CONTRACTS.md
    seed_offset: int


# 4 "positive" tickers (one per module), 4 near-miss negatives (one per module,
# each violating exactly one rule), 1 split-during-open-trade ticker, and 21 plain
# random-walk control tickers spread across sectors = 30 synthetic tickers total.
TICKER_SPECS: list[TickerSpec] = [
    TickerSpec("MOMA1", "Technology", "module_a_trigger", 1),
    TickerSpec("MOMB1", "Health Care", "module_b_trigger", 2),
    TickerSpec("MOMC1", "Financials", "module_c_trigger", 3),
    TickerSpec("MOMD1", "Industrials", "module_d_trigger", 4),
    TickerSpec("NEGA1", "Technology", "module_a_near_miss_extended", 5),
    TickerSpec("NEGB1", "Health Care", "module_b_near_miss_small_gap", 6),
    TickerSpec("NEGC1", "Financials", "module_c_near_miss_single_insider", 7),
    TickerSpec("NEGD1", "Industrials", "module_d_near_miss_low_volume", 8),
    TickerSpec("SPLIT1", "Consumer Discretionary", "split_during_open_trade", 9),
]
_remaining_sectors = (SECTORS * 3)[: 30 - len(TICKER_SPECS)]
for i, sector in enumerate(_remaining_sectors):
    TICKER_SPECS.append(TickerSpec(f"CTRL{i+1:02d}", sector, "random_walk_control", 100 + i))

ALL_DATES = trading_days(START, END)
N = len(ALL_DATES)


def _gbm_path(rng: np.random.Generator, n: int, start_price: float, drift: float, vol: float) -> np.ndarray:
    rets = rng.normal(drift, vol, n)
    return start_price * np.exp(np.cumsum(rets))


def _ohlcv_from_close(rng: np.random.Generator, close: np.ndarray, base_volume: float) -> pd.DataFrame:
    n = len(close)
    open_ = np.empty(n)
    open_[0] = close[0] * (1 + rng.normal(0, 0.002))
    open_[1:] = close[:-1] * (1 + rng.normal(0, 0.003, n - 1))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    volume = np.abs(rng.normal(base_volume, base_volume * 0.2, n))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def build_spy_and_sector_etfs(rng: np.random.Generator) -> dict[str, pd.DataFrame]:
    out = {}
    spy_close = _gbm_path(rng, N, 400.0, 0.0003, 0.009)
    out["SPY"] = _ohlcv_from_close(rng, spy_close, 8e7)
    for etf in SECTOR_ETFS.values():
        beta_noise = rng.normal(0, 0.006, N)
        etf_close = spy_close * (1 + np.cumsum(beta_noise) * 0.05)
        out[etf] = _ohlcv_from_close(rng, etf_close, 3e7)
    return out


def _base_random_walk(rng: np.random.Generator, start_price: float = 60.0) -> np.ndarray:
    return _gbm_path(rng, N, start_price, 0.0002, 0.02)


def build_module_a_trigger(rng: np.random.Generator) -> np.ndarray:
    """Strong 6-month leader, pulls back to the 20/50 SMA on light volume, then
    breaks the prior day's high. Engineered breakout at index N-15."""
    close = _base_random_walk(rng, 50.0)
    uptrend = np.linspace(0, 0.9, N)
    close = close * (1 + uptrend)
    # carve a shallow pullback then a breakout in the final 20 sessions
    tail = close[-25:].copy()
    pullback = np.linspace(0, -0.06, 12)
    tail[:12] *= (1 + pullback)
    tail[12:20] = tail[11] * (1 + np.linspace(0, 0.01, 8))
    tail[20:] = tail[19] * (1 + np.linspace(0.01, 0.07, len(tail) - 20))
    close[-25:] = tail
    return close


def build_module_b_trigger(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, date]:
    """Gap up >= 5% with 2x+ volume on the earnings day (index N-20), holds a tight
    range for a few sessions, then breaks out. Returns (close, volume, earnings_date)."""
    close = _base_random_walk(rng, 40.0)
    volume = np.abs(rng.normal(2e6, 3e5, N))
    gap_idx = N - 20
    close[gap_idx:] *= 1.07  # +7% gap, sustained
    volume[gap_idx] *= 2.5
    # tight range for 4 sessions after the gap
    for i in range(gap_idx + 1, gap_idx + 5):
        close[i] = close[gap_idx] * (1 + rng.normal(0, 0.01))
    # breakout above the range afterward
    close[gap_idx + 5:] = close[gap_idx] * (
        1 + np.linspace(0.03, 0.10, N - (gap_idx + 5))
    )
    return close, volume, ALL_DATES[gap_idx]


def build_module_c_trigger(rng: np.random.Generator) -> tuple[np.ndarray, date, date]:
    """Drifts sideways then reclaims/holds the 20-day SMA after a cluster of insider
    buys (two officers, filed within days of each other around index N-30).
    Returns (close, insider_buy_date_1, insider_buy_date_2)."""
    close = _base_random_walk(rng, 30.0)
    cluster_idx = N - 30
    close[cluster_idx:] = close[cluster_idx] * (
        1 + np.linspace(0.0, 0.12, N - cluster_idx)
    )
    return close, ALL_DATES[cluster_idx - 5], ALL_DATES[cluster_idx - 2]


def build_module_d_trigger(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Uptrend into a 5-week contracting base near the 52-week high, then breaks
    out on volume >= 1.5x average. Returns (close, volume)."""
    close = _base_random_walk(rng, 70.0)
    uptrend = np.linspace(0, 0.5, N)
    close = close * (1 + uptrend)
    volume = np.abs(rng.normal(1.5e6, 2e5, N))
    base_start = N - 30
    base_len = 25
    base_level = close[base_start]
    contraction = np.linspace(0.02, 0.005, base_len)
    for i in range(base_len):
        close[base_start + i] = base_level * (1 + rng.normal(0, contraction[i]))
    breakout_idx = base_start + base_len
    close[breakout_idx:] = base_level * (1.03 + np.linspace(0, 0.08, N - breakout_idx))
    volume[breakout_idx] *= 2.0
    return close, volume


def build_negative_a(rng: np.random.Generator) -> np.ndarray:
    """Like module A but the pullback extends well below the 50-day SMA (too deep)
    -- must NOT trigger module A."""
    close = _base_random_walk(rng, 50.0)
    close *= (1 + np.linspace(0, 0.6, N))
    tail = close[-25:].copy()
    tail *= (1 + np.linspace(0, -0.22, 25))  # deep breakdown, not a shallow pullback
    close[-25:] = tail
    return close


def build_negative_b(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, date]:
    """Gap of only 3% (below the 5% threshold) on normal volume -- must NOT trigger
    module B."""
    close = _base_random_walk(rng, 40.0)
    volume = np.abs(rng.normal(2e6, 3e5, N))
    gap_idx = N - 20
    close[gap_idx:] *= 1.03
    return close, volume, ALL_DATES[gap_idx]


def build_negative_c(rng: np.random.Generator) -> tuple[np.ndarray, date]:
    """Only a single insider purchase (not a cluster of >= 2) -- must NOT trigger
    module C."""
    close = _base_random_walk(rng, 30.0)
    idx = N - 30
    close[idx:] *= (1 + np.linspace(0, 0.1, N - idx))
    return close, ALL_DATES[idx - 3]


def build_negative_d(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """A genuine contracting base and breakout, but on volume below the 1.5x
    threshold -- must NOT trigger module D."""
    close = _base_random_walk(rng, 70.0)
    close *= (1 + np.linspace(0, 0.4, N))
    volume = np.abs(rng.normal(1.5e6, 2e5, N))
    base_start = N - 30
    base_len = 25
    base_level = close[base_start]
    for i in range(base_len):
        close[base_start + i] = base_level * (1 + rng.normal(0, 0.01))
    breakout_idx = base_start + base_len
    close[breakout_idx:] = base_level * (1.03 + np.linspace(0, 0.05, N - breakout_idx))
    # volume stays flat -- no confirming spike
    return close, volume


def build_split_ticker(rng: np.random.Generator) -> tuple[np.ndarray, date]:
    """A momentum-A-shaped setup (so a live trade opens near the end of the series)
    that undergoes a 2-for-1 split shortly after the signal date, while the
    hypothetical trade would still be open. Pre-split prices are ~2x post-split."""
    close = build_module_a_trigger(rng)
    split_idx = N - 8
    split_date = ALL_DATES[split_idx]
    close = close.copy()
    close[:split_idx] *= 2.0  # yfinance-style: history shown adjusted post-split
    return close, split_date


def build_prices(rng_master: np.random.Generator) -> dict[str, pd.DataFrame]:
    out = build_spy_and_sector_etfs(rng_master)
    for df in out.values():
        df.insert(0, "date", [d.isoformat() for d in ALL_DATES])
        df["adj_close"] = df["close"]
        df["price_source"] = "yfinance"
    extras: dict[str, dict] = {}

    for spec in TICKER_SPECS:
        rng = np.random.default_rng(SEED + spec.seed_offset)
        if spec.scenario == "module_a_trigger":
            close = build_module_a_trigger(rng)
            df = _ohlcv_from_close(rng, close, 5e6)
            df.loc[df.index[-16:-5], "volume"] *= 0.6  # light volume on the pullback
        elif spec.scenario == "module_b_trigger":
            close, volume, earn_date = build_module_b_trigger(rng)
            df = _ohlcv_from_close(rng, close, 2e6)
            df["volume"] = volume.astype(int)
            extras[spec.ticker] = {"earnings_date": earn_date}
        elif spec.scenario == "module_c_trigger":
            close, d1, d2 = build_module_c_trigger(rng)
            df = _ohlcv_from_close(rng, close, 1.5e6)
            extras[spec.ticker] = {"insider_dates": (d1, d2)}
        elif spec.scenario == "module_d_trigger":
            close, volume = build_module_d_trigger(rng)
            df = _ohlcv_from_close(rng, close, 1.5e6)
            df["volume"] = volume.astype(int)
        elif spec.scenario == "module_a_near_miss_extended":
            close = build_negative_a(rng)
            df = _ohlcv_from_close(rng, close, 5e6)
        elif spec.scenario == "module_b_near_miss_small_gap":
            close, volume, earn_date = build_negative_b(rng)
            df = _ohlcv_from_close(rng, close, 2e6)
            df["volume"] = volume.astype(int)
            extras[spec.ticker] = {"earnings_date": earn_date}
        elif spec.scenario == "module_c_near_miss_single_insider":
            close, d1 = build_negative_c(rng)
            df = _ohlcv_from_close(rng, close, 1.5e6)
            extras[spec.ticker] = {"insider_dates": (d1,)}
        elif spec.scenario == "module_d_near_miss_low_volume":
            close, volume = build_negative_d(rng)
            df = _ohlcv_from_close(rng, close, 1.5e6)
            df["volume"] = volume.astype(int)
        elif spec.scenario == "split_during_open_trade":
            close, split_date = build_split_ticker(rng)
            df = _ohlcv_from_close(rng, close, 5e6)
            extras[spec.ticker] = {"split_date": split_date}
        else:  # random_walk_control
            close = _base_random_walk(rng, float(rng.uniform(20, 150)))
            df = _ohlcv_from_close(rng, close, float(rng.uniform(5e5, 3e6)))

        df.insert(0, "date", [d.isoformat() for d in ALL_DATES])
        df["adj_close"] = df["close"]
        df["price_source"] = "yfinance"
        out[spec.ticker] = df

    out["_extras"] = extras  # type: ignore[assignment]
    return out


def populate_db(conn, prices: dict[str, pd.DataFrame]) -> None:
    extras = prices.pop("_extras")
    now = date.today().isoformat()

    ticker_rows = []
    for etf in list(SECTOR_ETFS.values()) + ["SPY"]:
        ticker_rows.append((etf, None, etf, "ARCA", None, None, None, None, 1, now))
    for spec in TICKER_SPECS:
        ticker_rows.append((
            spec.ticker, f"000{abs(hash(spec.ticker)) % 10_000_000:07d}", spec.ticker,
            "NASDAQ", spec.sector, f"{spec.sector} Industry", "7372",
            SECTOR_ETFS[spec.sector], 1, now,
        ))
    conn.executemany(
        "INSERT INTO tickers (ticker, cik, name, exchange, sector, industry, sic, "
        "sector_etf, is_active, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ticker_rows,
    )

    price_rows = []
    for ticker, df in prices.items():
        for _, row in df.iterrows():
            price_rows.append((
                ticker, row["date"], float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]), float(row["adj_close"]),
                int(row["volume"]), row["price_source"],
            ))
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close, "
        "volume, price_source) VALUES (?,?,?,?,?,?,?,?,?)",
        price_rows,
    )

    # Weekly universe snapshots: every Friday (or last trading day of the week),
    # all 30 synthetic tickers qualify.
    snapshot_rows = []
    week_ends = sorted({d - timedelta(days=d.weekday()) + timedelta(days=4) for d in ALL_DATES})
    for wk in week_ends:
        snap_date = wk if wk in ALL_DATES else max((d for d in ALL_DATES if d <= wk), default=None)
        if snap_date is None:
            continue
        for spec in TICKER_SPECS:
            df = prices[spec.ticker]
            row = df[df["date"] == snap_date.isoformat()]
            if row.empty:
                continue
            price = float(row["close"].iloc[0])
            adv20 = float(df["volume"].tail(20).mean() * price) if len(df) >= 20 else None
            snapshot_rows.append((snap_date.isoformat(), spec.ticker, price, 2_000_000_000.0, adv20))
    conn.executemany(
        "INSERT OR REPLACE INTO universe_snapshots (snapshot_date, ticker, price, "
        "market_cap, adv20) VALUES (?,?,?,?,?)",
        snapshot_rows,
    )

    # Regime log: derive from SPY vs its 200-day SMA and breadth of synthetic tickers
    # above their own 50-day SMA. Simple, deterministic, good enough for fixtures.
    spy_close = prices["SPY"]["close"].to_numpy()
    spy_sma200 = pd.Series(spy_close).rolling(200, min_periods=1).mean().to_numpy()
    closes_by_ticker = {s.ticker: prices[s.ticker]["close"].to_numpy() for s in TICKER_SPECS}
    regime_rows = []
    for i, d in enumerate(ALL_DATES):
        above = 0
        for s in TICKER_SPECS:
            c = closes_by_ticker[s.ticker]
            sma50 = pd.Series(c[: i + 1]).rolling(50, min_periods=1).mean().iloc[-1]
            if c[i] > sma50:
                above += 1
        breadth = 100.0 * above / len(TICKER_SPECS)
        spy_ok = spy_close[i] > spy_sma200[i]
        if spy_ok and breadth >= 50:
            regime = "Favorable"
        elif spy_ok or breadth >= 50:
            regime = "Caution"
        else:
            regime = "Unfavorable"
        regime_rows.append((d.isoformat(), float(spy_close[i]), float(spy_sma200[i]), breadth, regime))
    conn.executemany(
        "INSERT OR REPLACE INTO regime_log (date, spy_close, spy_sma200, breadth_pct, "
        "regime) VALUES (?,?,?,?,?)",
        regime_rows,
    )

    # Splits
    if "split_date" in extras.get("SPLIT1", {}):
        conn.execute(
            "INSERT INTO splits (ticker, date, ratio) VALUES (?,?,?)",
            ("SPLIT1", extras["SPLIT1"]["split_date"].isoformat(), 2.0),
        )

    # Earnings events + a matching 8-K filing for module B's earnings gap
    filing_rows = []
    earnings_rows = []
    insider_rows = []
    accession_counter = 1

    def next_accession() -> str:
        nonlocal accession_counter
        acc = f"0000000000-24-{accession_counter:06d}"
        accession_counter += 1
        return acc

    for spec in TICKER_SPECS:
        info = extras.get(spec.ticker, {})
        if "earnings_date" in info:
            ed = info["earnings_date"]
            acc = next_accession()
            earnings_rows.append((spec.ticker, ed.isoformat(), "before_open", "8-K_2.02", f"{ed.isoformat()}T07:00:00"))
            filing_rows.append((
                acc, f"000{abs(hash(spec.ticker)) % 10_000_000:07d}", spec.ticker, "8-K",
                ed.isoformat(), f"{ed.isoformat()}T07:00:00", "2.02,9.01", 1,
            ))
        if "insider_dates" in info:
            for j, trans_date in enumerate(info["insider_dates"]):
                acc = next_accession()
                filed = trans_date + timedelta(days=2)
                filing_rows.append((
                    acc, f"000{abs(hash(spec.ticker)) % 10_000_000:07d}", spec.ticker, "4",
                    filed.isoformat(), f"{filed.isoformat()}T18:00:00", "", 1,
                ))
                insider_rows.append((
                    acc, 1, f"000{abs(hash(spec.ticker)) % 10_000_000:07d}", spec.ticker,
                    f"CIK_INSIDER_{j}", f"Officer {j} of {spec.ticker}", 1, 0, 0,
                    "Chief Executive Officer" if j == 0 else "Chief Financial Officer",
                    "P", trans_date.isoformat(), 10_000.0, 15.0, 150_000.0, "A",
                    filed.isoformat(),
                ))

    conn.executemany(
        "INSERT INTO earnings_dates (ticker, event_date, timing, source, "
        "acceptance_datetime) VALUES (?,?,?,?,?)",
        earnings_rows,
    )
    conn.executemany(
        "INSERT INTO filings (accession, cik, ticker, form, filed_date, "
        "acceptance_datetime, items, processed) VALUES (?,?,?,?,?,?,?,?)",
        filing_rows,
    )
    conn.executemany(
        "INSERT INTO insider_trades (accession, row_num, cik, ticker, insider_cik, "
        "insider_name, is_officer, is_director, is_ten_pct_owner, officer_title, "
        "trans_code, trans_date, shares, price, value, acquired_disposed, filed_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        insider_rows,
    )

    # Minimal fundamentals summary snapshot (latest week only), used by guardrail
    # tests: all synthetic tickers get a plausible healthy profile except NEGA1
    # which is given a guardrail-failing profile (both op income and FCF negative).
    as_of = ALL_DATES[-1].isoformat()
    fs_rows = []
    for spec in TICKER_SPECS:
        if spec.ticker == "NEGA1":
            fs_rows.append((as_of, spec.ticker, 5e7, -2e6, -1e6, 4e7, 5e6, 1e7, None, None))
        else:
            fs_rows.append((as_of, spec.ticker, 5e8, 6e7, 5e7, 1e8, 8e7, 1e7, 12.0, 40.0))
    conn.executemany(
        "INSERT INTO fundamentals_summary (as_of, ticker, revenue_ttm, op_income_ttm, "
        "fcf_ttm, total_debt, cash, shares, ev_fcf, ev_fcf_sector_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        fs_rows,
    )

    conn.commit()


def main() -> None:
    rng_master = np.random.default_rng(SEED)
    DB_PATH.unlink(missing_ok=True)
    conn = init_db(DB_PATH)
    prices = build_prices(rng_master)
    populate_db(conn, prices)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    export_state(conn, STATE_DIR, as_of=END)
    conn.close()
    print(f"Wrote {DB_PATH} and {STATE_DIR}")


if __name__ == "__main__":
    main()
