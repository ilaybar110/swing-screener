"""Daily EDGAR processing: Form 4 / 8-K filings of universe companies for one day.

``process_day(conn, date_)`` reads the EDGAR daily *form* index, keeps Form 4/4-A and
8-K/8-K-A filings whose CIK is in ``tickers``, and

- 8-K: fetches the filing index page (one small request) for the acceptance time,
  items, primary document and Exhibit 99.1 URL -> ``filings``; Item 2.02 8-Ks also
  produce ``earnings_dates`` rows (source ``8k_2.02``).
- Form 4: fetches the complete submission ``.txt`` (header acceptance time + the
  ownership XML), parses it with ``form4`` -> ``insider_trades`` and ``filings``.

Time zones: the filing index page and the ``.txt`` header report acceptance in
**Eastern time**; the submissions JSON API reports **UTC**. ``filings.
acceptance_datetime`` and ``earnings_dates.acceptance_datetime`` are always stored as
naive Eastern ISO text (``YYYY-MM-DDTHH:MM:SS``), same as the fixture data.

Idempotent: accessions already in ``filings`` with ``processed=1`` are skipped, so a
catch-up run may safely re-walk days. A filing that errors is left unprocessed and is
retried on the next run.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.data import form4
from src.data.edgar_client import BASE_ARCHIVES, EdgarClient
from src.utils.calendar import is_trading_day, next_trading_day
from src.utils.logging import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
DEFAULT_CLOSE = time(16, 0)
EARNINGS_SOURCE = "8k_2.02"

FORM4_FORMS = {"4", "4/A"}
EIGHTK_FORMS = {"8-K", "8-K/A"}
WANTED_FORMS = FORM4_FORMS | EIGHTK_FORMS

_client: Optional[EdgarClient] = None


def get_default_client() -> EdgarClient:
    """Process-wide EdgarClient built from config.yaml (shared so the 4 req/s
    throttle is respected by every caller in this process)."""
    global _client
    if _client is None:
        from src.config import load_config

        cfg = load_config()
        _client = EdgarClient(
            cfg.paths.raw_cache_dir,
            max_requests_per_second=cfg.edgar.max_requests_per_second,
        )
    return _client


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    form: str
    company: str
    cik: str  # zero-padded 10
    filed_date: str  # ISO
    file_name: str  # edgar/data/<cik>/<accession>.txt

    @property
    def accession(self) -> str:
        return self.file_name.rsplit("/", 1)[-1].removesuffix(".txt")


_INDEX_LINE = re.compile(
    r"^(?P<form>\S.*?)\s{2,}(?P<name>.*?)\s+(?P<cik>\d+)\s+(?P<date>\d{8})\s+"
    r"(?P<file>edgar/\S+)\s*$"
)


def parse_form_index(text: str, forms: Optional[set[str]] = None) -> list[IndexEntry]:
    """Parse ``form.YYYYMMDD.idx`` text; optionally keep only the given form types."""
    out: list[IndexEntry] = []
    for line in text.splitlines():
        m = _INDEX_LINE.match(line)
        if not m:
            continue
        form = m.group("form").strip()
        if forms is not None and form not in forms:
            continue
        d = m.group("date")
        out.append(
            IndexEntry(
                form=form,
                company=m.group("name").strip(),
                cik=m.group("cik").zfill(10),
                filed_date=f"{d[:4]}-{d[4:6]}-{d[6:]}",
                file_name=m.group("file"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Filing index page (8-K)
# ---------------------------------------------------------------------------


@dataclass
class FilingIndex:
    accepted_et: Optional[str]  # naive ET ISO
    items: list[str]  # ["2.02", "9.01"]
    primary_doc_url: Optional[str]
    exhibit_991_url: Optional[str]


_ACCEPTED_RE = re.compile(
    r'Accepted</div>\s*<div class="info">\s*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})', re.S
)
_ITEMS_BLOCK_RE = re.compile(r'Items</div>\s*<div class="info">(.*?)</div>', re.S)
_ITEM_NO_RE = re.compile(r"Item\s+(\d{1,2}\.\d{2})")
_DOC_ROW_RE = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>[^<]*</td>\s*'
    r'<td[^>]*>\s*<a href="([^"]+)"[^>]*>[^<]*</a>.*?</td>\s*<td[^>]*>([^<]*)</td>',
    re.S,
)


def _abs_url(href: str) -> str:
    href = href.replace("&amp;", "&")
    if href.startswith("/ix?doc="):  # inline-XBRL viewer wrapper
        href = href[len("/ix?doc="):]
    if href.startswith("http"):
        return href
    return "https://www.sec.gov" + href


def parse_filing_index(html: str) -> FilingIndex:
    """Parse a ``<accession>-index.htm`` page. Time is Eastern (see module docs)."""
    m = _ACCEPTED_RE.search(html)
    accepted = f"{m.group(1)}T{m.group(2)}" if m else None

    items: list[str] = []
    mb = _ITEMS_BLOCK_RE.search(html)
    if mb:
        items = _ITEM_NO_RE.findall(mb.group(1))

    primary = None
    ex99 = None
    ex99_generic = None
    for href, dtype in _DOC_ROW_RE.findall(html):
        dtype = dtype.strip().upper()
        if primary is None and dtype in EIGHTK_FORMS:
            primary = _abs_url(href)
        elif ex99 is None and dtype == "EX-99.1":
            ex99 = _abs_url(href)
        elif ex99_generic is None and dtype == "EX-99":
            ex99_generic = _abs_url(href)
    return FilingIndex(accepted, items, primary, ex99 or ex99_generic)


def utc_to_et_naive(ts: str) -> str:
    """'2026-07-30T20:30:28.000Z' (submissions API / bulk data, UTC) -> naive ET ISO."""
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).replace(tzinfo=None, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Earnings event from an Item 2.02 acceptance time
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _nyse():
    import pandas_market_calendars as mcal

    return mcal.get_calendar("NYSE")


@lru_cache(maxsize=4096)
def session_close_et(day: date) -> time:
    """Regular close time for a session (13:00 on early-close days)."""
    try:
        sched = _nyse().schedule(start_date=day, end_date=day)
        if len(sched):
            close = sched["market_close"].iloc[0].tz_convert(ET)
            return close.time().replace(second=0, microsecond=0)
    except Exception:  # noqa: BLE001 -- fall back to the standard close
        log.debug("could not load session close for %s", day, exc_info=True)
    return DEFAULT_CLOSE


def earnings_event_from_acceptance(accepted_et: datetime) -> tuple[date, Optional[str]]:
    """Map an Item 2.02 8-K acceptance time (naive Eastern) to (event_date, timing).

    event_date is the first session whose *open* is strictly after the acceptance
    time -- i.e. the session in which the market first reacts:

    - session day, before 09:30            -> same day, ``BMO``
    - session day, at/after the close      -> next session, ``AMC``
    - session day, 09:30 <= t < close      -> next session, timing ``None`` (unknown;
      the reaction may already have happened intraday, but the first *open* after
      is the next session)
    - weekend / holiday                    -> next session, ``BMO`` (published before
      that session's open)
    """
    d = accepted_et.date()
    t = accepted_et.time()
    if not is_trading_day(d):
        return next_trading_day(d), "BMO"
    if t < MARKET_OPEN:
        return d, "BMO"
    if t >= session_close_et(d):
        return next_trading_day(d), "AMC"
    return next_trading_day(d), None


def record_earnings_event(
    conn: sqlite3.Connection, tickers: list[str], accepted_et_iso: str
) -> Optional[date]:
    """Write earnings_dates rows (source ``8k_2.02``) for each ticker. If a row for the
    same (ticker, event_date) exists, the earliest acceptance wins. No commit.
    Returns the event date."""
    accepted = datetime.fromisoformat(accepted_et_iso)
    event_date, timing = earnings_event_from_acceptance(accepted)
    for tk in tickers:
        row = conn.execute(
            "SELECT acceptance_datetime FROM earnings_dates "
            "WHERE ticker=? AND event_date=? AND source=?",
            (tk, event_date.isoformat(), EARNINGS_SOURCE),
        ).fetchone()
        if row is not None and row[0] and row[0] <= accepted_et_iso:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO earnings_dates "
            "(ticker, event_date, timing, source, acceptance_datetime) VALUES (?,?,?,?,?)",
            (tk, event_date.isoformat(), timing, EARNINGS_SOURCE, accepted_et_iso),
        )
    return event_date


# ---------------------------------------------------------------------------
# Ticker mapping
# ---------------------------------------------------------------------------


def load_cik_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Zero-padded CIK -> tickers (sorted; a CIK may have several share classes)."""
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT ticker, cik FROM tickers WHERE cik IS NOT NULL AND cik != ''"):
        out.setdefault(str(row[1]).zfill(10), []).append(row[0])
    for v in out.values():
        v.sort()
    return out


def pick_ticker(candidates: list[str], symbol: str = "") -> str:
    """The issuer's own trading symbol if it is one of the CIK's tickers (also
    tolerating '-' / '.' share-class spelling), else the first ticker."""
    sym = symbol.upper()
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())  # noqa: E731
    for c in candidates:
        if c == sym or (sym and norm(c) == norm(sym)):
            return c
    return candidates[0]


