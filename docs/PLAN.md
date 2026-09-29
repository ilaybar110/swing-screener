# Swing Screener — Plan / Spec

This document is the single source of truth for behavior. Code should implement this;
if code and this document disagree, this document wins unless a change is recorded in
`docs/CHANGE_REQUESTS.md` and merged back here.

## 1. Purpose

Generate swing-trade recommendations (holding period: days to ~3 months) for US stocks,
and automatically track the hypothetical outcome of **every** candidate that was ever
recommended, independent of whether the user acted on it. There is no broker integration,
no position sizing, no account management. The system is zero-cost to run.

## 2. Deployment model

- Daily and weekly runs execute as **Claude Code cloud Routines** on Linux, from a fresh
  clone of the repo each run, with network access to all data sources used below.
  The routine agent's job is narrow: run the pipeline scripts, write the LLM briefs
  (see §11), and commit results. It never modifies code.
- Persistent live state is a set of **CSV files under `state/`**, committed to the repo.
  Each cloud run builds a temporary SQLite database at `data/run.db` (gitignored) by
  importing `state/` plus freshly downloaded data, does its work, then exports state
  back out to CSV (`src/state_io.py`).
- The user's own computer holds a **permanent research database** at `data/research.db`
  (gitignored) with full price/fundamentals/filings history for backtesting, plus a
  local Streamlit dashboard that reads `state/` CSVs and `research.db` directly.
- The exact same schema and the exact same application code operate on both databases;
  only which DB file is opened differs (`src/config.py` / `src/db.py`).

## 3. Schedule

- Daily routine runs after US market sessions close, mornings Israel time, Tuesday
  through Saturday (i.e., covering Mon–Fri US trading days).
- Weekly routine runs Sunday.
- If a daily run is missed, the next daily run catches up every missed US trading day,
  in chronological order, before/while producing the current day's report.

## 4. Data sources

All raw HTTP downloads pass through a caching helper (`src/data/raw_cache.py`):
persistent on disk under `data/raw/` when run locally, per-run (ephemeral) in the cloud.

### 4.1 Prices
- Primary: Yahoo Finance via `yfinance`, `auto_adjust=False` (split-adjusted OHLC plus a
  separate `adj_close` column).
- Tickers that fail are retried once after a pause.
- Tickers still missing after retry are fetched from Nasdaq's historical data API
  (`api.nasdaq.com/api/quote/<SYM>/historical`) and flagged `price_source="nasdaq"`.
- Stooq is **not** used — it is blocked from the cloud environment.
- Live daily runs download roughly the last 300 trading days per ticker (more for any
  ticker with an open recommendation older than that, so the full trade can be
  re-simulated). The research DB holds full history starting 2014-01-01.

### 4.2 Splits
- Sourced from `yfinance` corporate actions.
- Used to adjust the stored entry/stop/target of any recommendation whose signal date
  predates a subsequently discovered split.

### 4.3 Universe, market cap, sector/industry
- Primary: Nasdaq's stock screener API.
- Fallback: SEC `company_tickers_exchange.json`.

### 4.4 EDGAR
- Daily form index: `daily-index/form.YYYYMMDD.idx`, used to find each day's Form 4 and
  8-K filings.
- Submissions API for 8-K items and filing acceptance timestamps.
- Form 4 XML for insider transaction detail.
- `companyfacts` per company, fetched on demand (not bulk) for candidate fundamentals.
- XBRL "frames" API for the weekly universe-wide fundamentals summary.
- Bulk files (`companyfacts.zip`, `submissions.zip`, Insider Transactions Data Sets) —
  local backfill only, never in the cloud routine.
- All EDGAR requests set `User-Agent` from the `SEC_EMAIL` environment variable and are
  rate-limited to at most 4 requests/second.
- The current day's daily index file may not yet be published when the routine runs;
  the client walks backward to the latest published one.

### 4.5 Earnings
- Upcoming: Nasdaq's earnings calendar.
- Historical/recent earnings events: derived from 8-K Item 2.02 filing acceptance
  timestamps.

