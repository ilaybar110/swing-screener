"""Local-only historical backfill from SEC bulk files (never run in the cloud routine).

``backfill(conn, start_year=2014)`` fills the research DB in two resumable stages:

1. ``submissions.zip`` (~1.5 GB, one file per CIK) -> ``filings`` rows (10-K, 10-Q,
   8-K with items + acceptance time, Form 4/4-A index) for every CIK in ``tickers``,
   then ``earnings_dates`` rows (source ``8k_2.02``) from 8-Ks with Item 2.02 using the
   same acceptance-time rule as the daily job (``edgar_daily``).
2. The quarterly *Insider Transactions Data Sets* (``<year>q<n>_form345.zip``) ->
   ``insider_trades`` in exactly the same shape ``edgar_daily`` writes (one row per
   non-derivative transaction, attributed to the primary reporting owner, 4/A
   superseding the original).

Resumability: downloads go to ``<raw_cache_dir>/bulk/`` with HTTP Range resume, an
already complete file is never fetched again, every stage/quarter that finishes is
recorded in ``job_log`` (job ``edgar_bulk:<stage>``) and skipped next time, and all
writes are idempotent (INSERT OR IGNORE / REPLACE), so an interrupted run can simply be
started again.

Time zones: bulk submissions carry UTC acceptance times, converted to naive Eastern
(see ``edgar_daily``). Insider data sets have no acceptance time; ``filings.
acceptance_datetime`` stays NULL for those Form 4s.

CLI::

    python -m src.data.edgar_bulk backfill [--start-year 2014]
    python -m src.data.edgar_bulk validate [TICKER ...]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import time as _time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from src.data import form4
from src.data.edgar_client import BASE_ARCHIVES, EdgarClient
from src.data.edgar_daily import (
    get_default_client,
    load_cik_map,
    pick_ticker,
    record_earnings_event,
    utc_to_et_naive,
)
from src.utils.logging import get_logger

log = get_logger(__name__)

SUBMISSIONS_ZIP_URL = f"{BASE_ARCHIVES}/daily-index/bulkdata/submissions.zip"
INSIDER_ZIP_URL = (
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
    "{year}q{q}_form345.zip"
)
# Form 4 index rows stay processed=0 until the insider data set covers them
FORM4_FORMS = {"4", "4/A"}
BULK_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "4", "4/A"}
VALIDATION_TICKERS = ["INTC", "NKE", "PYPL", "BA", "DIS"]

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ---------------------------------------------------------------------------
# Download helper (resumable)
# ---------------------------------------------------------------------------


def download_file(client: EdgarClient, url: str, dest: Path, attempts: int = 5) -> bool:
    """Stream ``url`` to ``dest`` through the EDGAR client's session + throttle, resuming
    a partial ``<dest>.part`` with a Range request. Returns False if the file does not
    exist on the server (403/404), True once ``dest`` is complete.

    (edgar_client's own getters buffer whole responses in memory and the raw cache, which
    is unsuitable for a 1.5 GB zip -- see docs/CHANGE_REQUESTS.md; this helper still goes
    through the client's session, User-Agent and rate limiter.)
    """
    dest = Path(dest)
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    for attempt in range(1, attempts + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            client._throttle()  # noqa: SLF001 -- shared 4 req/s limiter
            with client._session.get(url, headers=headers, stream=True, timeout=(15, 120)) as r:  # noqa: SLF001
                if r.status_code in (403, 404):
                    log.info("bulk file not available: %s (HTTP %s)", url, r.status_code)
                    return False
                if r.status_code == 416:  # nothing left to fetch -> partial file is complete
                    part.replace(dest)
                    return True
                r.raise_for_status()
                mode = "ab" if (have and r.status_code == 206) else "wb"
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            part.replace(dest)
            log.info("downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
            return True
        except Exception as exc:  # noqa: BLE001 -- retry/resume
            log.warning("download of %s failed (attempt %d/%d): %s", url, attempt, attempts, exc)
            _time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"could not download {url} after {attempts} attempts")


# ---------------------------------------------------------------------------
# job_log based stage tracking
# ---------------------------------------------------------------------------


def _job_done(conn: sqlite3.Connection, job: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM job_log WHERE job = ? AND status = 'ok' LIMIT 1", (job,)
    ).fetchone() is not None


def _mark_job(conn: sqlite3.Connection, job: str, started: str, message: str) -> None:
    conn.execute(
        "INSERT INTO job_log (run_id, job, trading_date, started_at, finished_at, status, message) "
        "VALUES (?,?,?,?,?,?,?)",
        ("edgar_bulk", job, None, started, datetime.now().isoformat(timespec="seconds"), "ok", message),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Stage 1: submissions.zip
# ---------------------------------------------------------------------------


def _filing_rows_from_submission_json(data: dict) -> Iterator[dict[str, Any]]:
    """Yield one dict per filing from a submissions JSON (``filings.recent`` columnar
    arrays, or the top-level arrays of a ``-submissions-NNN.json`` overflow file)."""
    block = data.get("filings", {}).get("recent") if "filings" in data else data
    if not block or "accessionNumber" not in block:
        return
    n = len(block["accessionNumber"])
    cols = {k: v for k, v in block.items() if isinstance(v, list) and len(v) == n}
    for i in range(n):
        yield {k: v[i] for k, v in cols.items()}


def _load_submissions_for_cik(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    names: set[str],
    cik: str,
    tickers: list[str],
    start_iso: str,
    stats: dict[str, int],
) -> None:
    members = [n for n in (f"CIK{cik}.json",) if n in names]
    members += sorted(n for n in names if n.startswith(f"CIK{cik}-submissions-"))
    cik_int = int(cik)
    for member in members:
        with zf.open(member) as fh:
            data = json.load(fh)
        for f in _filing_rows_from_submission_json(data):
            form = f.get("form")
            filed = f.get("filingDate") or ""
            if form not in BULK_FORMS or filed < start_iso:
                continue
            acc = f["accessionNumber"]
            accepted = utc_to_et_naive(f["acceptanceDateTime"]) if f.get("acceptanceDateTime") else None
            items = ",".join(i.strip() for i in (f.get("items") or "").split(",") if i.strip()) or None
            doc = f.get("primaryDocument")
            url = (f"{BASE_ARCHIVES}/data/{cik_int}/{acc.replace('-', '')}/{doc}" if doc else None)
            is_form4 = form in FORM4_FORMS
            conn.execute(
                "INSERT OR IGNORE INTO filings (accession, cik, ticker, form, filed_date, "
                "acceptance_datetime, items, primary_doc_url, exhibit_991_url, processed) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,?)",
                (acc, cik, tickers[0], form, filed, accepted, items, url, 0 if is_form4 else 1),
            )
            stats["filings"] += 1
            if form == "8-K" and items and "2.02" in items.split(",") and accepted:
                record_earnings_event(conn, tickers, accepted)
                stats["earnings_events"] += 1


def backfill_submissions(
    conn: sqlite3.Connection, zip_path: Path, start_year: int, ciks: dict[str, list[str]]
) -> dict[str, int]:
    """Load filings + earnings events for ``ciks`` from a local ``submissions.zip``."""
    stats = {"ciks": 0, "ciks_missing": 0, "filings": 0, "earnings_events": 0}
    start_iso = f"{start_year}-01-01"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        for n, (cik, tickers) in enumerate(sorted(ciks.items()), 1):
            if f"CIK{cik}.json" not in names:
                stats["ciks_missing"] += 1
                continue
            _load_submissions_for_cik(conn, zf, names, cik, tickers, start_iso, stats)
            stats["ciks"] += 1
            if n % 200 == 0:
                conn.commit()
                log.info("submissions: %d/%d CIKs, %d filings", n, len(ciks), stats["filings"])
    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Stage 2: Insider Transactions Data Sets
# ---------------------------------------------------------------------------


def _parse_dmy(s: str) -> Optional[str]:
    """'31-JAN-2024' -> '2024-01-31' (locale independent)."""
    s = (s or "").strip()
    try:
        d, m, y = s.split("-")
        return date(int(y), _MONTHS[m.upper()], int(d)).isoformat()
    except (ValueError, KeyError):
        return None


def _num(s: str) -> Optional[float]:
    s = (s or "").strip().replace(",", "")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _tsv(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""),
                                delimiter="\t", quoting=csv.QUOTE_NONE)
        for row in reader:
            yield row


def _owner_from_row(row: dict[str, str]) -> form4.ReportingOwner:
    rel = {t.strip().lower() for t in (row.get("RPTOWNER_RELATIONSHIP") or "").split(",")}
    cik = (row.get("RPTOWNER_CIK") or row.get("RPTOWNERCIK") or "").strip()
    return form4.ReportingOwner(
        cik=cik.zfill(10) if cik.isdigit() else cik,
        name=(row.get("RPTOWNERNAME") or row.get("RPTOWNER_NAME") or "").strip(),
        is_director="director" in rel,
        is_officer="officer" in rel,
        is_ten_pct_owner="tenpercentowner" in rel,
        is_other="other" in rel,
        officer_title=(row.get("RPTOWNER_TITLE") or "").strip(),
    )


def load_insider_zip(
    conn: sqlite3.Connection, zip_path: Path, cik_map: dict[str, list[str]]
) -> dict[str, int]:
    """Load one quarterly data set into insider_trades (issuers in ``cik_map`` only)."""
    stats = {"submissions": 0, "rows": 0}
    with zipfile.ZipFile(zip_path) as zf:
        subs: dict[str, dict[str, str]] = {}
        for r in _tsv(zf, "SUBMISSION.tsv"):
            cik = (r.get("ISSUERCIK") or "").strip().zfill(10)
            if r.get("DOCUMENT_TYPE") in FORM4_FORMS and cik in cik_map:
                subs[r["ACCESSION_NUMBER"]] = r
        owners: dict[str, list[form4.ReportingOwner]] = {}
        for r in _tsv(zf, "REPORTINGOWNER.tsv"):
            if r["ACCESSION_NUMBER"] in subs:
                owners.setdefault(r["ACCESSION_NUMBER"], []).append(_owner_from_row(r))
        trans: dict[str, list[tuple[int, form4.Transaction]]] = {}
        for r in _tsv(zf, "NONDERIV_TRANS.tsv"):
            acc = r["ACCESSION_NUMBER"]
            code = (r.get("TRANS_CODE") or "").strip().upper()
            if acc not in subs or not code:
                continue
            trans.setdefault(acc, []).append((
                int(r.get("NONDERIV_TRANS_SK") or 0),
                form4.Transaction(
                    security_title=(r.get("SECURITY_TITLE") or "").strip(),
                    trans_code=code,
                    trans_date=_parse_dmy(r.get("TRANS_DATE", "")),
                    shares=_num(r.get("TRANS_SHARES", "")),
                    price=_num(r.get("TRANS_PRICEPERSHARE", "")),
                    acquired_disposed=((r.get("TRANS_ACQUIRED_DISP_CD") or "").strip().upper() or None),
                ),
            ))

    # originals before amendments so 4/A supersession finds its target
    order = sorted(subs.values(), key=lambda r: (r["DOCUMENT_TYPE"] == "4/A",
                                                 _parse_dmy(r.get("FILING_DATE", "")) or "",
                                                 r["ACCESSION_NUMBER"]))
    for r in order:
        acc = r["ACCESSION_NUMBER"]
        cik = r["ISSUERCIK"].strip().zfill(10)
        filed = _parse_dmy(r.get("FILING_DATE", ""))
        if not filed:
            continue
        filing = form4.Form4Filing(
            document_type=r["DOCUMENT_TYPE"],
            period_of_report=_parse_dmy(r.get("PERIOD_OF_REPORT", "")),
            original_submission_date=_parse_dmy(r.get("DATE_OF_ORIG_SUB", "")),
            issuer_cik=cik,
            issuer_name=(r.get("ISSUERNAME") or "").strip(),
            issuer_symbol=(r.get("ISSUERTRADINGSYMBOL") or "").strip().upper(),
            owners=owners.get(acc, []),
            transactions=[t for _, t in sorted(trans.get(acc, []), key=lambda x: x[0])],
        )
        ticker = pick_ticker(cik_map[cik], filing.issuer_symbol)
        stats["rows"] += form4.store_form4(conn, filing, acc, filed, ticker)
        stats["submissions"] += 1
        # make the filings index consistent: row exists and is marked processed
        updated = conn.execute("UPDATE filings SET processed = 1 WHERE accession = ?", (acc,)).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO filings (accession, cik, ticker, form, filed_date, processed) "
                "VALUES (?,?,?,?,?,1)", (acc, cik, ticker, r["DOCUMENT_TYPE"], filed))
    conn.commit()
    return stats


def _quarters(start_year: int, through: date) -> Iterator[tuple[int, int]]:
    y, q = start_year, 1
    while (y, q) <= (through.year, (through.month - 1) // 3 + 1):
        yield y, q
        q += 1
        if q == 5:
            y, q = y + 1, 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _check_local_db(conn: sqlite3.Connection) -> None:
    """Refuse to run against the cloud's ephemeral data/run.db."""
    try:
        from src.config import load_config

        run_db = Path(load_config().paths.run_db).resolve()
        for _, name, file in conn.execute("PRAGMA database_list"):
            if name == "main" and file and Path(file).resolve() == run_db:
                raise RuntimeError("edgar_bulk.backfill is local-only; refusing to run on data/run.db")
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001 -- config unavailable: nothing to compare against
        return


def backfill(
    conn: sqlite3.Connection,
    start_year: int = 2014,
    client: Optional[EdgarClient] = None,
    bulk_dir: Optional[Path] = None,
    through: Optional[date] = None,
) -> dict[str, Any]:
    """Resumable local backfill (see module docstring). Returns a stats dict.

    ``bulk_dir`` defaults to ``<raw_cache_dir>/bulk``; files already there are reused.
    ``through`` bounds the insider quarters tried (default today).
    """
    _check_local_db(conn)
    ciks = load_cik_map(conn)
    if not ciks:
        raise RuntimeError("tickers table has no CIKs -- run the universe build first")
    if bulk_dir is None:
        from src.config import load_config

        bulk_dir = Path(load_config().paths.raw_cache_dir) / "bulk"
    bulk_dir = Path(bulk_dir)
    through = through or date.today()
    report: dict[str, Any] = {"submissions": None, "insider_quarters": {}, "skipped": []}

    def _client() -> EdgarClient:
        nonlocal client
        client = client or get_default_client()
        return client

    # -- stage 1
    job = f"edgar_bulk:submissions:{start_year}"
    if _job_done(conn, job):
        report["skipped"].append(job)
    else:
        started = datetime.now().isoformat(timespec="seconds")
        zip_path = bulk_dir / "submissions.zip"
        if not download_file(_client(), SUBMISSIONS_ZIP_URL, zip_path):
            raise RuntimeError("submissions.zip is not available from SEC")
        stats = backfill_submissions(conn, zip_path, start_year, ciks)
        report["submissions"] = stats
        _mark_job(conn, job, started, json.dumps(stats))
        log.info("submissions stage done: %s", stats)

    # -- stage 2
    for year, q in _quarters(start_year, through):
        tag = f"{year}q{q}"
        job = f"edgar_bulk:insider:{tag}"
        if _job_done(conn, job):
            report["skipped"].append(job)
            continue
        started = datetime.now().isoformat(timespec="seconds")
        path = bulk_dir / "insider" / f"{tag}_form345.zip"
        if not path.exists() and not download_file(_client(), INSIDER_ZIP_URL.format(year=year, q=q), path):
            log.info("insider data set %s not published yet; stopping", tag)
            break
        stats = load_insider_zip(conn, path, ciks)
        report["insider_quarters"][tag] = stats
        _mark_job(conn, job, started, json.dumps(stats))
        log.info("insider %s: %s", tag, stats)
    return report


# ---------------------------------------------------------------------------
# Manual validation helper
# ---------------------------------------------------------------------------


def validate_purchases(
    conn: sqlite3.Connection, tickers: Optional[Iterable[str]] = None, limit: int = 8
) -> str:
    """Render the most recent open-market purchases (code P) per ticker, with EDGAR
    links, to compare by eye with EDGAR's own insider pages."""
    out: list[str] = []
    for tk in list(tickers or VALIDATION_TICKERS):
        cik = conn.execute("SELECT cik FROM tickers WHERE ticker = ?", (tk,)).fetchone()
        header = f"=== {tk}" + (f"  (CIK {cik[0]})" if cik and cik[0] else "  (not in tickers table)")
        out.append(header)
        if cik and cik[0]:
            out.append(f"    EDGAR: https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK={cik[0]}")
        rows = conn.execute(
            "SELECT * FROM insider_trades WHERE ticker = ? AND trans_code = 'P' "
            "ORDER BY trans_date DESC, accession DESC, row_num LIMIT ?", (tk, limit)).fetchall()
        if not rows:
            out.append("    (no open-market purchases in DB)")
        for r in rows:
            role = "/".join(x for x, f in (("Officer", r["is_officer"]), ("Director", r["is_director"]),
                                           ("10%", r["is_ten_pct_owner"])) if f) or "Other"
            val = f"${r['value']:,.0f}" if r["value"] is not None else "n/a"
            px = f"{r['price']:.4f}" if r["price"] is not None else "n/a"
            acc_nd = r["accession"].replace("-", "")
            out.append(
                f"    {r['trans_date']}  filed {r['filed_date']}  {r['insider_name'][:28]:28s} "
                f"{role:16s} {r['shares'] or 0:>12,.0f} sh @ {px:>9s} = {val:>14s}")
            out.append(
                f"        https://www.sec.gov/Archives/edgar/data/{int(r['cik'])}/{acc_nd}/{r['accession']}-index.htm")
    return "\n".join(out)


def _main(argv: Optional[list[str]] = None) -> int:
    from src.config import load_config
    from src.db import init_db
    from src.utils.logging import setup_logging

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="load bulk SEC data into the research DB")
    b.add_argument("--start-year", type=int, default=2014)
    v = sub.add_parser("validate", help="print recent open-market purchases for manual checking")
    v.add_argument("tickers", nargs="*", default=VALIDATION_TICKERS)
    v.add_argument("--limit", type=int, default=8)
    args = ap.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.paths.logs_dir)
    conn = init_db(cfg.paths.research_db)
    try:
        if args.cmd == "backfill":
            print(json.dumps(backfill(conn, args.start_year), indent=2))
        else:
            print(validate_purchases(conn, args.tickers, args.limit))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
