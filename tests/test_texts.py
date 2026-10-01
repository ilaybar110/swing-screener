"""Tests for src/data/texts.py (no network)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.data import texts
from src.db import init_db

EDGAR = Path(__file__).resolve().parent / "fixtures" / "edgar"
AAPL_PRIMARY = (EDGAR / "aapl_8k_primary.htm").read_text(encoding="utf-8")

MULTI_ITEM_DOC = """<html><body>
<p>FORM 8-K</p>
<p><b>Item 5.02 Departure of Directors or Certain Officers</b></p>
<p>On May&nbsp;3, 2024, Jane Doe resigned as CFO.</p><p>John Roe was appointed.</p>
<p><b>Item 8.01 Other Events.</b></p><div>The Board authorized a $1B buyback.</div>
<p><b>Item 9.01 Financial Statements and Exhibits</b></p><p>(d) Exhibits</p>
<p>SIGNATURE</p><p>Pursuant to the requirements ... duly authorized.</p>
</body></html>"""

EX99 = "<html><head><style>p{x}</style></head><body><h1>ACME Reports Q1</h1><p>Revenue rose 12%.</p><script>var a=1;</script></body></html>"


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_text(self, url, use_cache=True):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError("404")
        return self.pages[url]


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


def _filing(conn, acc, filed, items, primary="P", ex99=None, ticker="ACME", form="8-K"):
    conn.execute(
        "INSERT INTO filings (accession, cik, ticker, form, filed_date, items, primary_doc_url, "
        "exhibit_991_url, processed) VALUES (?,?,?,?,?,?,?,?,1)",
        (acc, "0000000001", ticker, form, filed, items, primary, ex99),
    )
    conn.commit()


def test_html_to_text_real_inline_xbrl_filing():
    t = texts.html_to_text(AAPL_PRIMARY)
    assert t.startswith("UNITED STATES")
    assert "us-gaap:CommonStockMember" not in t  # hidden ix:header dropped
    assert "Apple Inc." in t and "<" not in t


def test_html_to_text_basic_cleaning():
    t = texts.html_to_text(EX99)
    assert t == "ACME Reports Q1\n\nRevenue rose 12%."
    assert texts.html_to_text("   ") == ""
    assert texts.html_to_text("plain text\n\n\n\nbody") == "plain text\n\nbody"


def test_split_items_real_filing():
    items = texts.split_items(texts.html_to_text(AAPL_PRIMARY))
    assert list(items) == ["2.02"]
    assert "third fiscal quarter" in items["2.02"] and "Exhibit 99.1" in items["2.02"]


def test_split_items_multi_item_stops_at_next_item_and_signature():
    items = texts.split_items(texts.html_to_text(MULTI_ITEM_DOC))
    assert set(items) == {"5.02", "8.01"}  # 9.01 is not a wanted item
    assert "Jane Doe resigned as CFO." in items["5.02"] and "May 3, 2024" in items["5.02"]
    assert "buyback" not in items["5.02"]
    assert items["8.01"].endswith("$1B buyback.") and "Exhibits" not in items["8.01"]


def test_split_items_longest_duplicate_wins():
    doc = "Item 1.01 Entry\nItem 8.01 Other\nItem 1.01 Entry into Agreement\nWe signed a deal with X.\n"
    assert "signed a deal" in texts.split_items(doc)["1.01"]


def test_get_8k_texts_fetch_cache_and_point_in_time(conn):
    _filing(conn, "A-1", "2024-05-03", "5.02,8.01,9.01", primary="http://p1", ex99="http://e1")
    _filing(conn, "A-2", "2024-04-01", "2.02", primary="http://p2")           # outside 30d window
    _filing(conn, "A-3", "2024-05-20", "8.01", primary="http://p3")           # filed after as_of
    _filing(conn, "A-4", "2024-05-04", "9.01", primary="http://p4")           # no wanted item
    _filing(conn, "A-5", "2024-05-02", "5.07", primary="http://p5")           # unwanted item
    _filing(conn, "B-1", "2024-05-03", "8.01", primary="http://pb", ticker="OTHR")
    client = FakeClient({"http://p1": MULTI_ITEM_DOC, "http://e1": EX99})

    out = texts.get_8k_texts(conn, "ACME", date(2024, 5, 10), days=30, client=client)
    assert [(r["accession"], r["section"]) for r in out] == [
        ("A-1", "item_5.02"), ("A-1", "item_8.01"), ("A-1", "EX-99.1")]
    assert out[0]["filed_date"] == "2024-05-03" and out[0]["items"] == ["5.02", "8.01", "9.01"]
    assert out[2]["text"].startswith("ACME Reports Q1")
    assert client.calls == ["http://p1", "http://e1"]  # only the in-scope filing was fetched

    # cached: second call does no HTTP and returns the same thing
    again = texts.get_8k_texts(conn, "ACME", date(2024, 5, 10), days=30, client=client)
    assert again == out and len(client.calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM filing_texts WHERE accession='A-1'").fetchone()[0] == 4

    # as_of before the filing date -> nothing (point-in-time)
    assert texts.get_8k_texts(conn, "ACME", date(2024, 5, 2), days=30, client=client) == []


def test_get_8k_texts_failure_not_cached_and_others_survive(conn):
    _filing(conn, "A-1", "2024-05-03", "8.01", primary="http://bad")
    _filing(conn, "A-2", "2024-05-02", "8.01", primary="http://ok")
    client = FakeClient({"http://ok": MULTI_ITEM_DOC})
    out = texts.get_8k_texts(conn, "ACME", date(2024, 5, 10), client=client)
    assert [r["accession"] for r in out] == ["A-2"]
    assert conn.execute("SELECT COUNT(*) FROM filing_texts WHERE accession='A-1'").fetchone()[0] == 0


def test_get_8k_texts_no_urls_is_skipped(conn):
    _filing(conn, "A-1", "2024-05-03", "8.01", primary=None)
    assert texts.get_8k_texts(conn, "ACME", date(2024, 5, 10), client=FakeClient({})) == []


def test_get_8k_texts_ex99_only_when_item_wanted_and_amendments_included(conn):
    _filing(conn, "A-1", "2024-05-03", "2.02", primary=None, ex99="http://e1", form="8-K/A")
    out = texts.get_8k_texts(conn, "ACME", date(2024, 5, 10), client=FakeClient({"http://e1": EX99}))
    assert [(r["accession"], r["section"]) for r in out] == [("A-1", "EX-99.1")]


def test_html_to_text_strips_sgml_document_wrapper():
    raw = ("<DOCUMENT>\n<TYPE>EX-99.1\n<SEQUENCE>2\n<FILENAME>x.htm\n<DESCRIPTION>EX-99.1\n<TEXT>\n"
           "<html><head><title>Document</title></head><body><div>Exhibit 99.1</div><div>Results</div>"
           "</body></html>\n</TEXT>\n</DOCUMENT>")
    assert texts.html_to_text(raw) == "Exhibit 99.1\n\nResults"


def test_html_to_text_separates_table_cells():
    t = texts.html_to_text("<table><tr><td>Revenue</td><td>$109.4</td><td>(5)</td></tr><tr><td>EPS</td><td>2.02</td></tr></table>")
    assert [ln for ln in t.splitlines() if ln] == ["Revenue $109.4 (5)", "EPS 2.02"]