# ---------------------------------------------------------------------------
# Per-filing handlers
# ---------------------------------------------------------------------------


def _insert_filing(
    conn: sqlite3.Connection,
    *,
    accession: str,
    cik: str,
    ticker: str,
    form: str,
    filed_date: str,
    acceptance: Optional[str],
    items: Optional[list[str]],
    primary_doc_url: Optional[str],
    exhibit_991_url: Optional[str],
    processed: int = 1,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO filings (accession, cik, ticker, form, filed_date, "
        "acceptance_datetime, items, primary_doc_url, exhibit_991_url, processed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            accession, cik, ticker, form, filed_date, acceptance,
            ",".join(items) if items else None, primary_doc_url, exhibit_991_url, processed,
        ),
    )


def _filing_index_url(cik: str, accession: str) -> str:
    return f"{BASE_ARCHIVES}/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"


def process_8k(
    conn: sqlite3.Connection,
    client: EdgarClient,
    entry: IndexEntry,
    tickers: list[str],
    report: dict[str, Any],
) -> None:
    """Fetch/parse one 8-K filing index, store it, derive earnings events."""
    acc = entry.accession
    idx = parse_filing_index(client.get_text(_filing_index_url(entry.cik, acc)))
    if idx.accepted_et is None:
        raise ValueError(f"no acceptance time on filing index for {acc}")

    _insert_filing(
        conn, accession=acc, cik=entry.cik, ticker=tickers[0], form=entry.form,
        filed_date=entry.filed_date, acceptance=idx.accepted_et, items=idx.items,
        primary_doc_url=idx.primary_doc_url, exhibit_991_url=idx.exhibit_991_url,
    )
    report["eightk_processed"] += 1
    # Amendments (8-K/A) restate an earlier report; only the original makes an event.
    if entry.form == "8-K" and "2.02" in idx.items:
        ev = record_earnings_event(conn, tickers, idx.accepted_et)
        report["earnings_events"] += len(tickers)
        log.info("earnings 8-K %s %s accepted %s -> event %s", tickers[0], acc, idx.accepted_et, ev)
    conn.commit()


