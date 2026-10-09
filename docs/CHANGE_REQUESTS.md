# Change Requests

Append-only log of requested changes to shared/foundation files. A bot that needs a
change to a file it doesn't own adds an entry here (never edits the file directly)
and works around it locally in the meantime. Bot 0 (or whoever currently owns the
target file) reviews and applies.

Template:

```
## YYYY-MM-DD — Bot N
File: path/to/file
Request: what you need changed
Why: what breaks/is-blocked without it
Status: open | applied | declined (+ reason)
```

---

## 2026-10-01 — Bot 4
File: tests/fixtures/generate_fixtures.py
Request: In `build_module_b_trigger` (and `build_negative_b`), make the gap day's *open* gap, not only the close. `_ohlcv_from_close` derives `open[i]` from `close[i-1]`, so MOMB1's 2024-12-03 bar opens ~0.3% above the prior close even though it closes +7.8%. Set `open[gap_idx] = close[gap_idx-1] * 1.07` (NEGB1: `* 1.03`) after building the OHLCV frame, and keep `low <= open <= high`.
Why: PLAN.md 7-B (and the Bot 4 brief) require the earnings session to *open* >= 5% above the prior close. With the fixture as shipped, Module B correctly does not fire on MOMB1. Bot 4's tests patch a private copy of fixture.db (open of MOMB1 +7%, NEGB1 +3% on 2024-12-03) so MOMB1 triggers on 2024-12-10; once regenerated these patches become no-ops.
Status: applied by Bot 7 (2026-10-01): `_open_gap()` helper makes MOMB1's gap-day open +7% (NEGB1 +3%) without consuming random numbers; fixture.db and state/ regenerated, full suite green. Bot 4's private patches are now no-ops. Side effect of regenerating: the synthetic CIKs in the tickers/filings/insider_trades CSVs differ from before (the generator assigns them non-deterministically; harmless, but a future cleanup could seed them).

