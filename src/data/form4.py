"""Form 4 / 4-A parsing and storage into ``insider_trades``.

Pure parsing (no network) lives here so it can be unit-tested against saved
filings; fetching is done by ``edgar_daily`` through ``edgar_client``.

Conventions (see docs/status/BOT_3.md):

- Only the **non-derivative** transaction table is read (open-market buys/sells,
  awards, gifts, ...). Derivative rows and pure holdings are ignored.
- ``insider_trades`` has one insider per row. A Form 4 may list several reporting
  owners (joint filings, e.g. an officer plus the fund they control). Every
  transaction is stored **once**, attributed to the *primary* owner: the first
  owner who is an officer or director, otherwise the first owner. This keeps
  value totals from being double counted by the cluster-buying module.
- ``value`` = shares x price. If the price is missing (price only in a footnote,
  or blank) ``price`` and ``value`` are NULL -- never guessed. A price given as a
  weighted average is still just the numeric value in ``transactionPricePerShare``.
- A Form 4/A supersedes the original it amends: the original accession's rows for
  the same issuer/insider/transaction dates are deleted when the amendment is stored.
"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class ReportingOwner:
    cik: str
    name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_pct_owner: bool = False
    is_other: bool = False
    officer_title: str = ""


@dataclass
class Transaction:
    security_title: str
    trans_code: str
    trans_date: Optional[str]  # ISO date
    shares: Optional[float]
    price: Optional[float]
    acquired_disposed: Optional[str]  # "A" / "D"

    @property
    def value(self) -> Optional[float]:
        if self.shares is None or self.price is None:
            return None
        return round(self.shares * self.price, 2)


@dataclass
class Form4Filing:
    document_type: str  # "4" or "4/A"
    period_of_report: Optional[str]
    original_submission_date: Optional[str]  # set on amendments
    issuer_cik: str  # zero-padded 10
    issuer_name: str
    issuer_symbol: str  # upper-case, may be ""
    owners: list[ReportingOwner] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)

    @property
    def is_amendment(self) -> bool:
        return self.document_type.upper().endswith("/A")

    def primary_owner(self) -> Optional[ReportingOwner]:
        """Officer/director first, else the first listed owner."""
        for o in self.owners:
            if o.is_officer or o.is_director:
                return o
        return self.owners[0] if self.owners else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_XML_BLOCK = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.S | re.I)
_ACCEPT_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")


def extract_ownership_xml(submission_text: str) -> Optional[str]:
    """Pull the ownershipDocument XML out of a complete submission ``.txt``."""
    for m in _XML_BLOCK.finditer(submission_text):
        block = m.group(1)
        if "<ownershipDocument" in block:
            # drop the XML declaration (ElementTree rejects str input with encoding)
            return re.sub(r"^<\?xml[^>]*\?>\s*", "", block)
    return None


def header_acceptance_et(submission_text: str) -> Optional[str]:
    """ACCEPTANCE-DATETIME from a submission ``.txt`` header, as naive ET ISO text.

    The ``.txt`` header (like the filing index page) is already Eastern time --
    unlike the submissions JSON API, which is UTC.
    """
    m = _ACCEPT_RE.search(submission_text)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").isoformat()


def _text(node: Optional[ET.Element], path: str) -> str:
    if node is None:
        return ""
    el = node.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def _flag(node: Optional[ET.Element], path: str) -> bool:
    return _text(node, path).lower() in ("1", "true")


def _num(s: str) -> Optional[float]:
    s = s.strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _pad_cik(s: str) -> str:
    s = s.strip()
    return s.zfill(10) if s.isdigit() else s


def parse_form4_xml(xml_text: str) -> Form4Filing:
    """Parse an ``ownershipDocument`` (Form 4 or 4/A) into a ``Form4Filing``."""
    root = ET.fromstring(xml_text.strip())
    issuer = root.find("issuer")
    filing = Form4Filing(
        document_type=_text(root, "documentType") or "4",
        period_of_report=_text(root, "periodOfReport") or None,
        original_submission_date=(_text(root, "dateOfOriginalSubmission") or None),
        issuer_cik=_pad_cik(_text(issuer, "issuerCik")),
        issuer_name=_text(issuer, "issuerName"),
        issuer_symbol=_text(issuer, "issuerTradingSymbol").upper(),
    )

    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        filing.owners.append(
            ReportingOwner(
                cik=_pad_cik(_text(ro, "reportingOwnerId/rptOwnerCik")),
                name=_text(ro, "reportingOwnerId/rptOwnerName"),
                is_director=_flag(rel, "isDirector"),
                is_officer=_flag(rel, "isOfficer"),
                is_ten_pct_owner=_flag(rel, "isTenPercentOwner"),
                is_other=_flag(rel, "isOther"),
                officer_title=_text(rel, "officerTitle"),
            )
        )

    table = root.find("nonDerivativeTable")
    if table is not None:
        for tx in table.findall("nonDerivativeTransaction"):
            code = _text(tx, "transactionCoding/transactionCode")
            if not code:
                continue
            filing.transactions.append(
                Transaction(
                    security_title=_text(tx, "securityTitle/value"),
                    trans_code=code.upper(),
                    trans_date=_text(tx, "transactionDate/value")[:10] or None,
                    shares=_num(_text(tx, "transactionAmounts/transactionShares/value")),
                    price=_num(_text(tx, "transactionAmounts/transactionPricePerShare/value")),
                    acquired_disposed=(
                        _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value").upper()
                        or None
                    ),
                )
            )
    return filing


def parse_form4_submission(submission_text: str) -> tuple[Form4Filing, Optional[str]]:
    """Parse a complete submission ``.txt``; returns (filing, acceptance_et_iso)."""
    xml = extract_ownership_xml(submission_text)
    if xml is None:
        raise ValueError("no ownershipDocument XML found in submission text")
    return parse_form4_xml(xml), header_acceptance_et(submission_text)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_INSERT_SQL = (
    "INSERT OR REPLACE INTO insider_trades (accession, row_num, cik, ticker, insider_cik, "
    "insider_name, is_officer, is_director, is_ten_pct_owner, officer_title, trans_code, "
    "trans_date, shares, price, value, acquired_disposed, filed_date) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def build_rows(
    filing: Form4Filing, accession: str, filed_date: str, ticker: Optional[str]
) -> list[tuple]:
    """insider_trades rows (one per non-derivative transaction, primary owner)."""
    owner = filing.primary_owner()
    if owner is None:
        return []
    rows = []
    for i, t in enumerate(filing.transactions, start=1):
        rows.append(
            (
                accession,
                i,
                filing.issuer_cik,
                ticker,
                owner.cik,
                owner.name,
                int(owner.is_officer),
                int(owner.is_director),
                int(owner.is_ten_pct_owner),
                owner.officer_title or None,
                t.trans_code,
                t.trans_date,
                t.shares,
                t.price,
                t.value,
                t.acquired_disposed,
                filed_date,
            )
        )
    return rows


def supersede_originals(
    conn: sqlite3.Connection,
    amend_accession: str,
    issuer_cik: str,
    insider_cik: str,
    trans_dates: set[str],
    amend_filed_date: str,
    original_date: Optional[str] = None,
) -> int:
    """Delete rows of earlier accessions that a 4/A restates. Returns rows deleted.

    An accession is superseded if it has the same issuer and insider, was filed on or
    before the amendment (and on ``original_date`` when the amendment states it),
    is not itself an amendment, and shares at least one transaction date with it.
    """
    if not trans_dates:
        return 0
    cur = conn.execute(
        "SELECT DISTINCT it.accession FROM insider_trades it "
        "LEFT JOIN filings f ON f.accession = it.accession "
        "WHERE it.cik = ? AND it.insider_cik = ? AND it.accession != ? "
        "AND it.filed_date <= ? AND (f.form IS NULL OR f.form NOT LIKE '%/A') "
        "AND (? IS NULL OR it.filed_date = ?) "
        "AND it.trans_date IN (" + ",".join("?" * len(trans_dates)) + ")",
        (issuer_cik, insider_cik, amend_accession, amend_filed_date,
         original_date, original_date, *sorted(trans_dates)),
    )
    accs = [r[0] for r in cur.fetchall()]
    deleted = 0
    for acc in accs:
        deleted += conn.execute(
            "DELETE FROM insider_trades WHERE accession = ?", (acc,)
        ).rowcount
    if accs:
        log.info("4/A %s supersedes %s (%d rows removed)", amend_accession, accs, deleted)
    return deleted


def store_form4(
    conn: sqlite3.Connection,
    filing: Form4Filing,
    accession: str,
    filed_date: str,
    ticker: Optional[str],
) -> int:
    """Write the filing's transactions (replacing any prior rows for the same
    accession) and apply 4/A supersession. Returns rows written. Does not commit."""
    conn.execute("DELETE FROM insider_trades WHERE accession = ?", (accession,))
    rows = build_rows(filing, accession, filed_date, ticker)
    if rows:
        conn.executemany(_INSERT_SQL, rows)
    if filing.is_amendment and rows:
        owner = filing.primary_owner()
        supersede_originals(
            conn,
            accession,
            filing.issuer_cik,
            owner.cik,
            {t.trans_date for t in filing.transactions if t.trans_date},
            filed_date,
            filing.original_submission_date,
        )
    return len(rows)
