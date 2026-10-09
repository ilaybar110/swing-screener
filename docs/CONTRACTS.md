# Contracts

Narrative companion to `src/contracts.py` and `src/data_access.py`. Read those
files for exact signatures/docstrings; this document explains the shapes and the
fixture data used to test against them.

## Dataclasses (`src/contracts.py`)

- **Candidate** — one module's raw signal for one ticker on one day, before
  ranking: `ticker, module, signal_date, entry, stop, setup_score (0-100),
  rationale, details`.
- **Recommendation** — the ranked, tradeable output; mirrors the `recommendations`
  table row for row. `id` is `"YYYY-MM-DD_TICKER"`. JSON-typed fields (`modules`,
  `guardrail_reasons`, `valuation_info`, `details`, `llm_brief`) are plain Python
  objects in the dataclass; `src/state_io.py` and any DB write path handle
  (de)serialization to/from the TEXT columns.
- **GuardrailResult** — `status ("pass"/"pass_partial"/"fail"/"unknown"), reasons,
  metrics, valuation_info`. `pass_partial` = evaluable checks passed, some could not be
  evaluated (listed in `reasons`); ranking treats it like `pass`.
- **TrackResult** — output of `tracker.simulate()`; `is_final=True` once a
  recommendation reaches a terminal state (stopped/target_hit/time_stop/expired)
  and its `recommendation_results` row should be written and never touched again.
- **LLMBrief** — `ticker, summary, upcoming_catalysts, red_flags,
  recent_positive_events, sources`.

## StrategyModule interface

Every file under `src/modules/` implements:

```python
class SomeModule:
    name: str

    def scan(self, as_of: date, data: DataAccess) -> list[Candidate]: ...
    def scan_history(self, start: date, end: date, data: DataAccess) -> list[Candidate]: ...
```

`scan_history` must be vectorized for backtest performance but must return results
identical to calling `scan()` once per trading day in `[start, end]`. Both are
point-in-time: neither may use information dated after `as_of` (or, for
`scan_history`, after the day being evaluated within the range).

## DataAccess (`src/data_access.py`)

Thin, read-only SQLite query layer, shared unmodified by every bot. Construct with
`DataAccess(conn)` where `conn` points at either `data/run.db` or
`data/research.db` — the queries behave identically either way. See the class
docstrings for the exact query semantics of each method (`get_prices`,
`get_splits`, `get_universe`, `get_sector_etf`, `get_regime`, `get_fundamentals`,
`get_fundamentals_summary`, `get_insider_trades`, `get_earnings_events`,
`get_upcoming_earnings`). All `as_of`/`filed_as_of` parameters are point-in-time
cutoffs — rows dated/filed after that date are never returned.

## Cross-bot function signatures

`src/contracts.py` has one stub function per cross-bot entry point, named
`_botN_<function>`, each a docstring-only `NotImplementedError` describing exactly
what the real function (owned by that bot, in its own file) must do: name,
parameters, return shape, and which section of `docs/PLAN.md` it implements. Do not
implement business logic in `src/contracts.py` — copy the signature into your own
module and implement it there.

## Fixture tickers (`tests/fixtures/`)

`tests/fixtures/generate_fixtures.py` builds a fully deterministic (seed 424242)
synthetic universe covering 2022-01-03 through 2024-12-31: SPY, all 11 sector ETFs
(XLK/XLF/XLV/XLY/XLP/XLE/XLI/XLB/XLU/XLRE/XLC), and 30 synthetic stock tickers.
Regenerate with `python -m tests.fixtures.generate_fixtures` after any scenario
change, and commit both `tests/fixtures/fixture.db` and `tests/fixtures/state/`.

| Ticker | Scenario | Expected outcome |
|---|---|---|
| `MOMA1` | Leader pullback, shallow (~6%) pullback to the 20/50 SMA on light volume (last 25 sessions), then a breakout above the prior day's high | **Should trigger Module A** |
| `MOMB1` | +7% earnings gap with 2.5x volume ~20 sessions before the end, 4-session tight range, then breakout | **Should trigger Module B** (earnings date recorded in `earnings_dates` / `filings` as an 8-K with item `2.02`) |
| `MOMC1` | Sideways drift, then two officers file open-market buys (`insider_trades`, code `P`) 3 and 6 sessions before a reclaim of the 20-day SMA | **Should trigger Module C** |
| `MOMD1` | Uptrend into a ~25-session contracting base near the highs, breakout on 2x volume | **Should trigger Module D** |
| `NEGA1` | Same setup as `MOMA1` but the "pullback" is a 22% breakdown, not a shallow pullback | **Must NOT trigger Module A** (also carries a guardrail-failing fundamentals profile: TTM operating income and FCF both negative — use to test `guardrail.evaluate` returning `fail`) |
| `NEGB1` | Same shape as `MOMB1` but only a 3% gap (below the 5% threshold) | **Must NOT trigger Module B** |
| `NEGC1` | Only a single insider buy, not a cluster of >= 2 | **Must NOT trigger Module C** |
| `NEGD1` | Genuine contracting base and breakout, but volume stays flat (no >= 1.5x confirmation) | **Must NOT trigger Module D** |
| `SPLIT1` | Same shape as `MOMA1`'s breakout, but undergoes a 2-for-1 split (`splits` table, ratio 2.0) 8 sessions before the end of the series — while a trade opened on the late signal would still be open | Use to test split-adjustment of stored entry/stop/target in an open trade (docs/PLAN.md §4.2) |
| `CTRL01`…`CTRL21` | Plain random walks, varied starting prices/sectors, no engineered setup | Control group — should mostly not trigger any module; used for baseline sampling and to check modules don't over-fire on noise |

All 30 synthetic tickers pass the weekly universe filters every week in
`universe_snapshots` (price/market cap/ADV thresholds are satisfied by construction).
`fundamentals_summary` has one snapshot as of 2024-12-31 with a healthy profile for
every ticker except `NEGA1` (guardrail-fail profile, see above).

If a module's real logic needs the fixture reshaped to trigger reliably (e.g. a
different pullback depth), file a `docs/CHANGE_REQUESTS.md` entry against
`tests/fixtures/generate_fixtures.py` rather than hand-editing `fixture.db` or
`state/` directly — the DB and CSVs must always be regenerated from that script.
