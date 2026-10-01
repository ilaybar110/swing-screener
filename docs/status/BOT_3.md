# Bot 3 (EDGAR filings, insider forms, text, news) — status

## What was built

| File | Purpose |
|---|---|
| `src/data/edgar_daily.py` | `process_day(conn, date_, client=None, lookback_days=10) -> dict` — one day's Form 4/4-A and 8-K/8-K-A for universe CIKs → `filings`, `insider_trades`, `earnings_dates`. Idempotent. |
| `src/data/form4.py` | Pure Form 4 XML parser + `store_form4` (writes `insider_trades`, applies 4/A supersession). Shared with the bulk loader. |
| `src/data/edgar_bulk.py` | `backfill(conn, start_year=2014)` — local-only, resumable: `submissions.zip` → `filings` + earnings events; quarterly Insider Transactions Data Sets → `insider_trades`. Also `validate_purchases` + CLI. |
| `src/data/texts.py` | `get_8k_texts(conn, ticker, as_of, days=30)` — cleaned item sections + EX-99.1, cached in `filing_texts`. |
| `src/data/news.py` | `get_news(ticker, company_name, as_of, days=14)` — Google News RSS, deduped, point-in-time. |
| `tests/test_{edgar_daily,form4,edgar_bulk,texts,news}.py` | 70 tests, no network by default. |
| `tests/fixtures/edgar/` | Real saved EDGAR/Google News samples (daily index, Form 4 submissions incl. multi-owner purchase / weighted-price / 4/A, 8-K index page + primary doc, a trimmed 2024q1 insider data set, RSS feed). |

## How to run

```bash
# daily (what run_daily.py should call, once per missed trading day, oldest first)
from src.data.edgar_daily import process_day
report = process_day(conn, trading_date)      # -> {"form4_processed":…, "eightk_processed":…, "earnings_events":…, "errors":[…], "index_date":…}

# local backfill (needs tickers populated with CIKs; ~1.5 GB download on first run)
python -m src.data.edgar_bulk backfill --start-year 2014
# manual validation against EDGAR's website (defaults INTC NKE PYPL BA DIS)
python -m src.data.edgar_bulk validate [TICKER ...] [--limit 8]
```
`SEC_EMAIL` must be set (the shared `EdgarClient` raises otherwise).

## Tests

```bash
python -m pytest tests/test_edgar_daily.py tests/test_form4.py tests/test_edgar_bulk.py tests/test_texts.py tests/test_news.py -q
# live smoke test (5 tickers, hits EDGAR):
SEC_EMAIL=you@example.com RUN_NETWORK_TESTS=1 python -m pytest tests/test_edgar_daily.py -k live
```
69 pass, 1 skipped (live) without the env vars; live test also verified passing. The earnings-timing rule has a parametrized table (BMO / exactly 09:30 / intraday / exactly 16:00 / AMC / Friday-after-close / weekend / holiday / day-before-holiday / early-close day / year-end).

## Interpretation calls (please read — downstream bots depend on these)

