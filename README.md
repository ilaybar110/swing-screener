# Swing Screener

Finds swing-trade candidates (holding period: days to ~3 months) among liquid US stocks and
automatically tracks the hypothetical outcome of **every** candidate it ever recommended,
whether or not you acted on it. No broker connection, no position sizing, no account
management - it is a research and tracking tool. Zero running cost.

The behavioural spec lives in [docs/PLAN.md](docs/PLAN.md); this README is the operating manual.

## How it works

```
 Sunday 04:00 UTC        Tue-Sat 04:00 UTC
 weekly routine          daily routine (Claude Code cloud Routines, fresh clone each run)
 ──────────────          ─────────────────────────────────────────────────────────────────
 universe refresh        prepare : import state/ -> run.db, for EVERY missed US session in order:
 fundamentals summary              EDGAR filings -> market regime -> 4 strategy modules -> ranking
 earnings (35 days)                -> save recommendations -> baselines; prices downloaded once;
 export state/                     tracking re-simulation; LLM brief inputs; export state/
                         the routine agent writes one short JSON brief per top stock
                         finalize: merge briefs -> build report -> export state/ -> Telegram
                         commit state/ + reports/ and push to main
```

* **State is CSV in git.** `state/` holds everything that must persist (tickers, universe
  snapshots, regime log, recent insider trades / filings / earnings dates, the latest
  fundamentals summary, `job_log`, and month-sharded `recommendations`,
  `recommendation_results` and `baselines`). Every run rebuilds a throw-away SQLite database
  (`data/run.db`, git-ignored) from it, does its work, and exports the CSVs back
  deterministically (sorted rows, fixed columns) so diffs stay small.
* **Tracking is stateless.** Each run re-simulates every non-final recommendation from its
  signal date on fresh prices through one pure function (`src/tracker.py: simulate`). A
  result is written once, when final, and never recomputed.
* **Four modules** (A momentum pullback, B earnings gap drift, C insider cluster buying,
  D base breakout) all run every day in every regime; the regime (Favorable / Caution /
  Unfavorable) only changes how many recommendations the report shows (10 / 5 / none).
  A fundamental guardrail drops names with deteriorating fundamentals; ranking blends setup
  quality, relative strength and module overlap.
* **Control group.** For every recommendation, three randomly drawn universe stocks are
  tracked with the same stop distance and exit rules (`baselines`), so statistics show
  whether the screener beats chance.
* **LLM briefs** are written by Claude inside the daily routine from input files only
  (8-K items, Exhibit 99.1, 14 days of news) and merged back; a missing or invalid brief
  just omits that section.
* **Idempotent and self-healing.** If a run is missed, the next one processes every missed
  session in chronological order. Re-running a stage that is already done changes nothing.

Where things live:

| What | Where |
|---|---|
| Daily/weekly reports | `reports/YYYY-MM-DD.html` (self-contained) + `.md`; `reports/latest.html` / `latest.md` mirror the newest |
| Live state (committed) | `state/*.csv`, `state/recommendations/YYYY-MM.csv`, `state/recommendation_results/…`, `state/baselines/…` |
| Scratch DB per run (ignored) | `data/run.db` |
| Permanent research DB (ignored, local only) | `data/research.db` |
| Raw download cache, logs (ignored) | `data/raw/`, `data/logs/swing_screener.log` |
| LLM brief inputs / outputs (ignored) | `work/brief_inputs/`, `work/brief_outputs/` |
| Configuration | `config.yaml` (all strategy parameters; fixed before testing - do not tune on results) |