## 2026-10-01 — Bot 4
File: config.yaml / src/config.py
Request: Add the module parameters that the strategy spec fixes but config does not yet carry (all currently defaulted as constants at the top of each src/modules/*.py file, read via `getattr(section, name, default)` so adding them to config takes effect with no code change):
- a_momentum_pullback: `pullback_touch_window: 5`, `pullback_touch_pct: 0.02`, `pullback_close_floor: 0.97`, `sma_slope_sessions: 10`, `stop_atr_buffer: 0.1`
- b_earnings_gap_drift: `tight_range_atr_multiple: 1.5`, `stop_atr_buffer: 0.1`
- c_insider_cluster: `completion_window_sessions: 20`, `stop_lookback_sessions: 10`
- d_base_breakout: `contraction_ratio: 0.7`
Why: "All parameters come from config.yaml" -- these are the remaining hard-coded numbers. (Existing keys already cover RS top 20% = pct >= 80, 75% of 52w high, and base 3-8 weeks = 15-40 sessions.)
Status: applied by Bot 7 (2026-10-01): the 9 parameters were added to `config.yaml` and as optional (defaulted) fields in `src/config.py`; defaults equal the previous module constants, so behaviour is unchanged.

## 2026-10-01 — Bot 3
File: .gitignore
Request: Change the `data/` line to `/data/` (anchored to the repo root).
Why: the unanchored pattern also ignores `src/data/`, so NOTHING under `src/data/` is tracked by git -- including Bot 0's `edgar_client.py`/`raw_cache.py` and every data module from Bots 1-3 (`git status` doesn't even list them). A fresh clone in the cloud routine would have no `src/data/` package at all. Bot 3 commits its own files there with `git add -f` as a workaround; Bots 0/1/2 files under `src/data/` are still untracked.
Status: applied by Bot 7 (2026-10-01): the line is now `/data/`, so all of `src/data/` is tracked again (Bots 0/1/2 files there had never been committed).

## 2026-10-01 — Bot 3
File: src/data/edgar_client.py
Request: Add a streaming download method, e.g. `download_file(url, dest_path, resume=True)`, that goes through the client's session/User-Agent/throttle and writes to disk with HTTP Range resume (no in-memory/raw-cache buffering).
Why: `submissions.zip` is ~1.5 GB; `_get` buffers the whole body and hex-encodes it into the raw cache. `edgar_bulk.download_file` is a local workaround that uses the client's `_session` and `_throttle()` directly.
Status: declined by Bot 8 audit (2026-10-01): not needed. `edgar_bulk.download_file` (Range resume via the client session/throttle) was exercised for real by the audit backfill (1.5 GB submissions.zip + 1.4 GB companyfacts.zip, 0 errors), so a second public method would only duplicate it.

## 2026-10-01 — Bot 6
File: src/stats.py
Request: `summary(conn, "live")` raises `KeyError: 'id'` (line ~195, `base[base["parent_rec_id"].isin(sub["id"])]`) when recommendations exist but none has closed yet (empty `closed` frame loses its columns after the `.map(...)` filter, so `sub["id"]` fails). Please make the per-module `contains` loop robust to an empty `closed` (e.g. skip modules when `closed.empty`, or build `sub` with `closed.loc[mask]`).
Why: This is the normal state for the first days/weeks of live running. Bot 6's report works around it with a counts-only fallback, so the report never fails, but the module/baseline table is blank until fixed.
Status: applied by Bot 7 (2026-10-01): the per-module `contains` loop now uses `closed.loc[mask.astype(bool)]`, so an empty `closed` keeps its columns. Regression test: tests/e2e/test_integration_fixes.py.


## 2026-10-01 — Bot 7 (integration; all APPLIED by Bot 7)
Files: src/data/prices.py, src/guardrail.py, src/valuation.py (new), src/jobs.py (new)
Request/Change: Bot 1 never delivered `src/data/prices.py` and Bot 2 never delivered `src/guardrail.py` / `src/valuation.py` (only their tests/other files existed; `tests/test_guardrail.py` failed to import). Bot 7 wrote them to the contracts in `src/contracts.py` and `tests/test_guardrail.py`: `update_prices` (yfinance, one retry pass, Nasdaq fallback with `price_source="nasdaq"`, `BRK/B` -> `BRK-B` symbol mapping), `update_splits`, `guardrail.evaluate` (PLAN s.8; an unevaluable check is skipped, "unknown" only when nothing can be evaluated) and `valuation.valuation_info`. New `src/jobs.py` holds the shared entrypoint plumbing (job_log helper, "latest completed session", price-download plan).
Status: applied (docs/OWNERSHIP.md updated). Bots 1/2 never wrote status files; there are none for them.

## 2026-10-01 — Bot 7
File: src/data/fundamentals.py
Change: found by the real weekly smoke run: (1) `update_summary` crashed on `sqlite3.Row.get` -> converted to dict; (2) the shares frame was requested with unit `USD` (must be `shares`); (3) the instant-frame period containing `as_of` is not filed yet (404) -> newest-first merge over the last 3 quarters; (4) cash-flow items have no quarterly frames after Q1 (cash-flow statements are year-to-date), so OCF/capex fall back to the latest fiscal-year frame; revenue/capex/debt/shares try a second tag; (5) `BULK_COMPANYFACTS_ZIP_URL` was wrong (verified 403) -> `.../daily-index/xbrl/companyfacts.zip` (verified 200, 1.4 GB). Coverage on 49 large caps went from 1 -> 34 FCF values, 0 -> 26 debt, 1 -> 37 shares.
Status: applied

## 2026-10-01 — Bot 7
Files: requirements.txt, requirements-local.txt (new), tests/conftest.py, tests/test_universe.py, docs/OWNERSHIP.md
Change: `requirements.txt` is now the lean runtime set (the cloud routine installs it every run); streamlit (>=1.50, what dashboard/app.py needs) and pytest moved to `requirements-local.txt`. `tests/conftest.py` sets a placeholder `SEC_EMAIL` (tests need it to construct EdgarClient; Bot 1's refresh_universe tests failed without it). `tests/test_universe.py`: `is False/True` -> `bool(...) is ...` (pandas 3 returns `np.bool_`).
Status: applied

## 2026-10-01 — Bot 7
File: tests/fixtures/generate_fixtures.py (observation, not changed)
Note: SPLIT1's price series is stored *unadjusted* (the generator comment says "yfinance-style adjusted", but pre-split prices are 2x), and its split-day bar keeps the pre-split open/high. Live yfinance history is split-adjusted, so the e2e price stub (`tests/e2e/conftest.py::_as_yahoo_adjusted`) serves SPLIT1 adjusted and repairs that bar. Bot 5's tracker handles both raw and adjusted history.
Status: declined by Bot 8 audit (2026-10-01): cosmetic and already handled (tracker supports both raw and adjusted history, e2e stub serves adjusted); regenerating the fixtures would churn every committed fixture file for no behavioural gain.
