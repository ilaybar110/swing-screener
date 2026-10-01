"""Fundamentals: companyfacts parsing, TTM/FCF derivation, and the weekly
frames-based universe summary.

Three usage paths (see docs/PLAN.md):
- Cloud daily: `fetch_company` pulls one company's companyfacts on demand (3-8 MB),
  parses it, and keeps only the metrics we need -- the raw JSON is discarded.
- Cloud weekly: `update_summary` uses the XBRL frames API (one request = one
  metric for the whole universe for one period) -- companyfacts is never bulk
  downloaded in the cloud.
- Local: `backfill_bulk` loads the local companyfacts.zip bulk file into the
  research DB for point-in-time backtesting. Never called from the cloud routine.

Point-in-time correctness: every row keeps `filed_date`; a restatement is a new
row with a later `filed_date` for the same (metric, period_end), never an
overwrite. `src.data_access.DataAccess.get_fundamentals` is what collapses to
"latest known as of as_of" -- this module never filters by as_of itself.
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from src.data import xbrl_tags
from src.data.edgar_client import EdgarClient
from src.utils.logging import get_logger

log = get_logger(__name__)

# Tolerances for classifying a duration fact as a discrete quarter vs. a
# cumulative (YTD) figure that needs to be derived by subtraction.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------------------------------------------------------------------------
# Tag resolution
# ---------------------------------------------------------------------------


def resolve_tag(
    facts: dict, tag_specs: list[xbrl_tags.TagSpec]
) -> Optional[tuple[xbrl_tags.TagSpec, list[dict]]]:
    """Try each TagSpec in order; return (spec, raw_fact_entries) for the first
    one with any data, or None if none of them have data. IFRS-only / foreign
    filers (no us-gaap/dei facts at all) simply never match -> None."""
    taxonomies = facts.get("facts", {})
    for spec in tag_specs:
        node = taxonomies.get(spec.taxonomy, {}).get(spec.tag)
        if not node:
            continue
        entries: list[dict] = []
        for unit_entries in node.get("units", {}).values():
            entries.extend(unit_entries)
        if entries:
            return spec, entries
    return None


# ---------------------------------------------------------------------------
# Quarterization of duration (flow) facts
# ---------------------------------------------------------------------------


def _duration_days(entry: dict) -> Optional[int]:
    if "start" not in entry:
        return None
    return (_parse_date(entry["end"]) - _parse_date(entry["start"])).days


def quarterize(raw_entries: list[dict]) -> list[dict]:
    """Turn raw duration fact entries (each with start/end/val/accn/fy/fp/form/
    filed) into discrete-quarter rows: {period_start, period_end, value, form,
    filed_date, accession}.

    Entries are grouped by fiscal year (`fy`, as SEC reports it on every fact --
    falling back to the fact's `start` date if `fy` is absent) and walked in
    end-date order, keeping a running "covered so far" total for that fiscal
    year:
    - A fact whose duration is ~1 quarter (80-100 days) is already discrete;
      its value is used as-is and added to the running total.
    - A longer (YTD-cumulative or annual) fact is discretized as
      `value - running_total_so_far` -- e.g. Q4 = FY - 9mo YTD, Q2 = 6mo YTD -
      Q1, or, when only discrete quarters plus an annual total are tagged (no
      explicit YTD facts), Q4 = FY - sum(Q1, Q2, Q3). The running total is then
      reset to the fact's own (authoritative, already-cumulative) value.
    - A cumulative-duration fact with nothing earlier in its fiscal year to
      subtract against (e.g. an isolated 6-month YTD fact with no Q1) cannot be
      derived and is skipped rather than guessed.
    """
    by_fy: dict[Any, dict[date, dict]] = {}
    for e in raw_entries:
        if "start" not in e or "end" not in e or e.get("val") is None:
            continue
        days = _duration_days(e)
        if days is None:
            continue
        fy_key = e.get("fy", _parse_date(e["start"]))
        end = _parse_date(e["end"])
        bucket = by_fy.setdefault(fy_key, {})
        existing = bucket.get(end)
        if existing is None or e["filed"] > existing["filed"]:
            bucket[end] = e

    rows: list[dict] = []
    for fy_key, by_end in by_fy.items():
        ordered = sorted(by_end.values(), key=lambda e: _parse_date(e["end"]))
        cum_value = 0.0
        prev_end: Optional[date] = None
        for i, e in enumerate(ordered):
            end = _parse_date(e["end"])
            days = _duration_days(e)
            is_direct_quarter = _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS
            if is_direct_quarter:
                discrete_value = float(e["val"])
                cum_value += discrete_value
            else:
                if i == 0:
                    # Nothing earlier in this fiscal year to subtract against.
                    prev_end = end
                    continue
                discrete_value = float(e["val"]) - cum_value
                cum_value = float(e["val"])  # authoritative going forward

            period_start = (prev_end + timedelta(days=1)) if prev_end else _parse_date(e["start"])
            rows.append(
                {
                    "period_start": period_start,
                    "period_end": end,
                    "value": discrete_value,
                    "form": e.get("form"),
                    "filed_date": e.get("filed"),
                    "accession": e.get("accn"),
                }
            )
            prev_end = end

    return sorted(rows, key=lambda r: r["period_end"])


# ---------------------------------------------------------------------------
# Instant (point-in-time) facts
# ---------------------------------------------------------------------------


def _instant_rows(raw_entries: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple] = set()
    for e in raw_entries:
        if e.get("val") is None or "end" not in e:
            continue
        key = (e.get("accn"), e["end"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "period_start": None,
                "period_end": _parse_date(e["end"]),
                "value": float(e["val"]),
                "form": e.get("form"),
                "filed_date": e.get("filed"),
                "accession": e.get("accn"),
            }
        )
    return sorted(rows, key=lambda r: r["period_end"])


def _latest_by_period_end(rows: list[dict]) -> dict[date, dict]:
    """Collapse to one entry per period_end, keeping the most-recently-filed."""
    out: dict[date, dict] = {}
    for r in rows:
        existing = out.get(r["period_end"])
        if existing is None or r["filed_date"] > existing["filed_date"]:
            out[r["period_end"]] = r
    return out


# ---------------------------------------------------------------------------
# total_debt: composite of several instant balance-sheet tags
# ---------------------------------------------------------------------------


def _resolve_total_debt(facts: dict) -> tuple[list[dict], bool]:
    """Assemble total_debt per docs/PLAN.md's Bot 2 brief:
    LongTermDebt, or (LongTermDebtNoncurrent + LongTermDebtCurrent), plus
    ShortTermBorrowings / DebtCurrent; LongTermDebtAndCapitalLeaseObligations as a
    last-resort fallback for the long-term piece. Returns (rows, is_proxy)."""

    def resolved_by_end(specs: list[xbrl_tags.TagSpec]) -> dict[date, dict]:
        result = resolve_tag(facts, specs)
        if result is None:
            return {}
        _, raw = result
        return _latest_by_period_end(_instant_rows(raw))

    lt = resolved_by_end(xbrl_tags.LONG_TERM_DEBT_TAGS)
    lt_noncurrent = resolved_by_end(xbrl_tags.LONG_TERM_DEBT_NONCURRENT_TAGS)
    lt_current = resolved_by_end(xbrl_tags.LONG_TERM_DEBT_CURRENT_TAGS)
    lt_and_lease = resolved_by_end(xbrl_tags.LONG_TERM_DEBT_AND_CAPITAL_LEASE_TAGS)
    st_borrow = resolved_by_end(xbrl_tags.SHORT_TERM_BORROWINGS_TAGS)
    debt_current = resolved_by_end(xbrl_tags.DEBT_CURRENT_TAGS)

    all_ends = set(lt) | set(lt_noncurrent) | set(lt_current) | set(lt_and_lease)
    rows: list[dict] = []
    used_proxy = False
    for end in sorted(all_ends):
        base: Optional[float] = None
        base_row: Optional[dict] = None
        is_proxy_end = False
        if end in lt:
            base = lt[end]["value"]
            base_row = lt[end]
        elif end in lt_noncurrent or end in lt_current:
            base = lt_noncurrent.get(end, {}).get("value", 0.0) + lt_current.get(end, {}).get("value", 0.0)
            base_row = lt_noncurrent.get(end) or lt_current.get(end)
        elif end in lt_and_lease:
            base = lt_and_lease[end]["value"]
            base_row = lt_and_lease[end]
            is_proxy_end = True

        if base is None or base_row is None:
            continue

        extra = 0.0
        if end in st_borrow:
            extra = st_borrow[end]["value"]
        elif end in debt_current:
            extra = debt_current[end]["value"]

        used_proxy = used_proxy or is_proxy_end
        rows.append(
            {
                "period_start": None,
                "period_end": end,
                "value": base + extra,
                "form": base_row["form"],
                "filed_date": base_row["filed_date"],
                "accession": base_row["accession"],
            }
        )
    return rows, used_proxy


# ---------------------------------------------------------------------------
# Full companyfacts -> fundamentals rows
# ---------------------------------------------------------------------------


def parse_companyfacts(facts: dict, cik: str, ticker: Optional[str]) -> list[dict]:
    """Parse one company's companyfacts JSON into `fundamentals`-table-shaped
    row dicts. A metric with no resolvable tag (IFRS-only filer, genuinely
    missing disclosure) simply contributes no rows -- never an error."""
    rows: list[dict] = []

    for metric in xbrl_tags.DURATION_METRICS:
        result = resolve_tag(facts, xbrl_tags.METRIC_TAGS[metric])
        if result is None:
            continue
        spec, raw = result
        for q in quarterize(raw):
            rows.append(_finalize_row(q, cik, ticker, metric, "duration", spec))

    for metric in ("shares_outstanding", "cash"):
        result = resolve_tag(facts, xbrl_tags.METRIC_TAGS[metric])
        if result is None:
            continue
        spec, raw = result
        for inst in _instant_rows(raw):
            rows.append(_finalize_row(inst, cik, ticker, metric, "instant", spec))

    debt_rows, debt_is_proxy = _resolve_total_debt(facts)
    for d in debt_rows:
        rows.append(
            {
                "cik": cik,
                "ticker": ticker,
                "metric": "total_debt",
                "period_start": d["period_start"],
                "period_end": d["period_end"],
                "period_type": "instant",
                "value": d["value"],
                "form": d["form"],
                "filed_date": d["filed_date"],
                "accession": d["accession"],
                "tag_used": "total_debt_composite",
                "is_proxy": debt_is_proxy,
            }
        )

    return rows


def _finalize_row(
    r: dict, cik: str, ticker: Optional[str], metric: str, period_type: str, spec: xbrl_tags.TagSpec
) -> dict:
    return {
        "cik": cik,
        "ticker": ticker,
        "metric": metric,
        "period_start": r["period_start"],
        "period_end": r["period_end"],
        "period_type": period_type,
        "value": r["value"],
        "form": r["form"],
        "filed_date": r["filed_date"],
        "accession": r["accession"],
        "tag_used": spec.tag,
        "is_proxy": spec.is_proxy,
    }


# ---------------------------------------------------------------------------
# TTM / FCF
# ---------------------------------------------------------------------------


def ttm(quarterly_rows: list[dict], as_of: date) -> Optional[float]:
    """Sum of the most recent 4 quarters with period_end <= as_of. None if
    fewer than 4 are available (point-in-time: quarters after as_of are
    excluded even if present in the input)."""
    known = sorted(
        (r for r in quarterly_rows if r["period_end"] <= as_of),
        key=lambda r: r["period_end"],
        reverse=True,
    )
    if len(known) < 4:
        return None
    return sum(r["value"] for r in known[:4])


def fcf(operating_cash_flow: Optional[float], capex: Optional[float]) -> Optional[float]:
    """Free cash flow = operating cash flow - capex. None if either input is
    missing (never guess FCF from partial data)."""
    if operating_cash_flow is None or capex is None:
        return None
    return operating_cash_flow - capex


# ---------------------------------------------------------------------------
# fetch_company (cloud daily, on demand)
# ---------------------------------------------------------------------------


def _upsert_rows(conn: sqlite3.Connection, cik: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM fundamentals WHERE cik = ?", (cik,))
    conn.executemany(
        "INSERT INTO fundamentals (cik, ticker, metric, period_start, period_end, "
        "period_type, value, form, filed_date, accession, tag_used, is_proxy) "
        "VALUES (:cik, :ticker, :metric, :period_start, :period_end, :period_type, "
        ":value, :form, :filed_date, :accession, :tag_used, :is_proxy)",
        [
            {
                **r,
                "period_start": r["period_start"].isoformat() if r["period_start"] else None,
                "period_end": r["period_end"].isoformat(),
                "is_proxy": int(r["is_proxy"]),
            }
            for r in rows
        ],
    )
    conn.commit()


def fetch_company(conn: sqlite3.Connection, cik: str, client: Optional[EdgarClient] = None) -> int:
    """Fetch companyfacts for one CIK on demand, parse, and upsert into
    `fundamentals`. Discards the raw JSON once parsed (only the derived rows are
    kept in memory / on disk). Returns the number of rows written.

    `client` is injectable so tests and backtests never make live requests --
    see docs/PLAN.md's guardrail.evaluate note ("make that injectable so
    backtests never fetch")."""
    if client is None:
        from src.config import load_config

        cfg = load_config()
        client = EdgarClient(cfg.paths.raw_cache_dir, sec_email=cfg.secrets.sec_email)

    cik_padded = str(cik).zfill(10)
    row = conn.execute("SELECT ticker FROM tickers WHERE cik = ?", (cik_padded,)).fetchone()
    ticker = row["ticker"] if row else None

    log.info("fetching companyfacts for cik=%s ticker=%s", cik_padded, ticker)
    facts = client.get_companyfacts(cik_padded)
    rows = parse_companyfacts(facts, cik=cik_padded, ticker=ticker)
    _upsert_rows(conn, cik_padded, rows)
    log.info("parsed %d fundamentals rows for cik=%s", len(rows), cik_padded)
    return len(rows)


# ---------------------------------------------------------------------------
# backfill_bulk (local only)
# ---------------------------------------------------------------------------

BULK_COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
_BACKFILL_JOB_NAME = "fundamentals_backfill_bulk"


def backfill_bulk(conn: sqlite3.Connection, zip_path: Optional[Path] = None) -> dict:
    """Local-only: load the bulk companyfacts.zip (one JSON file per CIK) into
    `fundamentals` for every CIK known in `tickers`. Never called from the cloud
    routine (the zip is hundreds of MB).

    Resumable: each successfully processed CIK is recorded in `job_log` with
    job=_BACKFILL_JOB_NAME, so a re-run skips CIKs already done in a prior run
    (job_log rows are never deleted by this function -- callers wanting a full
    re-backfill should clear them first).

    `zip_path` lets tests/local runs point at an already-downloaded file instead
    of fetching BULK_COMPANYFACTS_ZIP_URL directly (the actual download, when
    zip_path is None, is not implemented here -- see docs/CHANGE_REQUESTS.md if
    a shared bulk-download helper is needed).
    """
    if zip_path is None:
        raise NotImplementedError(
            "backfill_bulk requires a pre-downloaded companyfacts.zip (pass "
            "zip_path=). Streaming the ~1GB+ bulk file is left to the caller so "
            "this function stays testable; see BULK_COMPANYFACTS_ZIP_URL."
        )

    done_ciks = {
        r["message"]
        for r in conn.execute(
            "SELECT message FROM job_log WHERE job = ? AND status = 'done'",
            (_BACKFILL_JOB_NAME,),
        ).fetchall()
    }

    universe_ciks = {
        r["cik"]: r["ticker"]
        for r in conn.execute("SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL").fetchall()
    }

    processed = 0
    skipped = 0
    failed: list[str] = []
    run_id = f"backfill-{datetime.utcnow().isoformat()}"

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.startswith("CIK") or not name.endswith(".json"):
                continue
            cik_padded = name[3:13]
            if cik_padded not in universe_ciks:
                continue
            if cik_padded in done_ciks:
                skipped += 1
                continue
            try:
                import json

                with zf.open(name) as f:
                    facts = json.load(f)
                rows = parse_companyfacts(facts, cik=cik_padded, ticker=universe_ciks[cik_padded])
                _upsert_rows(conn, cik_padded, rows)
                conn.execute(
                    "INSERT INTO job_log (run_id, job, started_at, finished_at, status, message) "
                    "VALUES (?, ?, ?, ?, 'done', ?)",
                    (run_id, _BACKFILL_JOB_NAME, datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), cik_padded),
                )
                conn.commit()
                processed += 1
            except Exception as exc:  # noqa: BLE001 -- keep going across a huge zip
                log.warning("backfill_bulk failed for cik=%s: %s", cik_padded, exc)
                failed.append(cik_padded)

    return {"processed": processed, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# update_summary (cloud weekly, frames API)
# ---------------------------------------------------------------------------

_FRAMES_TAXONOMY = "us-gaap"
# Duration metrics resolved via duration frames (e.g. CY2025Q2); instant metrics
# via instant frames (e.g. CY2025Q2I). Only the primary (non-proxy) tag is tried
# via frames -- the frames API doesn't benefit from the same fallback chain
# fetch_company uses, since it's one request per (tag, quarter) for the *entire*
# universe, and we want to keep the request count low (see module docstring).
_SUMMARY_DURATION_TAGS = {
    "revenue": xbrl_tags.REVENUE_TAGS[:2],
    "operating_cash_flow": xbrl_tags.OPERATING_CASH_FLOW_TAGS[:1],
    "capex": xbrl_tags.CAPEX_TAGS[:2],
}
_SUMMARY_INSTANT_TAGS = {
    "cash": xbrl_tags.CASH_TAGS[:1],
    # dei (cover-page share count) is the broadest; us-gaap fills the rest
    "shares_outstanding": xbrl_tags.SHARES_OUTSTANDING_TAGS[:2],
    "total_debt": xbrl_tags.LONG_TERM_DEBT_TAGS[:1] + xbrl_tags.LONG_TERM_DEBT_NONCURRENT_TAGS[:1],
}
# Cash-flow statements report year-to-date figures, so quarterly frames only exist for Q1;
# for these metrics the latest full fiscal year (calendar-year frame) is the TTM stand-in.
_ANNUAL_FALLBACK_METRICS = ("operating_cash_flow", "capex")


def _quarter_periods(as_of: date, n: int) -> list[tuple[int, int]]:
    """Last n (calendar-year, quarter) pairs ending in the quarter containing
    as_of, oldest first."""
    q = (as_of.month - 1) // 3 + 1
    year = as_of.year
    periods = []
    for _ in range(n):
        periods.append((year, q))
        q -= 1
        if q == 0:
            q = 4
            year -= 1
    return list(reversed(periods))


def update_summary(conn: sqlite3.Connection, as_of: date, client: Optional[EdgarClient] = None) -> dict:
    """Weekly job: pull the XBRL frames API for the tags in
    _SUMMARY_DURATION_TAGS / _SUMMARY_INSTANT_TAGS over the last ~6 quarters,
    map CIK -> ticker via `tickers`, compute TTM values / EV / EV-FCF / EV-FCF
    sector percentile, and write `fundamentals_summary` rows for as_of.

    Keeps the request count low: at most len(duration tags)*6 +
    len(instant tags)*6 requests total, regardless of universe size (frames
    returns every company for a period in one response). Returns a report dict
    with the request count and per-metric coverage."""
    if client is None:
        from src.config import load_config

        cfg = load_config()
        client = EdgarClient(cfg.paths.raw_cache_dir, sec_email=cfg.secrets.sec_email)

    cik_to_ticker = {
        r["cik"]: r["ticker"]
        for r in conn.execute("SELECT cik, ticker FROM tickers WHERE cik IS NOT NULL").fetchall()
    }
    universe = {r["ticker"] for r in conn.execute("SELECT ticker FROM universe_snapshots WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshots)").fetchall()}
    sector_by_ticker = {r["ticker"]: r["sector"] for r in conn.execute("SELECT ticker, sector FROM tickers").fetchall()}

    quarters = _quarter_periods(as_of, 6)
    requests_made = 0

    def frame_entries(spec, unit: str, period: str) -> list[dict]:
        nonlocal requests_made
        try:
            data = client.get_frames(spec.taxonomy, spec.tag, unit, period).get("data", [])
        except Exception as exc:  # noqa: BLE001 - the in-progress period is normally not filed yet (404)
            log.info("frames %s/%s %s unavailable: %s", spec.taxonomy, spec.tag, period, exc)
            data = []
        requests_made += 1
        return data

    # duration_data[metric][ticker] = list of {"period_end": date, "value": float}
    duration_data: dict[str, dict[str, list[dict]]] = {m: {} for m in _SUMMARY_DURATION_TAGS}
    for metric, specs in _SUMMARY_DURATION_TAGS.items():
        seen: set[tuple[str, date]] = set()  # first tag in the chain wins for each (ticker, period)
        for spec in specs:
            for year, q in quarters:
                for entry in frame_entries(spec, "USD", f"CY{year}Q{q}"):
                    ticker = cik_to_ticker.get(str(entry.get("cik", "")).zfill(10))
                    if ticker is None or ticker not in universe:
                        continue
                    end = _parse_date(entry["end"])
                    if (ticker, end) in seen:
                        continue
                    seen.add((ticker, end))
                    duration_data[metric].setdefault(ticker, []).append({"period_end": end, "value": float(entry["val"])})

    annual_data: dict[str, dict[str, tuple[date, float]]] = {m: {} for m in _ANNUAL_FALLBACK_METRICS}
    for metric in _ANNUAL_FALLBACK_METRICS:
        for spec in _SUMMARY_DURATION_TAGS[metric]:
            for year in (as_of.year - 2, as_of.year - 1):
                for entry in frame_entries(spec, "USD", f"CY{year}"):
                    ticker = cik_to_ticker.get(str(entry.get("cik", "")).zfill(10))
                    if ticker is None or ticker not in universe:
                        continue
                    end = _parse_date(entry["end"])
                    if end <= as_of and (ticker not in annual_data[metric] or end > annual_data[metric][ticker][0]):
                        annual_data[metric][ticker] = (end, float(entry["val"]))

    instant_data: dict[str, dict[str, float]] = {m: {} for m in _SUMMARY_INSTANT_TAGS}
    for metric, specs in _SUMMARY_INSTANT_TAGS.items():
        unit = "shares" if metric == "shares_outstanding" else "USD"
        for spec in specs:
            # newest quarter first; older quarters only fill tickers that have not filed the newer one
            # yet (the quarter containing as_of has just a few early filers, or none)
            for year, q in reversed(quarters[-3:]):
                for entry in frame_entries(spec, unit, f"CY{year}Q{q}I"):
                    ticker = cik_to_ticker.get(str(entry.get("cik", "")).zfill(10))
                    if ticker is None or ticker not in universe or ticker in instant_data[metric]:
                        continue
                    instant_data[metric][ticker] = float(entry["val"])

    rows: dict[str, dict[str, Any]] = {}
    for ticker in universe:
        revenue_ttm = ttm(duration_data["revenue"].get(ticker, []), as_of)
        ocf_ttm = ttm(duration_data["operating_cash_flow"].get(ticker, []), as_of)
        capex_ttm = ttm(duration_data["capex"].get(ticker, []), as_of)
        if ocf_ttm is None or capex_ttm is None:  # use the latest fiscal year, both legs together
            a_ocf = annual_data["operating_cash_flow"].get(ticker)
            a_capex = annual_data["capex"].get(ticker)
            if a_ocf and a_capex:
                ocf_ttm, capex_ttm = a_ocf[1], a_capex[1]
        fcf_ttm = fcf(ocf_ttm, capex_ttm)
        rows[ticker] = {
            "as_of": as_of.isoformat(),
            "ticker": ticker,
            "revenue_ttm": revenue_ttm,
            "op_income_ttm": None,  # not pulled via frames (kept for daily/on-demand path)
            "fcf_ttm": fcf_ttm,
            "total_debt": instant_data["total_debt"].get(ticker),
            "cash": instant_data["cash"].get(ticker),
            "shares": instant_data["shares_outstanding"].get(ticker),
            "ev_fcf": None,
            "ev_fcf_sector_pct": None,
        }

    # EV = market cap (from the latest universe snapshot) + total_debt - cash.
    price_rows = {
        r["ticker"]: dict(r)
        for r in conn.execute(
            "SELECT ticker, market_cap FROM universe_snapshots "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshots)"
        ).fetchall()
    }
    for ticker, row in rows.items():
        market_cap = price_rows.get(ticker, {}).get("market_cap")
        if market_cap is None or row["total_debt"] is None or row["cash"] is None:
            continue
        ev = market_cap + row["total_debt"] - row["cash"]
        if row["fcf_ttm"] and row["fcf_ttm"] != 0:
            row["ev_fcf"] = ev / row["fcf_ttm"]

    # Sector percentile of ev_fcf (lower EV/FCF is "cheaper"; percentile is the
    # share of same-sector peers this ticker's ev_fcf is less than or equal to).
    by_sector: dict[str, list[str]] = {}
    for ticker in rows:
        sector = sector_by_ticker.get(ticker)
        if sector:
            by_sector.setdefault(sector, []).append(ticker)
    for sector, tickers in by_sector.items():
        values = [(t, rows[t]["ev_fcf"]) for t in tickers if rows[t]["ev_fcf"] is not None]
        if len(values) < 2:
            continue
        sorted_values = sorted(v for _, v in values)
        for t, v in values:
            rank = sum(1 for x in sorted_values if x <= v)
            rows[t]["ev_fcf_sector_pct"] = 100.0 * rank / len(sorted_values)

    conn.execute("DELETE FROM fundamentals_summary WHERE as_of = ?", (as_of.isoformat(),))
    conn.executemany(
        "INSERT INTO fundamentals_summary (as_of, ticker, revenue_ttm, op_income_ttm, "
        "fcf_ttm, total_debt, cash, shares, ev_fcf, ev_fcf_sector_pct) VALUES "
        "(:as_of, :ticker, :revenue_ttm, :op_income_ttm, :fcf_ttm, :total_debt, "
        ":cash, :shares, :ev_fcf, :ev_fcf_sector_pct)",
        list(rows.values()),
    )
    conn.commit()

    resolved = {
        metric: sum(1 for t in universe if t in duration_data[metric]) for metric in duration_data
    }
    resolved.update({metric: sum(1 for t in universe if t in instant_data[metric]) for metric in instant_data})
    log.info("update_summary: %d requests, universe=%d, resolved=%s", requests_made, len(universe), resolved)
    return {"requests_made": requests_made, "universe_size": len(universe), "resolved": resolved}


