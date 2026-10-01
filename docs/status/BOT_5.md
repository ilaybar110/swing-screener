# Bot 5 (Tracking, baseline, stats, backtest, dashboard) - status

## What was built

- `src/tracker.py`
  - `simulate(rec, prices_df, splits_df, config, entry_mode="stop") -> TrackResult` -
    pure and deterministic (no DB/network, inputs are not mutated). Implements
    PLAN section 11 / section 10 exactly; the conventions chosen where daily bars are
    ambiguous are listed under "Interpretation calls" below.
  - `update_all(conn, as_of, source, config=None)` - re-simulates every non-final
    recommendation and baseline of `source` on prices up to `as_of`, updates
    `status / current_r / last_updated`, writes each `recommendation_results` row once
    (`INSERT OR IGNORE`; final recs are never selected again). Idempotent.
  - `row_to_rec(row)` builds a `Recommendation` from a DB row.
- `src/baseline.py` - `create_for(conn, recs, as_of)` (3 seeded random tickers per rec from
  the signal-day universe, excluding the rec's ticker; idempotent) and
  `update_baselines(...)` (called by `update_all`). Baselines enter at the next open with
  the same stop distance in percent and use the same `simulate()` (`entry_mode="open"`).
- `src/stats.py` - `summary(conn, source)` (by primary module, contains-module, rank bucket
  1-10/11-20/21-50/51+, regime, sector, earnings_in_window; count, win rate, avg/median R,
  expectancy, profit factor, avg excess vs SPY/sector, comparison with baseline; overall
  and per-module equal-risk equity curve; "not enough data yet" warning for modules with
  < 30 closed trades), plus `summary_by_period(...)` and `group_table(...)` helpers.
- `backtest/runner.py` - `run(start, end, notes) -> run_id` (CLI:
  `python -m backtest.runner --start 2015-01-01 --end 2024-12-31 --notes "..."`).
- `dashboard/app.py` + `dashboard/data.py` - local Streamlit app (see below).

## How to run

```bash
pytest tests/test_tracker.py tests/test_baseline.py tests/test_stats.py \
       tests/test_backtest.py tests/test_dashboard.py -q
python -m backtest.runner --start 2015-01-01 --end 2024-12-31 --notes "first run"   # needs data/research.db
streamlit run dashboard/app.py
```

Dashboard: sidebar "Source" = Live (state/ CSVs imported via `state_io` into an in-memory
SQLite DB) or any backtest run from `data/research.db` (opened read-only, only offered if
the file exists). Views: Overview (counts, open/pending recs, regime history), Modules vs
baseline, Rank buckets, Breakdowns (regime/sector/earnings), Equity curve, Excess returns,
All recommendations (search + module/status/regime filters + CSV download), Recommendation
detail (entry/stop/target chart fetched from yfinance only when the toggle is on, outcome,
rationale, guardrail, LLM briefing). No account or sizing concepts anywhere.
Needs `streamlit>=1.50` (uses `width="stretch"`); requirements.txt currently says `>=1.35`
(change request not filed because the installed 1.64 works; bump if someone has an old one).

## Tests

`tests/test_tracker.py` (38): fill at entry / gap above entry, expiry after 5 bars, fill on
bar 5 vs 6, setup broken before entry, stop touched on entry day, gap through stop,
stop+target same day, gap entry planned-risk R, target + partial + trail, breakeven only
from next bar, gap below breakeven, SMA20 exit at next open, time stop (20th bar) with and
without partial, data ending mid-trade, MAE/MFE, benchmark returns, 0.1% cost, split during
a trade (adjusted and raw history), split before signal, open-mode (baseline), purity,
`update_all` advance -> finalise-once -> never modified -> idempotent -> no lookahead past
`as_of`.
`tests/test_baseline.py` (6), `tests/test_stats.py` (11), `tests/test_backtest.py` (5, end to
end on the fixture with a stand-in module: run row + config hash, source isolation, same
tracker, report/warning, two runs coexist and agree), `tests/test_dashboard.py` (15, data
helpers + every view through Streamlit `AppTest`).

Also ran the real Bot 4 modules + ranking on the fixture 2022-06..2024-12: 220 recs,
215 finalised, 660 baselines, ~35 s, report printed with the survivorship warning.

## Interpretation calls (spec was ambiguous)

1. **Prices/benchmarks in `simulate`**: `prices_df` columns `date, open, high, low, close`
   and optionally `ticker`; with a `ticker` column the same frame also carries `SPY` and the
   rec's sector ETF (mapped via `config.tracking.sector_etfs[rec.sector]`) for excess
   returns; without it they are None. `splits_df`: `date, ratio` (+ optional `ticker`).
   Include >= 20 bars before the signal so SMA20 is warm (`update_all` loads 60 calendar days).
2. **`current_r`** for an open trade is returned in `TrackResult.r_multiple` (the contract has
   no `current_r` field); `is_final` is False and `update_all` stores it in
   `recommendations.current_r`. It is mark-to-market at the last close including a realised
   partial and the cost. Closed trades have `r_multiple` final and `current_r` NULL.
3. **Statuses**: `pending` (no fill yet), `open`, final: `stopped` (whole position stopped,
   no target hit), `target_hit` (partial taken; remainder left via breakeven/SMA20),
   `time_stop`, `expired`. `exit_reason` gives detail: `stop, breakeven, sma20_exit,
   time_stop, setup_broken, entry_window_elapsed`. A time stop after a partial is `time_stop`.
4. **Intraday ordering**: a stop on the entry bar counts. Same-bar stop+target -> stop.
   Target fills at the target price (never improved for gaps). After the partial the
   breakeven stop (= actual entry fill) is live from the *next* bar. Stop exits on a gap use
   the open; on the entry bar they use the stop price.
5. **Entry window** = the first 5 *bars after the signal date* (not `valid_until`), a close
   below the stop before the fill expires early. A close below the stop on a bar that also
   filled is a stop-out, not an expiry.
6. **R** uses planned risk `entry - stop` (split-adjusted); a gap fill therefore moves R
   realistically. Cost = 0.1% of the fill, charged once for the whole position:
   `R = (avg_exit - fill)/risk - 0.001*fill/risk`; `pct_return = avg_exit/fill - 1 - 0.001`.
   Excess returns subtract the (gross) SPY/ETF return from the net strategy return.
7. **SMA20 exit** applies only to the remainder after the partial exit (per spec "the
   remainder exits on a close below the 20-day SMA"); before the target only stop / target /
   time stop apply. Time stop day = 20th bar after the entry bar, fills at that close and
   beats a pending SMA exit from the same bar.
8. **Benchmark window**: open of the entry date to close of the exit date (open of the exit
   date for next-open SMA exits).
9. **MAE/MFE** only count the part of each bar the position was actually held: the entry
   bar's low is ignored unless the bar opened at/above the entry (low may precede the
   fill); the exit bar of a stop counts up to the exit price only.
10. **Splits**: stored entry/stop/target are divided by the cumulative ratio of splits after
    `signal_date` (spec: yfinance returns split-adjusted history). Defensive addition: if the
    history is *raw* around a split (prev close / split-day open ~ ratio), earlier bars are
    divided by the ratio too. The committed fixture's `SPLIT1` is raw, so this is exercised
    (both raw and adjusted forms are tested). `update_all` applies split adjustment for
    `source == "live"` only; backtest recs are struck from the same adjusted history.
11. **Baseline draws** are seeded on `(random_seed, signal_date, ticker)` so they are
    reproducible across runs (backtest rec ids are run-namespaced). Baseline stop/target are
    re-derived each run from the next open; the `baselines` table has no entry/target
    columns, so only status and final result fields are persisted.
12. **Backtest ids**: `recommendations.id` is a global PK, so backtest rows are stored as
    `<run_id>/<date>_<ticker>`; two runs coexist in one DB. The ranking track-record
    component is effectively off in backtests (no results exist while ranking), and outcomes
    are tracked to the last SPY price date in the DB (`track_until`), not just to `end`.
    Weekly universe snapshots are added from `build_historical_universe` only where
    `universe_snapshots` has none within 10 days (this writes to `universe_snapshots` in
    research.db).
13. **Profit factor** is None when there are no losing trades; win = R > 0; expectancy =
    win_rate*avg_win + loss_rate*avg_loss (= mean R). Only trades that filled count
    towards "closed"; expired-unfilled ones are counted separately.

## Known issues / notes

- `tests/test_guardrail.py` (Bot 2, untracked in the tree at the time) failed to import
  (`src.guardrail` missing); not mine, and my tests do not depend on it.
- Backtest on real research.db with ~10 years of daily scans has not been run (no
  research.db here); only fixture-proven. `ranking.rank` is ~0.15 s per signal day.
- Dashboard price chart needs network (yfinance) and is fetched only on request.
- No change requests were needed.
