# Bot 7 (Integration: entrypoints and ops) — status

## What was built

| File | Purpose |
|---|---|
| `run_daily.py` | `--stage prepare|finalize|all`, `--date`, `--dry-run`, `--limit`, `--force`. Idempotent two-stage daily job (see below). |
| `run_weekly.py` | Universe refresh (prices for every plausible member first, then `refresh_universe`), fundamentals summary (frames), upcoming earnings for 35 trading days, export. `--date --dry-run --limit`. |
| `run_backfill.py` | Local, resumable research backfill into `data/research.db` (`--limit N` writes `data/research_trial.db`): tickers → prices+splits → EDGAR bulk → fundamentals bulk → vectorised regime. Progress/ETA lines. `--stages`, `--start`, `--end`, `--db`. |
| `open_dashboard.sh` | git pull, activate `.venv` (Git Bash/Linux), `streamlit run dashboard/app.py`. Creates the venv on first use. |
| `src/jobs.py` | Shared plumbing: `JobLog` (every step → `job_log`), `latest_completed_session`, `missed_days`, `price_plan`, `CriticalError`. |
| `docs/ROUTINE_DAILY.md`, `docs/ROUTINE_WEEKLY.md` | The exact prompts + settings for the two cloud Routines. |
| `README.md` | System overview, local setup, backfill, backtest, dashboard, routines, modules on/off. |
| `tests/e2e/*` | 23 tests: full prepare → simulated briefs → finalize on fixtures; determinism; rerun = no change; 3-day catch-up; split during an open trade; failure handling; weekly; backfill. |

Files Bots 1/2 never delivered, written by Bot 7: `src/data/prices.py`, `src/guardrail.py`, `src/valuation.py`.
Integration fixes in other bots' files are listed (each with a resolution) in `docs/CHANGE_REQUESTS.md`.

## How the daily job works

`prepare`: fresh `data/run.db` from `state/` → find the last `day` row in `job_log` → every missed NYSE session up to the latest *finished* one (close + 20 min, early closes handled) →
1. universe bootstrap only if no snapshot exists;
2. prices once for the whole range (universe ∪ SPY ∪ sector ETFs ∪ open recommendations'/baselines' tickers, with extra history for old open trades) + splits for open recs;
3. Nasdaq earnings calendar (next 7 days) — deliberately *before* ranking, so the earnings-in-window flag sees fresh dates (the brief listed it after);
4. per day, oldest first: EDGAR `process_day` → regime → each module's `scan` → `rank` → insert recommendations → baselines → `day` marker;
5. `tracker.update_all`; LLM inputs for the latest day's `in_report` recs; `export_state`.

`finalize`: reuses the DB `prepare` just built (it holds this run's prices; if absent it is rebuilt from `state/` and prices for open positions are re-fetched) → merge briefs → `report.build` (with `since` = last finalised day, so a gap shows up in the catch-up section) → `finalize` marker → export → Telegram → prints the digest + any non-critical errors. Skips if the day was already finalised.

Non-critical (logged to `job_log`, run continues): a module's scan, ranking of one ticker, EDGAR errors for a day, news/8-K text, splits, earnings calendar, tracker/baseline hiccups, Telegram. Critical (alert via Telegram, export valid state, exit 2): no prices at all / SPY missing for the target day, EDGAR unavailable for every processed day, universe bootstrap failure, report build failure. Unexpected exception: alert, export, exit 1.

## How to run / test

```bash
pytest tests/e2e -q                    # ~1 min, no network
pytest -q                              # everything (~4 min)
python run_weekly.py --dry-run --limit 50
python run_daily.py  --dry-run --limit 50
python run_backfill.py --limit 50 --stages tickers,prices,regime
```

## Interpretation calls

* **A day is "complete" when its steps ran**, even if some failed non-critically (errors are in `job_log` and the summary). Otherwise a persistent module bug would re-process the same day forever; catch-up only guards against *missed* days.
* **`job_log` rows are replaced per (job, trading_date)** and pruned to 45 days (the `day`/`finalize` markers keep their latest row), so `state/job_log.csv` stays small. Re-importing state into an already-populated DB would duplicate job_log rows (the table has no primary key) — entrypoints never do that.
* **Finalize reuses `run.db`** instead of re-importing state into it (re-import = duplicate job_log rows; and the DB has this run's prices, which `report` needs for "triggered" detection).
* **`--dry-run` still downloads** and uses a scratch DB (`data/run_dry.db`); it writes nothing to `state/`, `reports/`, `work/brief_inputs`, and sends nothing.
* **`--limit N`** in the daily job only restricts the price download (and the bootstrap universe); with an existing universe the regime breadth just counts tickers that have prices.
* **Weekly `as_of`** = latest completed session (Friday), so the snapshot is dated Friday and Monday's scan uses it.
* **Regime backfill is vectorised** (`run_backfill.stage_regime`) and verified equal to `compute_regime` on the fixture (`tests/e2e/test_backfill.py`); the historical universe it uses for breadth is reconstructed from prices only (price ≥ $5, 20-day dollar volume ≥ $20M, ≥ 126 days), not market cap.
* **Trial backfills use a separate DB** (`research_trial.db`) so `edgar_bulk`/`fundamentals.backfill_bulk` (which read every ticker in `tickers`) are naturally limited.

## Known issues / not done

* **Real full-scale runs have not been done**: daily/weekly were smoke-run with `--limit 50` only; the full backfill (hours, ~2.5 GB of SEC downloads) was not run, and the `edgar`/`fundamentals` backfill stages were not run against the real SEC bulk files (URLs verified with HEAD requests; the loaders are Bot 3/Bot 2's, tested on synthetic/trimmed data). Run `python run_backfill.py --limit 50` first.
* The daily EDGAR pass reads Form 4s/8-Ks for **every** ticker in `tickers` (~7,000 listed companies), not just the universe — about 2 minutes per session on a trial run; catch-up of several days scales linearly. Restricting `load_cik_map` to the universe would cut this.
* `fundamentals_summary` coverage via frames is partial (≈ 70% FCF, ≈ 50% debt on large caps); it only feeds the informational EV/FCF percentile.
* The smoke runs used a placeholder `SEC_EMAIL` (`swing-screener-smoke@example.com`); set your real address in `.env` / the routine.
* Backtests were not run (no research.db yet).
* `docs/CHANGE_REQUESTS.md`: the `EdgarClient.download_file` request (Bot 3) and the SPLIT1 fixture note remain open.
