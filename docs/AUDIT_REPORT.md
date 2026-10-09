# Audit Report (Bot 8)

Independent verification of Bots 0-7, run 2026-10-01 on Windows 11 / Python 3.13 with live network access.
All trial output went to temporary directories (`--base-dir`); the repository's `state/` and `reports/`
are untouched (both still empty).

Final suite: `pytest` -> **360 passed, 2 skipped** (the 2 skips are the opt-in live-network tests), 0 failed.
Fresh venv from `requirements.txt` only: all 50 production modules import.

## 1. Checklist

| # | Check | Result | Evidence |
|---|---|---|---|
| **Phase 1 - Inventory** | | | |
| 1.1 | Every file in OWNERSHIP.md exists | PASS | all owner paths present (`git ls-files`, 136 tracked files before the audit); `src/data/` is tracked (`.gitignore` uses `/data/`) |
| 1.2 | Cross-bot signatures match `src/contracts.py` | PASS | new `tests/test_contract_signatures.py`: 28 tests, every `_botN_*` stub mapped to the real function; names/order identical, extra params optional. `_bot4_trade_plan_build` is `trade_plan.plan_levels` (dict shape); `trade_plan.build` is the Recommendation-returning variant (documented by Bot 4) |
| 1.3 | No leftover stubs | PASS | grep `NotImplementedError/TODO/FIXME/stub/pass`: only (a) the contract stubs by design, (b) `modules/base.py` abstract `scan_ticker`, (c) `fundamentals.backfill_bulk(zip_path=None)` documented (run_backfill always passes the zip), (d) `ranking._guardrail_stub` import-failure fallback. One placeholder constant in `baseline._pseudo_rec` (hard-coded `2.0`) replaced by `config.trade_plan.target_r_multiple` |
| 1.4 | CHANGE_REQUESTS all resolved | PASS | all "applied"; the two that were "open" (`EdgarClient.download_file`, SPLIT1 fixture note) are now "declined" with reasons (real 3 GB bulk download worked through the existing helper; cosmetic) |
| 1.5 | Status-file known issues | PASS | each is fixed or listed below (see section 4): Bot 0 none; Bot 3 `.gitignore` (fixed by Bot 7), `download_file` (declined), real 1.5 GB submissions.zip now exercised; Bot 4 fixture/config items (applied), performance (fine); Bot 5 backtest on real data (now run, found+fixed bug 2.9); Bot 6 stats KeyError (applied); Bot 7 real full-scale run (cannot be done locally - risks), EDGAR scope (see risks) |
| **Phase 2 - Static / environment** | | | |
| 2.1 | Fresh venv installs, all modules import | PASS | `python -m venv C:\sv2; pip install -r requirements.txt` -> 50 modules imported, 0 failures |
| 2.2 | Lint | PASS | `ruff check src backtest dashboard notify run_*.py --select F,E9,PLE` -> clean after fixes (F821 `pd` in contracts annotations, 4 unused imports, zero-width-space literal). Only unused imports in some test files remain (harmless) |
| 2.3 | No secrets / absolute paths / OS-specific code; pathlib; explicit encodings; CSV `\n` | PASS | greps: env reads only in `config.py`, `edgar_client.py`, `notify/telegram.py`; no drive letters; every text `open/read_text/write_text` has `encoding=`; `state_io._write_csv` uses `newline="\n"`, `lineterminator="\n"` (byte-identical round-trip test) |
| 2.4 | Config completeness; no hardcoded strategy thresholds | PASS with notes | every strategy parameter in PLAN is in `config.yaml` and validated by pydantic; module extras were added by Bot 7 and the module constants are only defaults for them. Structural indicator windows stay in code (ATR 14, volume averages 20/50, swing lookback 20, history windows). **Exception: the regime breadth threshold (50 %) is hard-coded in `src/regime.py` and not defined by the spec -> Decision D1** |
| 2.5 | Secrets from env only; Telegram absent -> silent skip | PASS | `tests/test_notify.py` (each var missing); real run printed only "Telegram not configured ... skipping" at INFO and exit 0 |
| **Phase 3 - Tests** | | | |
| 3.1 | Full suite | PASS | 360 passed, 2 skipped, 0 failed (4m18s) |
| 3.2 | Coverage of critical files | PASS | tracker 96 %, state_io 98 %, modules a/b/c/d 99/92/94/98 %, modules/base 89 %, ranking 91 %, guardrail 97 %, edgar_daily 94 %, brief_io 92 %, run_daily 76 % (remaining lines are error/alert branches, exercised by e2e failure tests and the real runs), prices 79 % (was 0 test file; new), fundamentals 51 % (bulk backfill + frames paths: exercised by the real runs), total 87 % |
| **Phase 4 - Spec compliance** | | | |
| 4.1 | Tracker: entry fill at entry / at open on gap-up | PASS | `test_fill_at_entry_and_mark_to_market`, `test_gap_above_entry_fills_at_open` |
| 4.2 | Stop fill at stop / at open on gap-down; stop on entry day; stop+target same day -> stop | PASS | `test_plain_stop_out_after_entry`, `test_gap_down_through_stop_fills_at_open`, `test_stop_touched_on_entry_day_counts`, `test_stop_and_target_same_day_stop_wins` |
| 4.3 | 50 % at 2R, stop to entry, remainder exits at next open after close < SMA20 | PASS | `test_target_takes_half_and_stays_open`, `test_partial_then_breakeven_stop`, `test_remainder_exits_on_close_below_sma20_at_next_open` |
| 4.4 | Time stop (20th day), expiry (5 days / close below stop), 0.1 % cost | PASS | `test_time_stop_at_close_of_20th_bar_after_entry`, `test_expires_after_five_bars_without_fill`, `test_fill_on_sixth_bar_is_too_late`, `test_setup_breaking_before_entry_expires_early`, `test_pct_return_includes_round_trip_cost` |
| 4.5 | Split during an open trade; results written once; same `simulate()` live/backtest | PASS | `test_split_during_open_trade_matches_unsplit_trade`, e2e `test_split_during_open_trade_is_handled`, `test_update_all_advances_then_finalises_once/never modified`, `test_backtest.py` (same tracker); real backtest used it for 326 recs |
| 4.6 | Fixture tickers trigger on expected dates; negatives don't | PASS | `test_modules_scan.py` (MOMA1 12-31, MOMB1 12-10, MOMC1 11-18, MOMD1 12-24; NEG* silent) |
| 4.7 | `scan()` == `scan_history()`; point-in-time (prices, filings, insider trades, fundamentals) | PASS | `test_scan_matches_scan_history_day_by_day`, `..._over_a_long_range`, `test_scan_ignores_future_prices_and_filings`, `test_insider_cluster_not_visible_before_filing`; **new** `test_guardrail_ignores_fundamentals_filed_after_as_of` (restated + later-period facts filed after as_of change nothing) |
| 4.8 | Stop distance outside 2-12 % skipped | PASS (new test) | `test_stop_distance_outside_band_is_skipped` (min above / max below every real distance -> zero candidates; baseline non-empty) + `test_candidate_levels_are_valid` |
| 4.9 | Multi-module merge uses primary module's entry/stop | PASS (new test) | `test_merge_uses_primary_modules_entry_and_stop` (both input orders) |
| 4.10 | Guardrail rules incl. Financials / op-income <= 0 exceptions; missing = unknown | PASS | `tests/test_guardrail.py` (11) + new `test_guardrail_missing_data_is_unknown_never_fail`; see D2 for partial data |
| 4.11 | Ranking 45/35/20, track record >= 30 then 10 %, top-N by regime, <= 2 per industry, all candidates saved/tracked | PASS | `test_ranking_orders_by_total...` (formula checked), `test_track_record_locked_below_threshold / unlocks_at_30...`, `test_regime_top_n_and_industry_cap` (10/5/0), `test_industry_cap_skips_to_next_best`; real run: 6 recs saved, 5 in_report (Caution), 6 tracked, 18 baselines |
| 4.12 | Baselines: 3 per rec, seeded, same stop %, same exit rules | PASS | `tests/test_baseline.py` (6); real run: 18 baselines for 6 recs |
| 4.13 | state_io byte-identical round trip; month sharding; retention | PASS | `test_export_import_export_is_stable`; new `test_state_export_retention_windows_and_latest_summary` (insider 120 d, earnings 180 d incl. future, filings 120 d, latest summary only) and `test_recommendations_are_sharded_by_month_and_only_live` (shards + byte-identical) |
| 4.14 | run_daily prepare/finalize idempotent | PASS | e2e `test_rerun_changes_nothing`; real data rerun: sha256 of all `state/`+`reports/` files identical before/after (`REAL_RERUN_IDENTICAL`) |
| 4.15 | Catch-up of missed days in order, then one report | PASS | e2e `test_catch_up_three_missed_days_in_order`; real run: 6 missed sessions 09-23..09-30 processed in order, one report, catch-up section listed the 09-25 DELL rec |
| 4.16 | EDGAR `process_day` idempotent, unpublished index | PASS | `test_process_day_is_idempotent`, `test_process_day_walks_back_when_day_unpublished`; real runs reprocessed days without duplicates |
| 4.17 | Earnings timing BMO/AMC/during -> correct session | PASS after fix | edgar_daily table of 11 timing cases; **bug 2.6 fixed**: Module B mapped the stored vocabulary wrongly; `test_module_b_maps_event_timing_to_reaction_session` |
| 4.18 | Price download retry + Nasdaq fallback + missing not fatal | PASS (new) | `tests/test_prices.py` (5): retry pass, Nasdaq fallback, missing reported, chunk exception, idempotent upsert, `BRK/B` mapping |
| 4.19 | Missing/invalid LLM briefs never block | PASS | e2e `test_missing_briefs_do_not_block_the_report`, `test_invalid_brief_is_dropped_but_report_is_built`, `test_report*` (4 invalid variants) |
| 4.20 | Report: self-contained HTML, MD, latest copies, no share counts, banners, Unfavorable suppressed, data-quality note, catch-up section | PASS | `test_report.py` `_assert_clean` (no `src=/href=/url(/@import`, no sizing language), `test_unfavorable_suppresses_cards`, `test_data_quality_note_from_job_log`, `test_catch_up_day`; real report: `grep -cE '(src|href)=|url\(|@import'` -> 0, `latest.*` byte-equal to dated copy |
| **Phase 5 - End to end** | | | |
| 5.1 | Fixtures: prepare -> briefs written by hand per INSTRUCTIONS.md -> finalize -> rerun unchanged | PASS | CLI path with fixture stand-ins: `prepare 2024-12-31: processed 15 day(s), 9 recommendation(s), 0 non-critical error(s)`; wrote `work/brief_outputs/MOMA1.json`; report contains the brief; rerun: `already up to date` / `already finalised`, sha256 listing identical |
| 5.2 | Real data, `--limit 50`, weekly then daily | PASS after fixes | see runtimes below; HTML/MD inspected: regime banner, cards with entry/stop/target %, earnings warning, guardrail, EV/FCF percentile, LLM brief section, tracking updates, open table, statistics with baseline columns, catch-up section; found and fixed bugs 2.3-2.8 |
| 5.3 | Backfill `--limit 50` + backtest 2023-01-01..2024-12-31 | PASS after fix | backfill all 5 stages 6m50s (3 GB real SEC downloads); backtest (after bug 2.9 fix): 326 recs, per-module table for all four modules, 89 s; before the fix: "no recommendations generated" |
| 5.4 | Dashboard | PASS | Streamlit `AppTest` over Live + 2 backtest runs x 8 views = 0 exceptions, 0 errors; real `streamlit run --server.headless` -> `/_stcore/health` = ok, `/` = 200 |
| **Phase 6 - Routine readiness** | | | |
| 6.1 | ROUTINE_DAILY/WEEKLY use real flags and paths, commit only `state/` `reports/`, `pull --rebase`, forbid code changes | PASS | read against `argparse` definitions: `--stage prepare|finalize`, `--date`, `--limit`, `--force`, `--dry-run` exist; `git add state reports`; `git pull --rebase origin main` before `git push`; hard rule 1 forbids touching code |
| 6.2 | Runtime estimate | PASS with risk | section 5 |
| 6.3 | `docs/ROUTINE_SMOKE_TEST.md` written | PASS | one-off cloud routine, `--base-dir /tmp/smoke`, commits only `smoke_test_results/<ts>.md` to branch `audit-smoke-test` |