## Local setup

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash;  Linux/macOS: source .venv/bin/activate
pip install -r requirements-local.txt   # runtime + dashboard + pytest
cp .env.example .env                  # then edit: SEC_EMAIL is required for any EDGAR access
pytest                                # full test suite, no network needed (~3 min)
```

`.env` is read by every entrypoint (it is git-ignored; never commit it):

| Variable | Needed for |
|---|---|
| `SEC_EMAIL` | **required** - contact address SEC requires in the User-Agent of every EDGAR request |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional - Telegram report/alert delivery; silently skipped when unset |

## Running things locally

### Research backfill (once, then occasionally to top up)

```bash
python run_backfill.py --limit 50     # trial: 50 largest companies into data/research_trial.db
python run_backfill.py                # the real thing, into data/research.db
```

Stages: tickers -> prices from 2014 (+ splits) -> EDGAR bulk (filings, earnings events,
insider purchases; ~1.5 GB download) -> fundamentals bulk (`companyfacts.zip`, ~1 GB) ->
market regime for every trading day. It is **resumable** (rerun after any interruption; it
only fetches what is missing or stale), prints done/total, elapsed time and an ETA, and
`--stages prices,regime` runs a subset. Expect several hours the first time, dominated by
the SEC downloads and the 3,000-ticker price history. Network and disk are the limits; the
SEC is throttled to 4 requests/second.

### Backtest

```bash
python -m backtest.runner --start 2015-01-01 --end 2024-12-31 --notes "first run"
```

Replays the same modules, ranking and `simulate()` against `data/research.db`
point-in-time (by filing date). Suggested windows: 2015-2019, 2020-2022, 2023+. Every
result is an **upper bound** on real performance: the universe only contains tickers that
exist today (survivorship bias), as the printed warning says. Runs are stored with a config
hash and show up in the dashboard.

### Dashboard

```bash
./open_dashboard.sh          # git pull, activate .venv, streamlit run dashboard/app.py
```

Reads `state/` (live results) and `data/research.db` (backtests) and never writes to
either. Run it from Git Bash on Windows or any shell on Linux/macOS.

### Dry runs

```bash
python run_weekly.py --dry-run --limit 50
python run_daily.py  --dry-run --limit 50     # whole pipeline, nothing written to state/ or reports/, no Telegram
```

`--dry-run` still downloads data and uses a scratch DB (`data/run_dry.db`). Other flags:
`--date YYYY-MM-DD` (treat that session as the latest), `--stage prepare|finalize|all`,
`--force` (redo an already-complete day), `--limit N` (trial runs), `--base-dir DIR` (put
`state/`, `reports/`, `data/` and `work/` under DIR instead of the repo - use a temporary copy
for realistic trial runs that must not touch the committed state; same as setting `SWING_BASE_DIR`;
`run_weekly.py`, `run_daily.py`, `run_backfill.py` and `python -m backtest.runner` all accept it).

## Setting up the cloud routines

You need two Claude Code cloud Routines (create them from claude.ai/code). The exact
prompts and settings are in:

* [docs/ROUTINE_DAILY.md](docs/ROUTINE_DAILY.md) - **Tuesday-Saturday ~04:00 UTC**
* [docs/ROUTINE_WEEKLY.md](docs/ROUTINE_WEEKLY.md) - **Sunday ~04:00 UTC**

For both routines:

1. Repository `ilaybar110/swing-screener`, branch `main`.
2. **Environment variables:** `SEC_EMAIL` (required), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (optional).
3. **Network access: Full** (Yahoo Finance, Nasdaq, SEC EDGAR, Google News, Telegram).
4. **Allow pushes to `main`** (the routine commits `state/` and `reports/` and pushes).
5. Paste the prompt from the matching `docs/ROUTINE_*.md` file as-is.

Run the weekly routine once first (trigger it manually) so a universe snapshot exists; then
enable the daily one. The routine agent only runs the scripts, writes the brief JSON files
and commits - its prompt forbids it from changing code, "fixing" errors, or printing
secrets. Failures that make a run meaningless (no prices at all, EDGAR completely down, no
report) send a Telegram alert and make the script exit non-zero; everything else (one
ticker's data, news, a brief, Telegram) is logged and the run carries on.

Creating a Telegram bot: talk to `@BotFather`, `/newbot`, copy the token; send your bot a
message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Enabling and disabling modules

Edit `config.yaml`:

```yaml
modules:
  c_insider_cluster:
    enabled: false      # a disabled module is not scanned, tracked or ranked
```

Module parameters live in the same file and are meant to be fixed *before* looking at any
statistics (no in-sample tuning). Changing a parameter affects new signals only; recommendations
already in `state/` keep the levels they were created with.

## Repository map

```
run_daily.py  run_weekly.py  run_backfill.py   entrypoints (this README / docs/ROUTINE_*.md)
open_dashboard.sh                              local dashboard launcher
src/            pipeline: data sources (src/data), modules, ranking, trade plan, tracker,
                baseline, stats, regime, universe, guardrail, report, LLM brief I/O, state_io
backtest/       local backtest runner         dashboard/   Streamlit app
notify/         Telegram                      templates/   report HTML/Markdown templates
state/ reports/ committed output              tests/       unit tests + tests/e2e
docs/           PLAN.md (spec), CONTRACTS.md, OWNERSHIP.md, ROUTINE_*.md, status/BOT_*.md
```

## Known limitations

* Backtests carry survivorship bias (see above). Treat them as upper bounds.
* The daily EDGAR pass reads every Form 4 / 8-K of all listed companies in `tickers`, not
  only the universe (a few minutes per session).
* Prices are not persisted in `state/`; each routine run re-downloads about 300 trading
  days for the universe.
* Yahoo Finance and Nasdaq endpoints are unofficial and can change or throttle; failures are
  logged per ticker and shown in the report's data-quality note.
* Not investment advice; no guarantee of profitability.
