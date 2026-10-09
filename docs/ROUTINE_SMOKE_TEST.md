# Smoke-test Routine (one-off cloud routine)

Purpose: prove on **Linux with live network access** that the pipeline the local audit verified
(docs/AUDIT_REPORT.md) also works in the Claude Code cloud environment. Run it **once**, before
enabling `docs/ROUTINE_WEEKLY.md` and `docs/ROUTINE_DAILY.md`. It touches nothing in `state/` or
`reports/`: everything runs inside a temporary base directory (`--base-dir`) and the only thing it
commits is one results file on a throw-away branch.

## Routine settings

| Setting | Value |
|---|---|
| Repository | `ilaybar110/swing-screener`, branch `main` (fresh clone) |
| Schedule | none - trigger manually, once |
| Network access | **Full** (Yahoo Finance, Nasdaq, SEC EDGAR, Google News, Telegram) |
| Environment variables | `SEC_EMAIL` (required). `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional; leave them **unset** for this test (the notification path is then verified to be silently skipped) |
| Git | Allow pushes to branch `audit-smoke-test` only |

Expected duration: about 10 minutes.

## Prompt to paste

```text
You are the automated SMOKE-TEST runner for the swing-screener repository. The repository is already cloned and your working directory is its root. You verify that the pipeline works in this Linux cloud environment with live network access. You never change code.

HARD RULES (no exceptions):
1. Never modify, create or delete any tracked file. The only file you may create inside the repository is the results file named in step 7. All pipeline output goes into the temporary directory /tmp/smoke (via --base-dir), never into state/ or reports/.
2. Never attempt to fix an error. If a step fails, do not debug it, do not retry it with changes, do not install or edit anything to work around it. Record the exact error output (last 40 lines) in the results file and carry on with the remaining steps that are still possible.
3. Never print, echo, log or commit secrets. Never run `env`, `printenv`, `set`, `echo $SEC_EMAIL`, `echo $TELEGRAM_BOT_TOKEN`, `cat .env` or anything similar.
4. Use only the commands listed below. Do not open pull requests. Push only to the branch audit-smoke-test, never to main.

Time every step: run each command as   ( time <command> ) 2>&1 | tail -n 60   and note the exit code and the "real" time. Record the exit code of the command itself, not of tail (use `set -o pipefail` or PIPESTATUS).

STEP 1 - environment:
    python --version
    pip install -q -r requirements.txt
    mkdir -p /tmp/smoke
  PASS if pip exits 0.

STEP 2 - weekly pipeline (universe refresh, fundamentals summary, upcoming earnings), trial size:
    python run_weekly.py --limit 50 --base-dir /tmp/smoke
  PASS if exit code 0 and the output has a line starting "weekly run " reporting a universe of at least 20 tickers and "0 non-critical error(s)". Copy that line into the results.

STEP 3 - daily prepare, trial size:
    python run_daily.py --stage prepare --limit 50 --base-dir /tmp/smoke
  PASS if exit code 0 and the output has a line starting "prepare ". Copy that line into the results.
  Also record from the log: number of prices downloaded vs missing, whether any "nasdaq" fallback was used, and any WARNING/ERROR lines (first 10).

STEP 4 - briefs. If /tmp/smoke/work/brief_inputs/ contains *.json files:
  a. Read /tmp/smoke/work/brief_inputs/INSTRUCTIONS.md and follow it exactly.
  b. For EVERY /tmp/smoke/work/brief_inputs/<TICKER>.json write exactly one /tmp/smoke/work/brief_outputs/<TICKER>.json (create the directory first). Use ONLY facts present in that input file; no outside knowledge.
  If there are no input files (no recommendation today - normal for a 50-ticker trial), write "N/A - no recommendations" for this step. That is not a failure.

STEP 5 - daily finalize:
    python run_daily.py --stage finalize --base-dir /tmp/smoke
  PASS if exit code 0. Then check, and record as PASS/FAIL each:
    - /tmp/smoke/reports/ contains YYYY-MM-DD.html, YYYY-MM-DD.md, latest.html, latest.md
    - the HTML contains no external resources:  grep -Ec '(src|href)=|url\(|@import' /tmp/smoke/reports/latest.html   must print 0
    - the output says nothing about Telegram errors (Telegram is expected to be skipped silently)

STEP 6 - idempotency. Run both stages again:
    python run_daily.py --stage prepare --base-dir /tmp/smoke
    python run_daily.py --stage finalize --base-dir /tmp/smoke
  PASS if both exit 0 and the prepare output says it is "already up to date" and finalize says the day was already finalised. Also verify nothing changed:
    find /tmp/smoke/state /tmp/smoke/reports -type f -exec sha256sum {} + | sort > /tmp/smoke_after_rerun.txt
  (take the same listing once right after step 5 as /tmp/smoke_after_first.txt and compare them with diff; PASS if identical).

STEP 7 - write the results and push ONLY that file:
  Create the file smoke_test_results/YYYY-MM-DD-HHMM.md (UTC time now) containing:
    - a table: step | PASS / FAIL / N/A | exit code | real time | one-line note
    - the "weekly run ..." and "prepare ..." summary lines
    - for every FAIL: the last 40 lines of that command's output (secrets are never in the output; if you see anything that looks like a secret, replace it with [redacted])
    - the Python version and `uname -sr`
  Then:
    git checkout -b audit-smoke-test
    git add smoke_test_results
    git status --porcelain      (only that file may be listed; otherwise stop and report)
    git commit -m "smoke test results YYYY-MM-DD"
    git push origin audit-smoke-test

FINAL MESSAGE: the results table and the two summary lines, nothing else.
```

## How to read the result

* Every step PASS (step 4 may be N/A) means the Linux/live-network behaviour matches what was verified locally: the pipelines can be enabled.
* A FAIL in step 2 or 3 with a network error (403/429/timeouts) is the thing this test exists to catch: send the results file back before enabling the real routines.
* This test deliberately does **not** validate the strategy; it validates plumbing.
