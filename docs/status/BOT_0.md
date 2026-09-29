# Bot 0 (Foundation) — status

## What was built

- `docs/PLAN.md` — full spec transcribed from the task brief (source of truth for
  behavior).
- `docs/CONTRACTS.md`, `docs/OWNERSHIP.md`, `docs/BOT_RULES.md`,
  `docs/CHANGE_REQUESTS.md` — coordination docs for bots 1–7.
- Full repo skeleton: `src/`, `src/utils/`, `src/data/`, `src/modules/`, `src/llm/`,
  `backtest/`, `dashboard/`, `notify/`, `templates/`, `tests/`, `tests/fixtures/`,
  `state/`, `reports/`, `docs/status/`.
- `config.yaml` + `src/config.py` — every parameter from the spec, grouped and
  pydantic-validated; secrets (`SEC_EMAIL`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`) come from environment variables only.
- `src/db.py` — full SQLite schema (all 16 tables from the spec) with a simple
  numbered-migration system (`init_db()` is idempotent).
- `src/contracts.py` — `Candidate`, `Recommendation`, `GuardrailResult`,
  `TrackResult`, `LLMBrief` dataclasses, the `StrategyModule` protocol, and one
  docstring-only stub function per cross-bot entry point (`_botN_*`) describing
  exact signatures for bots 1–6.
- `src/data_access.py` — `DataAccess`, the shared read-only query layer (prices,
  splits, universe, sector ETF map, regime, fundamentals, fundamentals summary,
  insider trades, earnings events/upcoming), all point-in-time correct.
- `src/state_io.py` — `import_state()` / `export_state()` between `state/` CSVs and
  either DB. Handles retention windows (insider_trades 120d, earnings_dates 180d,
  filings 120d), latest-snapshot-only for `fundamentals_summary`, and month-sharded
  CSVs for `recommendations` / `recommendation_results` / `baselines` (live source
  only). Deterministic output (sorted rows, fixed column order, UTF-8, `\n` line
  endings, `json.dumps(..., sort_keys=True)` for JSON columns) — round-trip tested.
- `src/utils/logging.py`, `src/utils/calendar.py` (NYSE calendar via
  pandas_market_calendars), `src/utils/http.py` (retrying session factory).
- `src/data/raw_cache.py` (gzipped cache keyed by URL/string) and
  `src/data/edgar_client.py` (User-Agent from `SEC_EMAIL`, 4 req/s throttle, cache,
  `get_cik_map`, `get_submissions`, `get_companyfacts`, `get_frames`,
  `latest_published_daily_index` with backward walk).
- `tests/fixtures/generate_fixtures.py` — deterministic (seed 424242) synthetic
  fixture: SPY + 11 sector ETFs + 30 tickers over 2022-01-03..2024-12-31, with
  engineered triggers for modules A–D, one near-miss per module, and a split during
  an open trade (`SPLIT1`). Produces `tests/fixtures/fixture.db` and
  `tests/fixtures/state/`. Ticker-by-ticker mapping documented in
  `docs/CONTRACTS.md`.
- `tests/conftest.py` — `fixture_conn` (read-only), `writable_fixture_conn` (tmp_path
  copy), `data_access`, `fixture_state_dir`.
- `tests/test_state_io.py` (round-trip + fixture-freshness check) and
  `tests/test_foundation.py` (config/db/calendar/data_access smoke tests).

## How to run

```bash
python -m venv .venv && source .venv/Scripts/activate   # or .venv/bin/activate on Linux
pip install -r requirements.txt
cp .env.example .env   # set SEC_EMAIL
python -m tests.fixtures.generate_fixtures   # regenerate fixture.db + fixture state/
pytest -q
```

All 10 tests pass locally (Windows, Python 3.13, venv per above).

## Known issues / assumptions

- `pyproject.toml` was intentionally not added; nothing needs packaging yet.
- Fixture module-trigger shapes (`MOMA1`..`MOMD1`, `NEGA1`..`NEGD1`) were engineered
  by hand against the module rules in `docs/PLAN.md` but have **not** been verified
  against real module implementations (owned by Bot 4, not yet written). If a
  module's real logic doesn't fire on its intended fixture ticker, file a
  `docs/CHANGE_REQUESTS.md` entry against `tests/fixtures/generate_fixtures.py`
  rather than hand-editing `fixture.db`/`state/`.
- `DataAccess.get_sector_etf` calls `load_config()` internally rather than taking a
  config object, to keep the constructor signature (`DataAccess(conn)`) exactly as
  specified. If this causes friction (e.g. in tests wanting a different config),
  raise a change request.
- `src/config.py`'s `load_config()` resolves relative `paths.*` against the repo
  root by default; tests/backtests that want a sandboxed `data/`/`state/` should
  pass `base_dir=tmp_path`.
- The `cloud-connectivity-test` branch was left untouched, as instructed.
- This repo was empty when I started (README.md only); I cloned
  https://github.com/ilaybar110/swing-screener as instructed by the user mid-task,
  confirmed on `main`, and built everything on top of that.

## Not built (by design — owned by other bots)

Any file listed under Bots 1–7 in `docs/OWNERSHIP.md`: strategy modules, ranking,
trade plan, tracker/baseline/stats/backtest, guardrail/valuation/fundamentals,
EDGAR daily/bulk/form4/texts/news, LLM briefs, report/templates/notify, and the
`run_daily.py`/`run_weekly.py`/`run_backfill.py` entrypoints.