def process_form4(
    conn: sqlite3.Connection,
    client: EdgarClient,
    entry: IndexEntry,
    cik_map: dict[str, list[str]],
    report: dict[str, Any],
) -> None:
    """Fetch/parse one Form 4 submission; store trades if the *issuer* is in scope."""
    acc = entry.accession
    text = client.get_text(f"https://www.sec.gov/Archives/{entry.file_name}")
    filing, accepted = form4.parse_form4_submission(text)
    candidates = cik_map.get(filing.issuer_cik)
    if not candidates:  # matched the index line via a reporting owner's CIK only
        report["form4_not_issuer"] += 1
        return
    ticker = pick_ticker(candidates, filing.issuer_symbol)

    n = form4.store_form4(conn, filing, acc, entry.filed_date, ticker)
    _insert_filing(
        conn, accession=acc, cik=filing.issuer_cik, ticker=ticker, form=entry.form,
        filed_date=entry.filed_date, acceptance=accepted, items=None,
        primary_doc_url=f"https://www.sec.gov/Archives/{entry.file_name}",
        exhibit_991_url=None,
    )
    conn.commit()
    report["form4_processed"] += 1
    report["insider_rows"] += n


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def process_day(
    conn: sqlite3.Connection,
    date_: date,
    client: Optional[EdgarClient] = None,
    lookback_days: int = 10,
) -> dict[str, Any]:
    """Process the EDGAR daily index for ``date_``. Returns a small report dict.

    If ``date_``'s index is not published yet (typical in the morning, HTTP 403) the
    client walks back to the latest published one (``latest_published_daily_index``);
    that is safe because already-processed accessions are skipped. The report's
    ``index_date`` says which day's index was actually read and ``index_is_requested_day``
    whether it is the requested one.
    """
    client = client or get_default_client()
    report: dict[str, Any] = {
        "requested_date": date_.isoformat(), "index_date": None,
        "index_is_requested_day": False,
        "form4_seen": 0, "form4_processed": 0, "form4_not_issuer": 0, "insider_rows": 0,
        "eightk_seen": 0, "eightk_processed": 0, "earnings_events": 0,
        "skipped_already_processed": 0, "errors": [],
    }

    index_date, text = client.latest_published_daily_index(date_, lookback_days)
    report["index_date"] = index_date.isoformat()
    report["index_is_requested_day"] = index_date == date_
    if index_date != date_:
        log.info("daily index for %s not published; using %s", date_, index_date)

    cik_map = load_cik_map(conn)
    entries = [e for e in parse_form_index(text, WANTED_FORMS) if e.cik in cik_map]

    done = {
        r[0]
        for r in conn.execute(
            "SELECT accession FROM filings WHERE processed = 1 AND filed_date = ?",
            (index_date.isoformat(),),
        )
    }

    seen: set[str] = set()
    for e in entries:
        acc = e.accession
        if acc in seen:
            continue
        seen.add(acc)
        is_4 = e.form in FORM4_FORMS
        report["form4_seen" if is_4 else "eightk_seen"] += 1
        if acc in done:
            report["skipped_already_processed"] += 1
            continue
        try:
            if is_4:
                process_form4(conn, client, e, cik_map, report)
            else:
                process_8k(conn, client, e, cik_map[e.cik], report)
        except Exception as exc:  # noqa: BLE001 -- one bad filing must not abort the day
            conn.rollback()
            msg = f"{e.form} {acc} ({e.company}): {type(exc).__name__}: {exc}"
            log.warning("process_day error: %s", msg)
            report["errors"].append(msg)

    log.info(
        "process_day %s (index %s): form4 %d/%d rows=%d, 8-K %d/%d earnings=%d, skipped=%d, errors=%d",
        date_, index_date, report["form4_processed"], report["form4_seen"],
        report["insider_rows"], report["eightk_processed"], report["eightk_seen"],
        report["earnings_events"], report["skipped_already_processed"], len(report["errors"]),
    )
    return report
