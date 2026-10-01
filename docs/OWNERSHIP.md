# Ownership

Each bot edits only the files it owns below. Shared files (owned by Bot 0) are
never edited directly by other bots — see docs/BOT_RULES.md rule 2 for the
append-a-change-request workaround.

## Bot 0 (Foundation)
Everything not listed under another bot: skeleton, `config.yaml` / `src/config.py`,
`src/db.py`, `src/contracts.py`, `src/data_access.py`, `src/state_io.py`,
`src/utils/*`, `src/data/edgar_client.py`, `src/data/raw_cache.py`,
`tests/fixtures/*`, `tests/conftest.py`, `docs/*` (this set).

## Bot 1 — Prices, universe, regime, earnings calendar
- `src/data/prices.py`
- `src/data/universe_source.py`
- `src/universe.py`
- `src/regime.py`
- `src/data/earnings.py`
- `tests/test_prices.py`, `tests/test_universe.py`, `tests/test_regime.py`

## Bot 2 — Fundamentals and guardrail
- `src/data/fundamentals.py`
- `src/data/xbrl_tags.py`
- `src/guardrail.py`
- `src/valuation.py`
- `tests/test_fundamentals.py`, `tests/test_guardrail.py`, `tests/test_valuation.py`

## Bot 3 — EDGAR filings, insider forms, text, news
- `src/data/edgar_daily.py`
- `src/data/edgar_bulk.py`
- `src/data/form4.py`
- `src/data/texts.py`
- `src/data/news.py`
- `tests/test_edgar_daily.py`, `tests/test_form4.py`, `tests/test_texts.py`, `tests/test_news.py`

## Bot 4 — Strategy modules, ranking, trade plan
- `src/modules/*` (module implementations; `src/modules/__init__.py`'s
  `get_enabled_modules` may be extended but keep the docstring contract)
- `src/indicators.py`
- `src/ranking.py`
- `src/trade_plan.py`
- `tests/test_modules_*.py`, `tests/test_ranking.py`, `tests/test_trade_plan.py`

## Bot 5 — Tracking, baseline, stats, backtest, dashboard
- `src/tracker.py`
- `src/baseline.py`
- `src/stats.py`
- `backtest/*`
- `dashboard/*`
- `tests/test_tracker.py`, `tests/test_baseline.py`, `tests/test_stats.py`, `tests/test_backtest.py`

## Bot 6 — LLM briefs, report, notify
- `src/llm/*`
- `src/report.py`
- `templates/*`
- `notify/*`
- `tests/test_llm_brief_io.py`, `tests/test_report.py`, `tests/test_notify.py`

## Bot 7 — Entrypoints and ops
- `run_daily.py`
- `run_weekly.py`
- `run_backfill.py`
- `open_dashboard.sh`
- `docs/ROUTINE_*.md`
- `README.md`
- `tests/e2e/*`
- `src/jobs.py` (shared plumbing of the entrypoints: job_log helper, session calendar, price plan)
- `requirements-local.txt`

Bot 7 also wrote the files Bots 1/2 never delivered: `src/data/prices.py`, `src/guardrail.py`,
`src/valuation.py` (see docs/CHANGE_REQUESTS.md).

Each bot owns `tests/test_<its modules>.py` as listed above and may add more test
files under the same naming convention.