1. **Time zones.** Verified against live EDGAR (Apple 8-K accepted 16:30:28 ET): the filing *index page* and the `.txt` header are **Eastern**; the submissions JSON/bulk `acceptanceDateTime` is **UTC** despite looking local. Everything stored in `filings.acceptance_datetime` / `earnings_dates.acceptance_datetime` is **naive Eastern** ISO (`2026-07-30T16:30:28`), same convention as the fixture rows.
2. **8-K data comes from the filing index page** (one small request per 8-K: acceptance, items, primary doc, EX-99.1) rather than the submissions API — it has everything in one hit and avoids per-CIK submissions JSON (stale-cache risk locally).
3. **Earnings events** (`earnings_dates`, `source="8k_2.02"`): `event_date` = first session whose open is *strictly after* acceptance. `timing`: `BMO` (<09:30, same day), `AMC` (≥ that day's close — 13:00 on early-close days — next session), `NULL` (09:30 ≤ t < close: unknown, next session), weekend/holiday → next session with `BMO`. Only original `8-K` forms create events (8-K/A never). If a ticker already has an event row for the same date, the earliest acceptance wins. All tickers of a CIK (share classes) get a row.
   - **Naming differs from fixtures/Bot 1:** fixtures use source `8-K_2.02` and timing `before_open`; the spec asked for `8k_2.02`, and Bot 1's Nasdaq rows use `BMO`/`AMC`/`NULL`. I followed the spec + Bot 1. Module B / `get_earnings_events` consumers should not filter on the source string.
4. **Form 4 attribution.** `insider_trades` has one insider per row, but joint filings list several owners. Each transaction is stored **once**, attributed to the *primary owner* = first owner who is an officer or director, else the first owner (avoids double-counting value for Module C). Flags (`is_officer`, `is_director`, `is_ten_pct_owner`, title) are that owner's. Only the non-derivative table is read. `price`/`value` are NULL when the filing gives no numeric price (footnote-only); a weighted-average price is just its numeric value.
5. **4/A.** Stored like any Form 4; the original accession's rows are deleted when the amendment restates ≥ 1 non-derivative transaction for the same issuer + primary insider + a shared transaction date (restricted to the original's filing date when `dateOfOriginalSubmission`/`DATE_OF_ORIG_SUB` is given). A derivative-only amendment (seen in real Workday data) leaves the original alone.
6. **Issuer filter.** Form 4 lines are matched by CIK in the index, but the *issuer* CIK in the XML decides: a filing that only matched via a reporting owner's CIK is ignored (`form4_not_issuer` in the report). `filings.ticker`/`insider_trades.ticker` = issuer symbol if it is one of the CIK's tickers, else the first (alphabetical).
7. **Unpublished index.** `process_day` uses `latest_published_daily_index`; if the requested day isn't published it processes the latest earlier index (harmless, idempotent) and reports `index_is_requested_day=False`. Callers wanting "today only" should check that flag.
8. **Failures** leave the accession unprocessed (no `processed=1`), so it is retried by the next run that walks that day; errors are listed in the report, never raised.
9. **Bulk.** Form 4 rows from `submissions.zip` are inserted `processed=0` and flipped to 1 when the insider data set covers them (data sets lag a quarter, so the newest Form 4s stay 0 → the daily job fills them). Existing rows are never overwritten (`INSERT OR IGNORE`), so daily-enriched 8-Ks keep their `exhibit_991_url`. Resumability: HTTP Range for downloads, `job_log` rows (`edgar_bulk:submissions:<year>`, `edgar_bulk:insider:<yyyyqN>`) skip finished stages. Refuses to run on `paths.run_db`.
10. **Texts.** Sections are `item_<n>` and `EX-99.1`; each dict has `accession, filed_date, items (list), section, text`, newest filing first; capped at 50k chars/section. A filing is fetched only if it has ≥ 1 of the wanted items; filings with no recorded URLs (e.g. fixture rows) are skipped silently. Failed fetches are not cached.
11. **News.** Returns `title, source, published_at (ISO UTC), url` plus `link`/`published` aliases (the names in `src/contracts.py`). Items with `published ≥ (as_of+1) 00:00 UTC` are dropped; the query also carries Google's `after:/before:` operators. Near-duplicate = normalized-title similarity ≥ 0.88. No item cap (a 14-day window can return ~100+ headlines for big names) — Bot 6 should truncate.

## Known issues / open items

- **`.gitignore` bug (important):** `data/` ignores `src/data/` too, so no file under `src/data/` is tracked — including Bot 0's `edgar_client.py`. A fresh cloud clone would lack the package. Logged in `docs/CHANGE_REQUESTS.md` (change to `/data/`). I committed my own files with `git add -f`; Bot 0/1/2 files in `src/data/` remain untracked until that is fixed.
- `edgar_bulk.download_file` reaches into `EdgarClient._session/_throttle` (change request filed for a public streaming method).
- The `submissions.zip` loader was tested on synthetic zips shaped like the documented format (columnar `filings.recent`, plus top-level arrays in `-submissions-NNN.json`); the real 1.5 GB file was not downloaded in this session. The insider data-set loader was tested on a real (trimmed) 2024q1 data set.
- Form 4 filed after 17:30 ET: `filed_date` comes from the index (EDGAR's official next-business-day date), `acceptance_datetime` is the real time — they can differ by a day, by design.
- Early-close handling uses `pandas_market_calendars` for the close time; if the calendar lookup fails it falls back to 16:00.
- Google News RSS can be rate-limited or change format; failures degrade to an empty list.
- `tests/test_guardrail.py` (Bot 2, uncommitted at the time) failed to import `src.guardrail` when I ran the full suite — not mine; I ran my files plus foundation tests.
