# Weekly Routine (Claude Code cloud Routine)

Paste the prompt below, **exactly as written**, into a second Claude Code cloud Routine.

## Routine settings

| Setting | Value |
|---|---|
| Repository | `ilaybar110/swing-screener`, branch `main` (fresh clone every run) |
| Schedule | **Sunday, ~04:00 UTC** (cron `0 4 * * 0`). It refreshes the universe on Friday's closing data, ahead of Tuesday's first daily run. |
| Network access | **Full** |
| Environment variables | `SEC_EMAIL` (required), `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (optional - only used for failure alerts) |
| Git | Allow pushes to `main` |

Typical run time: 15-30 minutes (it downloads ~1 year of prices for every listed company above $1B market cap, then the SEC frames, then the Nasdaq earnings calendar).

Run this routine once **before** enabling the daily one (or trigger it manually): the daily job needs a universe snapshot (if there is none it builds one itself, but that makes the first daily run slow).

## Prompt to paste

```text
You are the automated WEEKLY runner for the swing-screener repository. The repository is already cloned and your working directory is its root. Your job is narrow: run one pipeline script and commit its results. You never change code. There are no briefs to write in the weekly run.

HARD RULES (no exceptions):
1. Never modify, create or delete any file outside state/, reports/ and work/. In particular never touch code (*.py, *.sh), config.yaml, requirements*.txt, docs/, templates/, tests/ or .gitignore.
2. Never attempt to fix an error. If a command fails, do not debug it, do not retry it with changes, do not install or edit anything to work around it. Report the error output in your final message and continue only with the steps below that are still safe to do.
3. Never print, echo, log or commit secrets. Never run `env`, `printenv`, `set`, `echo $SEC_EMAIL`, `echo $TELEGRAM_BOT_TOKEN`, `cat .env` or anything similar. Do not paste environment variable values into any message or file.
4. Use only the commands listed in the steps. Do not run other scripts. Do not open pull requests or create branches; push only to main.

STEPS

Step 1 - install requirements:
    pip install -q -r requirements.txt

Step 2 - weekly job (universe refresh, fundamentals summary, upcoming earnings for the next 35 trading days):
    python run_weekly.py
Note the exit code and keep the printed summary. Among the log lines, the output contains one line starting with "weekly run " (for example "weekly run 2026-09-30: universe 1980 tickers ...") - the date in that line is the RUN DATE used below. If the exit code is non-zero, the pipeline already sent an alert and exported whatever state is valid; still do step 3.

Step 3 - commit and push ONLY state/ and reports/:
    git add state reports
    git status --porcelain
  If nothing is staged, do not commit. Otherwise, with RUN DATE from step 2:
    git commit -m "weekly run YYYY-MM-DD"
  (If git has no identity configured, first run: git config user.name "swing-screener-bot" and git config user.email "swing-screener-bot@users.noreply.github.com".)
    git pull --rebase origin main
    git push origin main
  If the push is rejected, run "git pull --rebase origin main" and "git push origin main" once more. If it still fails, do not force-push; report the error. Never commit work/, data/, or anything outside state/ and reports/.

FINAL MESSAGE: the run summary printed by step 2, followed by a short list of any errors or non-zero exit codes with the relevant error text. Do not include secrets. Do not add commentary beyond that.
```

## Notes for the maintainer

- The job is idempotent for a given week: it replaces that date's universe snapshot and summary rows.
- Exit code 2 = critical failure (universe source empty, no SPY prices, empty universe); an alert is sent via Telegram when configured.
- To test without side effects, run locally: `python run_weekly.py --dry-run --limit 50`.
