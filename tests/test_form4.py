"""Tests for src/data/form4.py -- real Form 4 submissions saved under tests/fixtures/edgar/."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.data import form4
from src.db import init_db

EDGAR = Path(__file__).resolve().parent / "fixtures" / "edgar"


def _load(name: str):
    return form4.parse_form4_submission((EDGAR / name).read_text(encoding="utf-8"))


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


def test_award_filing_basic_fields():
    f, accepted = _load("form4_awards.txt")
    assert accepted == "2026-09-29T06:57:39"  # header time is already Eastern
    assert f.document_type == "4" and not f.is_amendment
    assert f.issuer_cik == "0001738906" and f.issuer_symbol == "YI"
    assert len(f.transactions) == 9
    t = f.transactions[0]
    assert (t.trans_code, t.trans_date, t.shares, t.price, t.acquired_disposed) == (
        "A", "2023-09-08", 126295.0, 0.0, "A")
    assert t.value == 0.0
    o = f.owners[0]
    assert o.is_director and not o.is_officer and not o.is_ten_pct_owner
    assert o.cik == "0002121461"


def test_multi_owner_purchase_attributed_once_to_primary_owner(conn):
    f, _ = _load("form4_multi_owner_purchase.txt")
    assert len(f.owners) == 5 and all(o.is_ten_pct_owner for o in f.owners)
    assert [t.trans_code for t in f.transactions] == ["P", "P"]
    assert f.transactions[0].value == pytest.approx(1332742 * 5.0639, abs=0.01)
    assert f.transactions[0].acquired_disposed == "A"

    n = form4.store_form4(conn, f, "0000921895-26-002677", "2026-09-29", "COUR")
    assert n == 2  # 2 transactions, NOT 2 x 5 owners
    rows = conn.execute("SELECT * FROM insider_trades ORDER BY row_num").fetchall()
    assert [r["row_num"] for r in rows] == [1, 2]
    assert rows[0]["insider_name"] == "Pale Fire Capital SE"
    assert rows[0]["is_ten_pct_owner"] == 1 and rows[0]["is_officer"] == 0
    assert rows[0]["cik"] == "0001651562" and rows[0]["ticker"] == "COUR"
    assert rows[0]["value"] == pytest.approx(6748872.21, abs=0.01)


def test_weighted_average_price_uses_numeric_value():
    f, _ = _load("form4_weighted_price.txt")
    t = f.transactions[0]
    assert t.trans_code == "P" and t.price == 0.102
    assert t.value == pytest.approx(85963.05, abs=0.01)
    assert f.owners[0].is_other and not f.owners[0].is_officer


def test_missing_price_gives_null_value(conn):
    f, _ = _load("form4_purchase.txt")
    t = f.transactions[0]
    assert t.trans_code == "C" and t.price is None and t.value is None
    form4.store_form4(conn, f, "0001193125-26-408061", "2026-09-29", "ADRX")
    r = conn.execute("SELECT price, value, shares FROM insider_trades WHERE row_num=1").fetchone()
    assert r["price"] is None and r["value"] is None and r["shares"] == 3334938.0


_XML = """<ownershipDocument><documentType>{doctype}</documentType>
<periodOfReport>2024-05-02</periodOfReport>{orig}
<issuer><issuerCik>0000012345</issuerCik><issuerName>Acme</issuerName>
<issuerTradingSymbol>acme</issuerTradingSymbol></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000000111</rptOwnerCik><rptOwnerName>Fund LP</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isTenPercentOwner>true</isTenPercentOwner></reportingOwnerRelationship></reportingOwner>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000000222</rptOwnerCik><rptOwnerName>Jane CFO</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Financial Officer</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle>
<transactionDate><value>2024-05-01</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
<transactionPricePerShare><value>10.50</value><footnoteId id="F1"/></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction>
<nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle>
<transactionDate><value>2024-05-02</value></transactionDate>
<transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1,000</value></transactionShares>
<transactionPricePerShare><footnoteId id="F2"/></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable>
<derivativeTable><derivativeTransaction><transactionCoding><transactionCode>M</transactionCode></transactionCoding></derivativeTransaction></derivativeTable>
</ownershipDocument>"""


def _xml(doctype="4", shares="2000", orig=""):
    return _XML.format(doctype=doctype, shares=shares, orig=orig)


def test_primary_owner_prefers_officer_and_derivatives_ignored(conn):
    f = form4.parse_form4_xml(_xml())
    assert f.issuer_symbol == "ACME" and len(f.transactions) == 2  # derivative row ignored
    po = f.primary_owner()
    assert po.name == "Jane CFO" and po.is_officer and po.is_director
    assert po.officer_title == "Chief Financial Officer"
    assert f.transactions[0].value == 21000.0
    assert f.transactions[1].shares == 1000.0 and f.transactions[1].value is None  # footnote-only price
    form4.store_form4(conn, f, "A-1", "2024-05-03", "ACME")
    r = conn.execute("SELECT * FROM insider_trades WHERE row_num=1").fetchone()
    assert r["officer_title"] == "Chief Financial Officer" and r["is_director"] == 1


def test_store_is_idempotent(conn):
    f = form4.parse_form4_xml(_xml())
    form4.store_form4(conn, f, "A-1", "2024-05-03", "ACME")
    form4.store_form4(conn, f, "A-1", "2024-05-03", "ACME")
    assert conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0] == 2


def test_amendment_supersedes_original(conn):
    orig = form4.parse_form4_xml(_xml(shares="2000"))
    form4.store_form4(conn, orig, "ORIG-1", "2024-05-03", "ACME")
    other = form4.parse_form4_xml(_xml().replace("2024-05-01", "2024-03-01").replace("2024-05-02", "2024-03-02"))
    form4.store_form4(conn, other, "OTHER-1", "2024-03-04", "ACME")  # unrelated earlier filing

    amend = form4.parse_form4_xml(
        _xml(doctype="4/A", shares="2500",
             orig="<dateOfOriginalSubmission>2024-05-03</dateOfOriginalSubmission>"))
    assert amend.is_amendment and amend.original_submission_date == "2024-05-03"
    form4.store_form4(conn, amend, "AMEND-1", "2024-05-10", "ACME")

    accs = {r[0] for r in conn.execute("SELECT DISTINCT accession FROM insider_trades")}
    assert accs == {"OTHER-1", "AMEND-1"}
    assert conn.execute(
        "SELECT shares FROM insider_trades WHERE accession='AMEND-1' AND row_num=1").fetchone()[0] == 2500.0


def test_real_amendment_parses():
    f, _ = _load("form4_amendment.txt")
    assert f.is_amendment and f.original_submission_date == "2023-10-02"
    assert f.primary_owner().officer_title == "CEO AND PRESIDENT"


def test_missing_xml_raises():
    with pytest.raises(ValueError):
        form4.parse_form4_submission("<SEC-DOCUMENT>nothing here</SEC-DOCUMENT>")