### 4.6 News
- Google News RSS only. (Yahoo's news endpoints return nothing when called from the
  cloud environment.)

## 5. Universe (rebuilt weekly)

Included if all of:
- price > $5
- market cap > $1B
- 20-day average dollar volume > $20M
- at least 126 trading days of price history

Excluded:
- ETFs and funds
- SPACs (SIC code 6770, or common SPAC name patterns)
- preferred shares, warrants, units

A snapshot of the universe is saved every week (`universe_snapshots`).

## 6. Market regime (computed daily)

- SPY vs its 200-day SMA.
- Breadth = percentage of the universe trading above its own 50-day SMA.
- **Favorable**: both conditions OK.
- **Caution**: exactly one of the two fails.
- **Unfavorable**: both fail.

All strategy modules run and all candidates are tracked in every regime — regime only
affects how many recommendations are surfaced in the report (§12).

## 7. Strategy modules

Each module is independently enabled/disabled via `config.yaml`; parameters are fixed
in config before any testing begins (no in-sample tuning). All modules are
point-in-time only: they must not use information that would not have been available
as of the signal date.

Common rule for all modules: skip a signal if the resulting stop distance is
< 2% or > 12% of entry price.

### A — Momentum leader pullback
- Top 20% of the universe by 6-month relative strength vs SPY (skipping the most recent
  month).
- Within 25% of the 52-week high.
- Above a rising 50-day SMA.
- Pulls back to the 20- or 50-day SMA on below-average volume.
- Trigger: close above the prior day's high.
- Stop: below the pullback low.

### B — Earnings gap drift
- Gap up ≥ 5% on the first session after an earnings report.
- Volume ≥ 2x the 50-day average on the gap day.
- Holds above the gap-day low for 2–5 sessions in a tight range.
- Trigger: close above that range.
- Stop: below the gap-day low.

### C — Insider cluster buying
- At least 2 distinct officers/directors (not 10%-owners) file open-market purchases
  (transaction code `P`), each ≥ $50K, totaling ≥ $250K, within a rolling 30 days,
  measured by filing date.
- Trigger: close above the 20-day SMA after the cluster completes.
- Stop: below the recent swing low, or 2 ATR, whichever applies.
- Expected to fire rarely.

### D — Base breakout
- Within 15% of the 52-week high.
- ATR/range has been contracting for 3–8 weeks (a "base").
- Trigger: close above the base high on volume ≥ 1.5x the 50-day average.
- Stop: base midpoint, or 2 ATR, whichever is tighter.

## 8. Fundamental guardrail

Evaluated on demand, for candidates only (not the whole universe), result is one of
`pass` / `fail` / `unknown`:

- **Fail** if TTM operating income AND TTM free cash flow are both negative.
- **Fail** if diluted shares outstanding grew more than 10% year-over-year.
- **Fail** if total debt > 5x TTM operating income (this check is skipped for
  Financials sector companies, and whenever operating income ≤ 0).
- Missing data always resolves to **unknown**, never to fail.
- Informational only, not a pass/fail input: EV/FCF percentile vs the company's sector,
  computed from the weekly fundamentals summary.

## 9. Ranking

- Each of setup quality, relative strength, and overlap is converted to a 0–100
  percentile within that day's candidates.
- Overlap score: 1 module = 0, 2 modules = 70, 3+ modules = 100.
- Weights: setup quality 45%, relative strength 35%, overlap 20%.
- A module's own historical track record contributes 0% until that module has at least
  30 closed live results; after that it contributes 10%, with the other weights scaled
  down proportionally.
- The same ticker signaling on the same day from multiple modules becomes a single
  recommendation; the module with the highest setup score is primary and its entry/stop
  are used.
- The report shows at most 2 recommendations per GICS industry.
- Recommendations whose expected holding window (25 trading days) contains a known
  earnings date are flagged.

## 10. Trade plan

