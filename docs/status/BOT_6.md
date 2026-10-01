# Bot 6 (LLM briefs, report, notify) - status

## What was built

- `src/llm/brief_io.py` + `src/llm/brief_schema.json`
  - `write_inputs(conn, recs, as_of, out_dir="work/brief_inputs", *, config=None, get_8k_texts=None, get_news=None) -> list[Path]`:
    one `<TICKER>.json` per `in_report` rec (top `llm.briefs_top_n` by rank), holding `ticker, company, as_of, rec_id,
    truncated, filings_8k, news`. Trimmed to `llm.max_input_tokens_per_stock` (chars/4): EX-99.1 first, then item
    text, newest first; ~20% of the budget is reserved for headlines (unused filing budget flows to news); a text
    that doesn't fit is cut and marked `[truncated]`. Also writes `INSTRUCTIONS.md` (exact output contract + hard
    rules) and deletes stale `*.json` inputs. `get_8k_texts`/`get_news` default to Bot 3's functions (stubbable).
  - `merge_outputs(conn, in_dir="work/brief_outputs", *, inputs_dir=None) -> report dict`
    `{"merged", "invalid": {ticker: [errors]}, "missing", "unmatched"}`. Validates against the schema, stores valid
    briefs in `recommendations.llm_brief`, logs the rest. Never raises.
  - `validate_brief(data) -> list[str]` (hand-rolled, no `jsonschema` dependency; a test compares it with
    `jsonschema` when installed).
- `src/report.py` + `templates/report.html.j2`, `templates/report.md.j2`
  - `build(conn, as_of, *, config=None, since=None) -> (html_path, md_path)`; also writes `reports/latest.*`.
  - `build_context(...)` (shared dict feeding both templates), `build_summary(conn, as_of)` / `summary_text(ctx)`
    (the Telegram digest text), `render_html`, `render_markdown`.
- `notify/telegram.py`: `send_report(html_path, summary_text)`, `send_alert(text)`; both return a bool
  (True = delivered), never raise.

## How to run

```python
from src.llm import brief_io; from src import report; from notify import telegram
brief_io.write_inputs(conn, recs, as_of)               # -> work/brief_inputs/*.json + INSTRUCTIONS.md
# ... routine agent writes work/brief_outputs/<TICKER>.json ...
brief_io.merge_outputs(conn, "work/brief_outputs")
html, md = report.build(conn, as_of)
telegram.send_report(html, report.build_summary(conn, as_of))
```

`pytest tests/test_llm_brief_io.py tests/test_report.py tests/test_report_edge.py tests/test_notify.py -q` (~8 s).

## Tests (44)

- `test_llm_brief_io.py`: inputs content/ordering/instructions, top-N + stale cleanup, token-budget trimming,
  fetch failures, validator == schema file, full write -> agent output -> merge -> render round trip, invalid /
  mismatched / unmatched files, never raising, catch-up (two recs same ticker -> brief goes to the rec in the input).
- `test_report.py` / `test_report_edge.py`: normal day, empty day, Unfavorable, catch-up day, no catch-up section when
  nothing missed, missing brief, invalid brief JSON (4 variants), HTML escaping, data-quality note, tracking updates +
  open table, statistics section, Telegram summary, missing regime row, idempotent rebuild, Markdown block structure,
  no external resources / no sizing language / footer on every output.
- `test_notify.py`: text + document, long text, unconfigured (each var) logs once and no-ops, 5xx / network retry,
  429 `retry_after`, give-up, 4xx not retried, missing file, token never logged, unexpected exception swallowed.
- Test data comes from `tests/report_helpers.py` (a fresh `init_db` DB with synthetic live recs), because the committed
  `fixture.db` contains no recommendations.

## Interpretation calls

1. **Which recs are shown** = `in_report = 1` and `signal_date == as_of`, ordered by rank. Ranking already applied the
   regime top-N and the industry cap, so the report does not re-cap. In an **Unfavorable** regime the cards are
   suppressed regardless of the flag (banner + "No new recommendations").
2. **Previous report date** (for "updates since" and catch-up) = newest `reports/YYYY-MM-DD.md` before `as_of`; else the
   previous `regime_log` date; else `as_of - 1 day`. Bot 7 can pass `since=` explicitly. Catch-up section lists
   `in_report` recs with `since < signal_date < as_of`.
3. **Tracking updates**: finals come from `recommendation_results` (`exit_date` in `(since, as_of]`, falling back to
   `last_updated` when `exit_date` is NULL, e.g. expired). "Triggered" is not stored anywhere, so open recs are
   re-simulated with `tracker.simulate` and shown when `entry_date` falls in the window.
4. **Open table** = live `open` + `pending` recs with `signal_date < as_of` (today's are in the cards). Current R is
   shown for `open` only.
5. **Data-quality note** reads `job_log` rows with `job LIKE 'prices%'` and `trading_date = as_of`. Message may be JSON
   `{"missing": [...], "nasdaq_filled": [...]}` (the `update_prices` report shape) or free text (shown only when
   status is not ok/done/success). **Bot 7/Bot 1: please log the prices step with that shape**, otherwise no note
   appears.
6. **EV/FCF percentile** is read from `valuation_info` keys `ev_fcf_sector_pct` / `ev_fcf_pct` / `ev_fcf_percentile`
   (0-1 or 0-100) plus optional `ev_fcf`; Bot 2's guardrail code did not exist yet, so this is defensive.
7. **Brief schema**: `ticker` is optional; catalyst `date` must be `YYYY-MM-DD` or `null` (fuzzy timing goes in `event`);
   list sizes are capped (20/20/20/40). The brief is stored as the `LLMBrief` shape (with `ticker`). A brief is
   re-validated at render time, so a hand-edited/corrupt `llm_brief` is just omitted.
8. **Merge targeting**: each output is attached to the `rec_id` in the matching input file; without an input file it
   falls back to the latest live `in_report` rec for the ticker. Briefs are generated for the latest day's recs only
   (not for catch-up-section recs).
9. **No sizing**: only stop/target prices and %, plus insider-buy dollar values (public Form 4 data, shown as e.g.
   `$150K`); no share counts anywhere.
10. Telegram: summary is sent first, then the document, so the digest still arrives if the upload fails. Plain text (no
    `parse_mode`), truncated to 4000 chars. Retries: 3 attempts, 5xx/429/network only, `retry_after` honoured.
    Missing credentials are logged once per process at INFO.
11. Markdown tables are escaped for `|`, `<`, `>`; HTML is Jinja-autoescaped (LLM text is untrusted).

## Known issues

- `stats.summary("live")` (Bot 5) raises `KeyError: 'id'` whenever recs exist but none has closed - i.e. the first weeks
  of live running. Change request filed in `docs/CHANGE_REQUESTS.md`; the report falls back to a counts-only statistics
  section ("not enough data yet" for every module) so it never fails. Module/baseline columns fill in once that is fixed.
- `report.build` with no `regime_log` row for `as_of` renders "Regime data unavailable" instead of failing.
- Not exercised against a live DB / real EDGAR text (none available here); `write_inputs` was tested with stubs for
  Bot 3's functions as instructed.