## 2. Fixes made

| # | File(s) | Problem found | Fix | Regression test |
|---|---|---|---|---|
| 2.1 | `src/contracts.py`, `run_daily.py`, `src/jobs.py`, `src/universe.py`, `src/data/texts.py` | undefined `pd` in annotations, unused imports, zero-width-space literal | cleaned | ruff clean (no behaviour change) |
| 2.2 | `tests/test_foundation.py` | hard-coded `SEC_EMAIL == test@example.com` fails for anyone with a real `SEC_EMAIL` exported | compares to the environment | the test itself |
| 2.3 | `src/data/universe_source.py` | **Sector labels**: live Nasdaq screener says `Finance` (1,656 tickers), `Basic Materials`, `Telecommunications`; config sector-ETF map and guardrail expect `Financials`/`Materials`/`Communication Services` -> no sector ETF / sector excess return for ~1/4 of the market, and the guardrail's Financials debt exception never applied | `normalize_sector()` | `test_upsert_tickers_normalizes_nasdaq_sector_labels` |
| 2.4 | `src/data/universe_source.py` | `BRK/B` (Nasdaq) vs `BRK-B` (SEC map) -> NULL CIK -> no fundamentals, no Form 4/8-K matching for slash share classes | `_lookup_cik()` tries `/`->`-` and `.`->`-` | `test_upsert_tickers_finds_cik_for_slash_share_classes` |
| 2.5 | `src/data/fundamentals.py` (`quarterize`) | **TTM figures were garbage on real filings**: grouping by SEC's `fy` field (the *filing's* fiscal year, also stamped on prior-year comparatives) mixed years: AAPL TTM operating income $54B (real ~$155B), a -$64B quarter, CSCO FCF negative (real +$12.8B) -> false guardrail pass/fail | group cumulative facts by their real period start; derive a quarter only when earlier quarters tile the window; first-filed value kept (point-in-time); direct quarter preferred over a YTD difference | `test_quarterize_ignores_filing_fy_label_on_prior_year_comparatives`, `..._prefers_direct_quarter...`, `..._skips_ytd_it_cannot_derive` (+ the 18 existing) |
| 2.6 | `src/modules/b_earnings_gap_drift.py`, `src/modules/c_insider_cluster.py` | **Module B crashed every day with real data** (`'float' object has no attribute 'lower'`: NULL timing arrives as NaN); also the stored vocabulary (`BMO/AMC/NULL`, 8-K rows already carry the reaction session) was not what the module expected, so AMC events scanned the wrong sessions. Same NaN hazard for `officer_title` in Module C | NaN-safe text; 8-K-derived rows -> event date is the reaction session; calendar rows: AMC -> next session, BMO/during -> same day, unknown -> either | `test_module_b_maps_event_timing_to_reaction_session`, `test_module_b_and_c_tolerate_null_text_columns` |
| 2.7 | `src/guardrail.py`, `run_daily.py` | share count compared across a stock split reads as dilution (real: CRWD 4:1 -> "+308 % shares", PANW 2:1) -> false `fail` for a year after any split | counts put on the as_of share basis using known splits (<= as_of only); the daily job now fetches splits for the day's candidates before ranking | `test_stock_split_is_not_counted_as_dilution` (incl. split-after-as_of ignored) |
| 2.8 | `src/data_access.py` | second share class (GOOGL vs GOOG) found no fundamentals (rows stored under one ticker per CIK) -> permanent "unknown" + re-fetch every day | `get_fundamentals` also matches by CIK | `test_fundamentals_found_for_second_share_class_via_cik` |
| 2.9 | `src/universe.py` | **Backtest produced no signals**: `build_historical_universe` needed `fundamentals_summary` history, which a research DB built by `run_backfill` never has -> empty universe -> "0 recommendations" | market cap from the newest shares-outstanding fact filed by `as_of` (split-scaled), summary still preferred | `test_build_historical_universe_uses_filed_shares_when_no_summary` |
| 2.10 | `src/data/texts.py` | **8-K texts missing from LLM brief inputs for any filing from an earlier run**: `state/filings.csv` does not persist the document URLs, `get_8k_texts` skipped such filings (real: WDAY's earnings 8-K from the previous day had no text) | re-derive the URLs from the filing index page and store them | `test_get_8k_texts_rederives_urls_lost_in_the_state_round_trip`; verified live (WDAY item 2.02 text now in the brief input) |
| 2.11 | `src/trade_plan.py` | earnings flag fired for earnings whose market reaction was already behind the signal day (WDAY 8-K-derived event on the signal date) | `next_earnings` skips events whose reaction session is <= signal day | `test_next_earnings_ignores_reaction_already_behind_the_signal_day` |
| 2.12 | `src/config.py`, `run_daily.py`, `run_weekly.py`, `run_backfill.py`, `backtest/runner.py`, `dashboard/data.py`, README | no way to run the pipelines against a temporary copy of the state | `--base-dir` flag / `SWING_BASE_DIR` env (state, reports, data, work all move) | `test_base_dir_override_redirects_all_paths`, `test_entrypoints_accept_base_dir` |
| 2.13 | `templates/report.md.j2` | HTML entities (`&#9888;`, `&#9873;`) printed raw in the Markdown report | real characters | covered by `test_report*` |
| 2.14 | `src/baseline.py` | hard-coded `2.0` target multiple (unused by `simulate`, but a stray threshold) | uses `config.trade_plan.target_r_multiple` | `test_baseline.py` |
| 2.15 | tests | no tests for `update_prices`, contract signatures, several spec items | `tests/test_prices.py`, `tests/test_contract_signatures.py`, `tests/test_audit_spec.py` | — |

## 3. Decisions needed (none blocking; current behaviour is the safe/conservative reading)

* **D1 - Breadth threshold.** PLAN s.6 says "Favorable: both conditions OK" but never says what breadth counts as OK. Code: >= 50 % of the universe above its 50-day SMA, hard-coded in `src/regime.py`. Real run: 30 % -> Caution (SPY above 200-SMA). Please confirm 50 % (and say whether to move it into `config.yaml`).
* **D2 - Guardrail with partial data.** PLAN s.8: "missing data always resolves to unknown". Code (Bot 7's reading): an unevaluable check is skipped; status is `unknown` only when *nothing* could be evaluated, otherwise `pass` if the evaluable checks pass (e.g. ASML/HSBC show `pass` on the share-count check alone). Only `fail` drops a name, so ranking is unaffected; the badge in the report is. Stricter reading = `unknown` whenever any check lacks data.
* **D3 - Repeat signals.** The same ticker can produce a new recommendation on consecutive days (fixture run: CTRL12 on 12-11, 12-12, 12-13), each tracked separately. The spec dedups only within a day. Overlapping trades inflate counts and correlate statistics. Options: leave, or add a cool-down while a rec for the ticker is open.
* **D4 - Overlap component.** PLAN s.9 says setup, RS and overlap are each converted to a 0-100 *percentile*; code percentiles setup and RS but uses the stated overlap *score* (0/70/100) directly (Bot 4's reading; documented). Confirm.
* **D5 - "GICS industry".** The per-industry cap uses Nasdaq's `industry` field (e.g. "Computer Software: Prepackaged Software"), which is finer than GICS and not GICS. The cap is therefore weaker than intended for large sectors. No free GICS source is wired in.

## 4. Remaining risks that cannot be verified locally

* **Cloud/Linux/network behaviour**: Yahoo (yfinance) and Nasdaq APIs can rate-limit or block datacenter IPs; Google News RSS likewise; SEC rate limits. Local runs were from a residential IP. -> run `docs/ROUTINE_SMOKE_TEST.md`.
* **Telegram delivery** was never run with real credentials (only the unconfigured skip path and mocked HTTP).
* **Routine time limits and git push permissions** of the Claude Code cloud environment are unknown.
* **EDGAR scope**: the daily pass fetches Form 4 / 8-K for every ticker in `tickers` (~7,000 listed companies), not just the ~2,000 universe: ~110-135 s per session here; earnings-season days will be longer and a multi-day catch-up scales linearly. Restricting `load_cik_map` to the universe would cut it ~3x (not done: optimisation, not a defect).
* **News relevance**: Google News search by company name returns unrelated items (e.g. "Madison Pacific Properties" for MPC, "Murata" for MFG). The brief rules (input-only facts) limit harm, but briefs can mention noise; the agent was told to prefer short correct briefs.
* **Fundamentals coverage**: frames-based weekly summary resolves ~70 % of FCF/shares and ~50 % of debt for large caps; foreign filers (ASML, HSBC) have no US-GAAP facts and resolve to partial/unknown.
* **Backtest** was validated as a pipeline on 62 tickers x 2 years (survivorship-biased, as documented); the full-universe, 2014+ backfill (hours, ~3 GB + all insider data sets) was not run. First-time history for brief texts accumulates only from the first live run (30-day window).
* **State growth**: `tickers.csv` (~0.8 MB) is rewritten weekly and 120-day insider/filings windows for ~7,000 CIKs are committed daily (a few MB, deterministic ordering keeps diffs small); watch repo size after a few months.
* Real market holidays / early closes in cloud runs were covered by unit tests and calendar logic, not by a live occurrence.

## 5. Runtimes (this machine, 9 MB/s link) and scaling

| Step | Measured | Notes |
|---|---|---|
| `run_weekly.py --limit 50` | 1m54s | prices 62 tickers 5 s, universe 3 s, fundamentals frames ~22 s, Nasdaq earnings calendar 36 requests ~74 s |
| `run_daily.py --stage prepare --limit 50`, 1 session | 2m20s | EDGAR day ~2 min dominates |
| `prepare --limit 400`, 1 session | 3m09s-3m22s | prices 43 s (403 tickers), EDGAR 112-134 s, earnings calendar 12-15 s, rank+guardrail fetch 10 s, brief inputs 10 s |
| `prepare --limit 50`, catch-up of 6 sessions | 18m03s | ~3 min/session (EDGAR) |
| `finalize` | 2-7 s | |
| `run_backfill.py --limit 50` (all stages from 2021) | 6m50s | 1.5 GB submissions + 1.4 GB companyfacts + insider sets |
| backtest 2023-2024, 62 tickers | 89 s | 326 recs, 978 baselines |

Scaling to ~2,000 universe tickers: prices ~4-5 min (+ retries), EDGAR 2-6 min per session, guardrail+news ~1-2 min -> **one session ~10-15 min**, weekly **~10-15 min**; a 3-session catch-up ~20-30 min. Nothing here looks unreasonable, but the catch-up case and earnings-season EDGAR days are the ones to watch against the routine's time limit.

## 6. Re-run instructions

```bash
pytest -q                                              # 394 passed, 2 skipped
python run_weekly.py --limit 50 --base-dir /tmp/t      # real data into a temp dir
python run_daily.py --stage prepare --limit 50 --base-dir /tmp/t
python run_daily.py --stage finalize --base-dir /tmp/t
```

## 7. Decisions applied (Bot 9)

Final suite after the changes: `pytest` -> **394 passed, 2 skipped** (was 360 + 2). New tests: `tests/test_decisions.py` (D1-D3, R1, R3), `tests/e2e/test_catchup_limit.py` (R2). The fixture end-to-end tests (`tests/e2e/`) pass.

| # | Decision | What changed | Regression tests |
|---|---|---|---|
| D1 | Breadth threshold 50 % in config | `regime.breadth_threshold_pct: 50` in `config.yaml`; read by `src/regime.py` and by the report's breadth flag (the duplicate constant in `src/report.py` is gone) | `test_breadth_threshold_is_in_config_and_still_50`, `test_regime_reads_breadth_threshold_from_config` (50 -> Favorable, 50.01 -> Caution), `test_report_breadth_ok_uses_config_threshold` |
| D2 | `pass_partial` | `guardrail.evaluate` returns `pass_partial` when the evaluable checks pass but some could not be evaluated; reasons = `not evaluated: <check>`. Ranking only drops `fail`, so it is unchanged. Report: chip/line "Pass (partial)" naming the missing checks (HTML + MD) | `test_guardrail_pass_with_unevaluated_check_is_pass_partial`, `test_guardrail_missing_data_still_never_fails`, `test_ranking_treats_pass_partial_exactly_like_pass`, `test_report_shows_pass_partial_with_missing_checks`. Three older tests that seeded no share/debt data now expect `pass_partial` |
| D3 | No new recommendation while PENDING/ACTIVE | new `src/repeats.py`, called from `rank()`: the repeat is stored in the live recommendation's `details["repeat_signals"]` (`[{date, modules}]`, idempotent per date); no new row, no baselines. "Live" = the earlier recommendation re-simulated on prices up to the new signal date (not the stored status, which is stale during catch-up and backtests). The backtest runner now stores recommendations day by day so the same rule applies. Report: a Note column ("still valid from <signal date> (repeat signal ...)") in the catch-up and open tables | `test_repeat_signal_while_pending_is_recorded_not_recommended`, `..._accumulate_and_reprocessing_a_day_is_idempotent`, `test_other_tickers_are_unaffected...`, `test_new_recommendation_allowed_once_previous_expired / _stopped`, `test_catch_up_uses_prices_not_the_stale_status_column`, `test_backtest_applies_the_same_repeat_rule`, `test_report_shows_still_valid_from_signal_date`. `test_backtest.py` / `test_dashboard.py` fake modules now signal two different tickers (the same ticker twice is a repeat by design) |
| D4 | Overlap stays 0/70/100 | `docs/PLAN.md` s.9 says so (no code change) | existing ranking tests |
| D5 | Nasdaq industry for the cap | `docs/PLAN.md` s.9 says so (no code change) | existing ranking tests |
| R1 | EDGAR daily pass: universe CIKs only | `edgar_daily.load_universe_cik_map` (latest `universe_snapshots`; all share classes of a universe CIK kept; no snapshot -> falls back to all tickers with a warning). Backfill still uses all tickers | `test_edgar_daily_only_fetches_universe_ciks`, `test_universe_cik_map_keeps_every_share_class...` |
| R2 | Catch-up cap | `daily.max_catchup_days: 5`; `prepare` processes the oldest N missed days, records the remainder in `job_log` (job `catchup`), `finalize` passes it to the report: "Catch-up in progress, N days remaining" (HTML, MD, Telegram digest); prepare's printed summary mentions it | `test_catch_up_is_limited_and_reported` (cap 2 of 3 days, note shown, next run finishes and the note disappears) |
| R3 | News relevance | `news.is_relevant`: title must contain the ticker as a whole word (case-sensitive) or the cleaned company name / its first non-generic word; the count filtered is logged per ticker | `test_news_relevance` (12 cases incl. the MPC and MFG noise from the audit), `test_get_news_drops_irrelevant_items_and_logs_the_count` |

**R1 request count (real day, 2026-10-08, measured).** The daily form index for that session lists 477 distinct Form 4 / 8-K accessions across all filers. Requests the daily pass makes (one per matching accession: Form 4 submission or 8-K index page):

| Scope | CIKs | Form 4 | 8-K | Total requests |
|---|---|---|---|---|
| Before R1: every listed ticker with a CIK | 5,897 | 270 | 124 | **394** |
| After R1: latest universe snapshot (`--limit 50` trial: 49 tickers) | 47 | 43 | 0 | **43** |

The "after" figure equals what `process_day` actually fetched in the real run (43 Form 4, 0 errors). The 9x reduction is specific to the 49-ticker trial universe; with the real ~2,000-ticker universe the saving is smaller (the earlier ~3x estimate in section 4 still stands, not re-measured).

**Real-data run (`--limit 50`, temporary `--base-dir`, Windows, 2026-10-09).** `run_weekly.py --limit 50`: universe 49 tickers, fundamentals summary 49 rows, 4,386 upcoming-earnings rows, 0 non-critical errors. `run_daily.py --stage prepare --limit 50`: session 2026-10-08, 1 day processed, 0 recommendations, 0 errors. `--stage finalize`: report built (regime Favorable, SPY above its 200-day average, 65 % breadth vs the 50 % config threshold). Repo `state/` and `reports/` untouched. Because that day produced no recommendation, `pass_partial`, the repeat-signal note and the catch-up cap were **not** exercised on real data (fixture/unit tests only); the news filter was not exercised either (no brief inputs for a day without recommendations).

Behaviour notes: the stored status of a brand-new recommendation is `PENDING` (upper case) while the tracker writes lower case; the repeat rule does not depend on that column. The backtest guardrail still tries the live companyfacts endpoint for fixture tickers (404, logged and treated as unknown); unchanged.

VERDICT: READY — all local checks passed. Final confirmation: run docs/ROUTINE_SMOKE_TEST.md as a cloud routine.