- **Entry**: buy-stop placed above the trigger day's high.
- **Stop**: as defined by the module.
- **R** = entry − stop.
- **Target** = entry + 2R:
  - 50% of the (hypothetical) position exits at target; remaining stop moves to
    breakeven (entry).
  - The remainder exits on a close below the 20-day SMA (filled at the next day's open)
    or at breakeven, whichever comes first.
- **Time stop**: close of the 20th trading day after entry, if still open.
- **Validity**: the entry order is valid for 5 trading days, or until a close below the
  stop occurs before the entry ever triggers.
- No position sizes or dollar amounts are shown anywhere — only stop/target as price
  and percentage.

## 11. Tracking (stateless simulation)

Every run re-simulates every **non-final** recommendation from its original signal date,
using fresh prices, through one pure function `simulate()` (owned by Bot 5,
`src/tracker.py`, signature in `src/contracts.py`).

Rules:
- Entry fills at the entry price, or at the open if price gapped above entry.
- Stop fills at the stop price, or at the open if price gapped below stop.
- A stop touched on the same day as entry counts as a stop-out.
- If stop and target are both touched the same day, the stop wins (conservative).
- Round-trip trading cost: 0.1%, applied in the return math.
- Outputs per recommendation: status, R multiple, % return, days held, MAE and MFE (in
  R), SPY return and sector-ETF return over the identical period, and excess return vs
  each.
- Sector ETF map: XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC.
- Once a recommendation reaches a final state, its `recommendation_results` row is
  written once and never recomputed.

## 12. Report contents

- `reports/YYYY-MM-DD.html` (self-contained, no external assets) and
  `reports/YYYY-MM-DD.md`, plus `reports/latest.html` / `reports/latest.md` that mirror
  the most recent run.
- Each report includes: today's new recommendations, tracking updates for open
  positions, and a statistics section.
- Report length by regime: Favorable → top 10, Caution → top 5, Unfavorable → none
  (banner only, table suppressed).
- Optional Telegram delivery: a short text summary plus the HTML file attached; skipped
  silently (no error) if `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are not configured.

## 13. Baseline

For every recommendation, 3 randomly seeded tickers are drawn from that same day's
universe as a control group: entered at the next day's open, given the same stop
distance in percentage terms, exited by the same rules, and tracked the same way in a
separate `baselines` table (never mixed into the main recommendations statistics).

## 14. Statistics

Computed and grouped by: strategy module, rank bucket (1–10 / 11–20 / 21–50 / 51+),
market regime, sector, and whether earnings fell inside the holding window. For each
group: count, win rate, average/median R, expectancy, profit factor, excess return vs
SPY, excess return vs sector, excess return vs baseline, and an equal-risk hypothetical
equity curve.

## 15. Backtesting (local only, never in the cloud routine)

Same modules and the same `simulate()` function run against `data/research.db` using
strictly point-in-time data (by filing date). Windows: 2015–2019, 2020–2022, 2023+.
Results are explicitly documented as an upper bound on real performance, since
survivorship bias is not corrected for.

## 16. LLM briefs

Written by Claude *inside* the cloud routine, for the top 10 reported recommendations
only:
1. The pipeline (`src/llm/brief_io.py`, owned by Bot 6) writes one input file per stock:
   filtered 8-K items (1.01, 2.02, 3.02, 5.02, 7.01, 8.01) plus Exhibit 99.1 text, plus
   14 days of news, capped at roughly 6,000 tokens per stock.
2. The routine agent writes one JSON file per stock containing `summary`,
   `upcoming_catalysts`, `red_flags`, `recent_positive_events`, and `sources`, using
   only facts present in the input file (no outside knowledge).
3. The pipeline validates and merges the JSON files into the recommendation records.
4. A missing or invalid brief never blocks the report; the section is simply omitted
   for that stock.

## 17. Non-goals

No broker connectivity, no position sizing/account management, no real-money execution,
no guarantee of profitability. This is a research and tracking tool.
