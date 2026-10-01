"""Tests for src/data/edgar_daily.py (no network: a fake EdgarClient serves saved files)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from src.data import edgar_daily as ed
from src.db import init_db

EDGAR = Path(__file__).resolve().parent / "fixtures" / "edgar"
IDX_TEXT = (EDGAR / "form.sample.idx").read_text(encoding="utf-8")
AAPL_INDEX = (EDGAR / "aapl_8k_index.htm").read_text(encoding="utf-8")
FORM4_TXT = (EDGAR / "form4_awards.txt").read_text(encoding="utf-8")

F4_ACC = "0001104659-26-111575"  # 111, Inc. (YI), CIK 1738906
EIGHTK = {  # cik -> ticker
    "1084869": "FLWS",
    "1750": "AIR",
    "1144215": "AYI",
}


class FakeClient:
    """Stands in for EdgarClient; counts requests."""

    def __init__(self, index_date=date(2026, 9, 29), index_text=IDX_TEXT, index_html=None,
                 fail_on=None):
        self.index_date, self.index_text = index_date, index_text
        self.index_html = index_html or {}
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    def latest_published_daily_index(self, day, lookback_days=10):
        return self.index_date, self.index_text

    def get_text(self, url, use_cache=True):
        self.calls.append(url)
        for acc in self.fail_on:
            if acc in url:
                raise RuntimeError("boom")
        if url.endswith("-index.htm"):
            acc = url.rsplit("/", 1)[-1].removesuffix("-index.htm")
            return self.index_html.get(acc, AAPL_INDEX)
        if F4_ACC in url:
            return FORM4_TXT
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = init_db(tmp_path / "t.db")
    rows = [("YI", "0001738906")] + [(t, k.zfill(10)) for k, t in EIGHTK.items()]
    c.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)", rows)
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------


def test_parse_form_index_filters_and_pads_ciks():
    entries = ed.parse_form_index(IDX_TEXT, ed.WANTED_FORMS)
    assert {e.form for e in entries} == {"4", "8-K"}  # 10-K lines dropped
    yi = [e for e in entries if e.cik == "0001738906"]
    assert len(yi) == 1 and yi[0].company == "111, Inc."
    assert yi[0].accession == F4_ACC and yi[0].filed_date == "2026-09-29"
    air = [e for e in entries if e.cik == "0000001750"]
    assert air and air[0].form == "8-K"


def test_parse_form_index_form_types_with_spaces_and_amendments():
    text = (
        "4/A              Foo Corp                                                      5555        20240102    edgar/data/5555/0000005555-24-000001.txt\n"
        "SC 13G/A         Bar  Holdings  LLC                                            6666        20240102    edgar/data/6666/0000006666-24-000002.txt\n"
    )
    e = ed.parse_form_index(text)
    assert [(x.form, x.company, x.cik) for x in e] == [
        ("4/A", "Foo Corp", "0000005555"), ("SC 13G/A", "Bar  Holdings  LLC", "0000006666")]


# ---------------------------------------------------------------------------
# Filing index page + time zones
# ---------------------------------------------------------------------------


def test_parse_filing_index_aapl():
    idx = ed.parse_filing_index(AAPL_INDEX)
    assert idx.accepted_et == "2026-07-30T16:30:28"
    assert idx.items == ["2.02", "9.01"]
    assert idx.primary_doc_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000018/aapl-20260730.htm")
    assert idx.exhibit_991_url.endswith("/a8-kex991q3202606272026.htm")
    assert idx.exhibit_991_url.startswith("https://www.sec.gov/Archives/")


@pytest.mark.parametrize("utc,et", [
    ("2026-07-30T20:30:28.000Z", "2026-07-30T16:30:28"),   # EDT (verified against EDGAR's page)
    ("2026-01-29T21:30:33.000Z", "2026-01-29T16:30:33"),   # EST
    ("2024-03-10T06:59:59.000Z", "2024-03-10T01:59:59"),   # just before spring-forward
    ("2024-03-10T07:00:00.000Z", "2024-03-10T03:00:00"),
])
def test_utc_to_et(utc, et):
    assert ed.utc_to_et_naive(utc) == et


# ---------------------------------------------------------------------------
# Earnings event timing (Module B depends on this)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("accepted,event,timing", [
    # Tuesday 2024-12-03, regular session
    ("2024-12-03T00:00:00", date(2024, 12, 3), "BMO"),
    ("2024-12-03T07:00:00", date(2024, 12, 3), "BMO"),
    ("2024-12-03T09:29:59", date(2024, 12, 3), "BMO"),
    ("2024-12-03T09:30:00", date(2024, 12, 4), None),   # at the open: open is not *after* acceptance
    ("2024-12-03T12:00:00", date(2024, 12, 4), None),
    ("2024-12-03T15:59:59", date(2024, 12, 4), None),
    ("2024-12-03T16:00:00", date(2024, 12, 4), "AMC"),
    ("2024-12-03T16:30:28", date(2024, 12, 4), "AMC"),
    ("2024-12-03T23:59:59", date(2024, 12, 4), "AMC"),
    # Friday after close -> Monday
    ("2024-12-06T17:00:00", date(2024, 12, 9), "AMC"),
    ("2024-12-06T10:00:00", date(2024, 12, 9), None),
    # weekend: published before Monday's open
    ("2024-12-07T10:00:00", date(2024, 12, 9), "BMO"),
    ("2024-12-08T22:00:00", date(2024, 12, 9), "BMO"),
    # market holiday (Christmas, Wed) -> Thursday
    ("2024-12-25T08:00:00", date(2024, 12, 26), "BMO"),
    # Wed before Thanksgiving, after close -> skips Thursday holiday to Friday
    ("2024-11-27T17:00:00", date(2024, 11, 29), "AMC"),
    # Black Friday early close (13:00): 13:30 is already after the close
    ("2024-11-29T12:59:00", date(2024, 12, 2), None),
    ("2024-11-29T13:30:00", date(2024, 12, 2), "AMC"),
    # year end
    ("2024-12-31T16:05:00", date(2025, 1, 2), "AMC"),
])
def test_earnings_event_from_acceptance(accepted, event, timing):
    assert ed.earnings_event_from_acceptance(datetime.fromisoformat(accepted)) == (event, timing)


def test_record_earnings_event_keeps_earliest_and_tags_all_tickers(conn):
    ed.record_earnings_event(conn, ["GOOG", "GOOGL"], "2024-12-03T16:30:00")
    rows = conn.execute("SELECT * FROM earnings_dates ORDER BY ticker").fetchall()
    assert [(r["ticker"], r["event_date"], r["timing"], r["source"]) for r in rows] == [
        ("GOOG", "2024-12-04", "AMC", "8k_2.02"), ("GOOGL", "2024-12-04", "AMC", "8k_2.02")]
    ed.record_earnings_event(conn, ["GOOG"], "2024-12-03T18:00:00")  # later, same event -> ignored
    ed.record_earnings_event(conn, ["GOOG"], "2024-12-03T16:10:00")  # earlier -> wins
    r = conn.execute("SELECT acceptance_datetime FROM earnings_dates WHERE ticker='GOOG'").fetchone()
    assert r[0] == "2024-12-03T16:10:00"
    assert conn.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# process_day
# ---------------------------------------------------------------------------


def _no_earnings_html(accepted="2026-09-29 08:00:00"):
    h = AAPL_INDEX.replace("Item 2.02: Results of Operations and Financial Condition",
                           "Item 7.01: Regulation FD Disclosure")
    return h.replace("2026-07-30 16:30:28", accepted)


def test_process_day_end_to_end(conn):
    client = FakeClient(index_html={
        "0001084869-26-000033": AAPL_INDEX.replace("2026-07-30 16:30:28", "2026-09-29 07:15:00"),
        "0001104659-26-111482": _no_earnings_html(),
        "0001104659-26-111917": AAPL_INDEX.replace("2026-07-30 16:30:28", "2026-09-29 16:45:00"),
    })
    rep = ed.process_day(conn, date(2026, 9, 29), client=client)
    assert rep["errors"] == []
    assert rep["index_is_requested_day"] is True
    assert (rep["form4_seen"], rep["form4_processed"], rep["insider_rows"]) == (1, 1, 9)
    assert (rep["eightk_seen"], rep["eightk_processed"], rep["earnings_events"]) == (3, 3, 2)

    f = conn.execute("SELECT * FROM filings WHERE accession='0001084869-26-000033'").fetchone()
    assert f["ticker"] == "FLWS" and f["form"] == "8-K" and f["cik"] == "0001084869"
    assert f["items"] == "2.02,9.01" and f["acceptance_datetime"] == "2026-09-29T07:15:00"
    assert f["exhibit_991_url"].endswith("a8-kex991q3202606272026.htm") and f["processed"] == 1
    f4 = conn.execute("SELECT * FROM filings WHERE accession=?", (F4_ACC,)).fetchone()
    assert f4["ticker"] == "YI" and f4["acceptance_datetime"] == "2026-09-29T06:57:39"

    ev = {r["ticker"]: (r["event_date"], r["timing"], r["source"])
          for r in conn.execute("SELECT * FROM earnings_dates")}
    assert ev == {  # AIR's 8-K has no Item 2.02 -> no event
        "FLWS": ("2026-09-29", "BMO", "8k_2.02"),
        "AYI": ("2026-09-30", "AMC", "8k_2.02"),
    }
    trades = conn.execute("SELECT COUNT(*), MIN(ticker), MIN(cik) FROM insider_trades").fetchone()
    assert tuple(trades) == (9, "YI", "0001738906")


def test_process_day_is_idempotent(conn):
    client = FakeClient()
    ed.process_day(conn, date(2026, 9, 29), client=client)
    n_calls = len(client.calls)
    assert n_calls == 4  # 3 x 8-K index page + 1 Form 4 submission
    snapshot = [tuple(r) for r in conn.execute("SELECT * FROM insider_trades ORDER BY 1,2")]

    rep = ed.process_day(conn, date(2026, 9, 29), client=client)
    assert len(client.calls) == n_calls  # nothing refetched
    assert rep["skipped_already_processed"] == 4
    assert rep["form4_processed"] == rep["eightk_processed"] == 0
    assert [tuple(r) for r in conn.execute("SELECT * FROM insider_trades ORDER BY 1,2")] == snapshot
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 4


def test_process_day_error_is_reported_and_retried(conn):
    client = FakeClient(fail_on={"0001104659-26-111482"})
    rep = ed.process_day(conn, date(2026, 9, 29), client=client)
    assert len(rep["errors"]) == 1 and "0001104659-26-111482" in rep["errors"][0]
    assert rep["eightk_processed"] == 2 and rep["form4_processed"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM filings WHERE accession='0001104659-26-111482'").fetchone()[0] == 0

    client.fail_on = set()
    rep2 = ed.process_day(conn, date(2026, 9, 29), client=client)
    assert rep2["errors"] == [] and rep2["eightk_processed"] == 1
    assert rep2["skipped_already_processed"] == 3


def test_process_day_walks_back_when_day_unpublished(conn):
    client = FakeClient(index_date=date(2026, 9, 29))
    rep = ed.process_day(conn, date(2026, 10, 1), client=client)
    assert rep["requested_date"] == "2026-10-01" and rep["index_date"] == "2026-09-29"
    assert rep["index_is_requested_day"] is False
    assert rep["eightk_processed"] == 3


def test_8k_amendment_does_not_create_earnings_event(conn):
    text = ("8-K/A            AAR CORP                                                      "
            "1750        20260929    edgar/data/1750/0001104659-26-111999.txt\n")
    rep = ed.process_day(conn, date(2026, 9, 29), client=FakeClient(index_text=text))
    assert rep["eightk_processed"] == 1 and rep["earnings_events"] == 0
    assert conn.execute("SELECT form FROM filings").fetchone()[0] == "8-K/A"
    assert conn.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0] == 0


def test_form4_matched_only_via_owner_cik_is_ignored(conn):
    # an owner entity whose CIK is a universe company, filing as 10% owner of someone else
    conn.execute("DELETE FROM tickers WHERE ticker = 'YI'")  # the filing's issuer is not in scope
    conn.execute("INSERT INTO tickers (ticker, cik) VALUES ('OWN', '0000777777')")
    text = ("4                Owner Co                                                      "
            "777777      20260929    edgar/data/777777/0001104659-26-111575.txt\n")
    rep = ed.process_day(conn, date(2026, 9, 29), client=FakeClient(index_text=text))
    assert rep["form4_not_issuer"] == 1 and rep["form4_processed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0] == 0


def test_pick_ticker_prefers_issuer_symbol():
    assert ed.pick_ticker(["BRK-A", "BRK-B"], "BRK.B") == "BRK-B"
    assert ed.pick_ticker(["GOOG", "GOOGL"], "GOOGL") == "GOOGL"
    assert ed.pick_ticker(["GOOG", "GOOGL"], "") == "GOOG"


# ---------------------------------------------------------------------------
# Live network smoke test (skipped unless RUN_NETWORK_TESTS and SEC_EMAIL are set)
# ---------------------------------------------------------------------------

import os  # noqa: E402


@pytest.mark.skipif(
    not (os.environ.get("RUN_NETWORK_TESTS") and os.environ.get("SEC_EMAIL")),
    reason="set RUN_NETWORK_TESTS=1 and SEC_EMAIL to hit EDGAR",
)
def test_live_process_day_five_tickers(tmp_path):
    from src.data.edgar_client import EdgarClient

    c = init_db(tmp_path / "live.db")
    c.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)", [
        ("YI", "0001738906"), ("COUR", "0001651562"), ("FLWS", "0001084869"),
        ("AIR", "0000001750"), ("AYI", "0001144215")])
    c.commit()
    client = EdgarClient(tmp_path / "raw")
    rep = ed.process_day(c, date(2026, 9, 29), client=client)
    assert rep["errors"] == [] and rep["index_is_requested_day"]
    assert rep["form4_processed"] >= 1 and rep["eightk_processed"] >= 3
    assert c.execute("SELECT COUNT(*) FROM insider_trades WHERE ticker='COUR' AND trans_code='P'").fetchone()[0] >= 1
    assert all(r[0] for r in c.execute("SELECT acceptance_datetime FROM filings WHERE form='8-K'"))
    rep2 = ed.process_day(c, date(2026, 9, 29), client=client)
    assert rep2["skipped_already_processed"] == rep["form4_processed"] + rep["eightk_processed"]
    c.close()