# ---------------------------------------------------------------------------
# Coverage script
# ---------------------------------------------------------------------------


def coverage_report(conn: sqlite3.Connection, tickers: Optional[list[str]] = None) -> dict:
    """% of the given tickers (default: the whole `tickers` table) with each
    metric resolved at least once in `fundamentals`, plus the top failure
    reasons (tickers with zero rows at all vs. tickers missing just one
    metric)."""
    if tickers is None:
        tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM tickers WHERE ticker IS NOT NULL").fetchall()]

    all_metrics = sorted(set(xbrl_tags.METRIC_TAGS) | {"total_debt"})
    report: dict[str, dict[str, Any]] = {
        m: {"resolved": 0, "total": len(tickers), "missing_tickers": []} for m in all_metrics
    }

    resolved_by_ticker: dict[str, set[str]] = {t: set() for t in tickers}
    if tickers:
        placeholders = ",".join("?" * len(tickers))
        for row in conn.execute(
            f"SELECT DISTINCT ticker, metric FROM fundamentals WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall():
            resolved_by_ticker.setdefault(row["ticker"], set()).add(row["metric"])

    for ticker, metrics in resolved_by_ticker.items():
        for m in all_metrics:
            if m in metrics:
                report[m]["resolved"] += 1
            else:
                report[m]["missing_tickers"].append(ticker)

    zero_coverage_tickers = [t for t, metrics in resolved_by_ticker.items() if not metrics]
    report["_summary"] = {
        "tickers_with_zero_coverage": len(zero_coverage_tickers),
        "zero_coverage_tickers": zero_coverage_tickers[:20],
    }
    return report
