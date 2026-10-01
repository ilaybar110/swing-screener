"""Tests for src/data/edgar_bulk.py (no network: local zips only)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

import pytest

from src.data import edgar_bulk as eb
from src.db import init_db

EDGAR = Path(__file__).resolve().parent / "fixtures" / "edgar"
INSIDER_ZIP = EDGAR / "insider_sample_2024q1.zip"

TICKERS = [("WDAY", "0001327811"), ("OWPC", "0001622244"), ("ORGS", "0001460602"),
           ("LEE", "0000058361"), ("AAPL", "0000320193")]


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = init_db(tmp_path / "research.db")
    c.executemany("INSERT INTO tickers (ticker, cik) VALUES (?,?)", TICKERS)
    c.commit()
    yield c
    c.close()


def _recent(rows):
    keys = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument"]
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


def _make_submissions_zip(path: Path) -> None:
    aapl_recent = _recent([
        ("0000320193-24-000100", "2024-05-02", "2024-05-02T20:30:20.000Z", "8-K", "2.02,9.01", "a.htm"),   # AMC
        ("0000320193-24-000090", "2024-05-01", "2024-05-01T11:00:00.000Z", "8-K", "8.01", "b.htm"),
        ("0000320193-24-000080", "2024-04-01", "2024-04-01T20:00:00.000Z", "8-K/A", "2.02", "c.htm"),    # amendment
        ("0000320193-24-000070", "2024-03-01", "2024-03-01T21:00:00.000Z", "10-Q", "", "q.htm"),
        ("0000320193-24-000060", "2024-02-02", "2024-02-02T21:30:00.000Z", "4", "", "f4.xml"),
        ("0000320193-24-000050", "2024-02-01", "2024-02-01T21:30:00.000Z", "S-8", "", "s8.htm"),           # ignored form
        ("0000320193-12-000001", "2012-01-01", "2012-01-01T21:30:00.000Z", "8-K", "2.02", "old.htm"),     # before start_year
    ])
    overflow = _recent([  # top-level arrays, as in CIK...-submissions-001.json
        ("0000320193-15-000001", "2015-10-28", "2015-10-28T20:15:00.000Z", "8-K", "2.02,9.01", "o.htm"),  # EDT AMC
        ("0000320193-15-000002", "2015-12-15", "2015-12-15T14:00:00.000Z", "8-K", "2.02", "o2.htm"),      # 09:00 EST BMO
    ])
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("CIK0000320193.json", json.dumps({"cik": "320193", "filings": {"recent": aapl_recent, "files": []}}))
        zf.writestr("CIK0000320193-submissions-001.json", json.dumps(overflow))
        zf.writestr("CIK0000999999.json", json.dumps({"filings": {"recent": aapl_recent}}))  # not in universe


def test_backfill_submissions(conn, tmp_path):
    zp = tmp_path / "submissions.zip"
    _make_submissions_zip(zp)
    stats = eb.backfill_submissions(conn, zp, 2014, {"0000320193": ["AAPL"], "0000111111": ["NOPE"]})
    assert stats["ciks"] == 1 and stats["ciks_missing"] == 1

    rows = {r["accession"]: r for r in conn.execute("SELECT * FROM filings")}
    assert set(rows) == {
        "0000320193-24-000100", "0000320193-24-000090", "0000320193-24-000080", "0000320193-24-000070",
        "0000320193-24-000060", "0000320193-15-000001", "0000320193-15-000002"}  # no S-8, nothing < 2014
    r = rows["0000320193-24-000100"]
    assert r["ticker"] == "AAPL" and r["items"] == "2.02,9.01" and r["processed"] == 1
    assert r["acceptance_datetime"] == "2024-05-02T16:30:20"  # UTC -> Eastern
    assert r["primary_doc_url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/a.htm"
    assert rows["0000320193-24-000060"]["processed"] == 0  # Form 4 waits for the insider data set
    assert rows["0000320193-24-000070"]["items"] is None

    ev = [tuple(r) for r in conn.execute(
        "SELECT ticker, event_date, timing, source FROM earnings_dates ORDER BY event_date")]
    assert ev == [
        ("AAPL", "2015-10-29", "AMC", "8k_2.02"),
        ("AAPL", "2015-12-15", "BMO", "8k_2.02"),   # 09:00 ET -> same day
        ("AAPL", "2024-05-03", "AMC", "8k_2.02"),
    ]  # 8-K/A and non-2.02 8-Ks create no events

    # idempotent
    eb.backfill_submissions(conn, zp, 2014, {"0000320193": ["AAPL"]})
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM earnings_dates").fetchone()[0] == 3


def test_submissions_does_not_clobber_daily_enriched_rows(conn, tmp_path):
    conn.execute("INSERT INTO filings (accession, cik, ticker, form, filed_date, exhibit_991_url, processed) "
                 "VALUES ('0000320193-24-000100','0000320193','AAPL','8-K','2024-05-02','http://ex99',1)")
    zp = tmp_path / "s.zip"
    _make_submissions_zip(zp)
    eb.backfill_submissions(conn, zp, 2014, {"0000320193": ["AAPL"]})
    assert conn.execute("SELECT exhibit_991_url FROM filings WHERE accession='0000320193-24-000100'"
                        ).fetchone()[0] == "http://ex99"


def test_load_insider_zip_real_sample(conn):
    cik_map = eb.load_cik_map(conn)
    stats = eb.load_insider_zip(conn, INSIDER_ZIP, cik_map)
    assert stats["submissions"] == 17 + 1 + 7 + 5 + 3  # Form 4 / 4-A only (Form 3s excluded)

    # a real open-market purchase carried through with value = shares x price
    p = conn.execute(
        "SELECT * FROM insider_trades WHERE accession = '0002011620-24-000006' AND trans_code = 'P'"
    ).fetchall()
    assert p, "expected purchase rows"
    for r in p:
        assert r["value"] == pytest.approx(r["shares"] * r["price"], abs=0.01)
        assert r["acquired_disposed"] == "A" and r["filed_date"].startswith("2024-")
        assert r["ticker"] in {"WDAY", "OWPC", "ORGS", "LEE"} and len(r["cik"]) == 10
    # row_num is a dense 1..n sequence per accession
    for acc, n, mx, mn in conn.execute(
            "SELECT accession, COUNT(*), MAX(row_num), MIN(row_num) FROM insider_trades GROUP BY accession"):
        assert (mn, mx) == (1, n), acc

    # filings index kept consistent and marked processed
    assert conn.execute("SELECT COUNT(*) FROM filings WHERE processed = 0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM filings WHERE form='4/A'").fetchone()[0] == 1


def test_derivative_only_amendment_leaves_original_alone(conn):
    # Real case: Workday 4/A 0001327811-24-000026 (Bozzini) restates no non-derivative rows,
    # so the original 0001327811-24-000014 must be kept, not deleted on a guess.
    eb.load_insider_zip(conn, INSIDER_ZIP, eb.load_cik_map(conn))
    assert conn.execute("SELECT COUNT(*) FROM insider_trades WHERE accession='0001327811-24-000026'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM insider_trades WHERE accession='0001327811-24-000014'"
                        ).fetchone()[0] == 1


def _mini_insider_zip(path: Path) -> None:
    sub_h = "ACCESSION_NUMBER	FILING_DATE	PERIOD_OF_REPORT	DATE_OF_ORIG_SUB	DOCUMENT_TYPE	ISSUERCIK	ISSUERNAME	ISSUERTRADINGSYMBOL"
    own_h = "ACCESSION_NUMBER	RPTOWNERCIK	RPTOWNERNAME	RPTOWNER_RELATIONSHIP	RPTOWNER_TITLE"
    tr_h = ("ACCESSION_NUMBER	NONDERIV_TRANS_SK	SECURITY_TITLE	TRANS_DATE	TRANS_CODE	TRANS_SHARES	"
            "TRANS_PRICEPERSHARE	TRANS_ACQUIRED_DISP_CD")
    subs = [
        # the amendment is listed first on purpose: loader must still apply originals first
        "A-AMEND	20-MAR-2024	01-MAR-2024	05-MAR-2024	4/A	0000320193	Apple	AAPL",
        "A-ORIG	05-MAR-2024	01-MAR-2024		4	0000320193	Apple	AAPL",
        "A-OTHER	05-MAR-2024	01-MAR-2024		4	0000320193	Apple	AAPL",
        "A-FOREIGN	05-MAR-2024	01-MAR-2024		4	0000555555	NotUniverse	NOPE",
    ]
    owners = [
        "A-AMEND	0000000111	Jane CFO	Officer,Director	Chief Financial Officer",
        "A-ORIG	0000000111	Jane CFO	Officer,Director	Chief Financial Officer",
        "A-OTHER	0000000222	Bob Director	Director	",
        "A-FOREIGN	0000000333	Zed	Officer	CEO",
    ]
    trans = [
        "A-ORIG	1	Common	01-MAR-2024	P	1000	10.00	A",
        "A-AMEND	2	Common	01-MAR-2024	P	1500	10.00	A",
        "A-AMEND	3	Common	02-MAR-2024	P	500		A",
        "A-OTHER	4	Common	01-MAR-2024	P	2000	10.00	A",
        "A-FOREIGN	5	Common	01-MAR-2024	P	9	1.00	A",
    ]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("SUBMISSION.tsv", sub_h + "\n" + "\n".join(subs) + "\n")
        zf.writestr("REPORTINGOWNER.tsv", own_h + "\n" + "\n".join(owners) + "\n")
        zf.writestr("NONDERIV_TRANS.tsv", tr_h + "\n" + "\n".join(trans) + "\n")


def test_restating_amendment_supersedes_original_but_not_other_insiders(conn, tmp_path):
    zp = tmp_path / "mini.zip"
    _mini_insider_zip(zp)
    stats = eb.load_insider_zip(conn, zp, eb.load_cik_map(conn))
    assert stats["submissions"] == 3  # foreign issuer skipped
    accs = {r[0]: r[1] for r in conn.execute(
        "SELECT accession, COUNT(*) FROM insider_trades GROUP BY accession")}
    assert accs == {"A-AMEND": 2, "A-OTHER": 1}  # A-ORIG replaced by the 4/A; other insider untouched
    r = conn.execute("SELECT * FROM insider_trades WHERE accession='A-AMEND' ORDER BY row_num").fetchall()
    assert [x["shares"] for x in r] == [1500.0, 500.0]
    assert r[1]["price"] is None and r[1]["value"] is None
    assert r[0]["ticker"] == "AAPL" and r[0]["officer_title"] == "Chief Financial Officer"
    assert r[0]["is_officer"] == 1 and r[0]["is_director"] == 1 and r[0]["filed_date"] == "2024-03-20"


def test_parse_helpers():
    assert eb._parse_dmy("31-JAN-2024") == "2024-01-31"
    assert eb._parse_dmy("") is None and eb._parse_dmy("garbage") is None
    assert eb._num("1,234.5") == 1234.5 and eb._num("") is None and eb._num("x") is None
    assert list(eb._quarters(2023, date(2024, 2, 10))) == [(2023, 1), (2023, 2), (2023, 3), (2023, 4), (2024, 1)]


class NoDownloadClient:
    def __init__(self):
        self.calls = 0

    def _throttle(self):
        self.calls += 1
        raise AssertionError("no download expected")


def test_backfill_end_to_end_and_resume(conn, tmp_path):
    bulk = tmp_path / "bulk"
    (bulk / "insider").mkdir(parents=True)
    _make_submissions_zip(bulk / "submissions.zip")
    shutil.copy(INSIDER_ZIP, bulk / "insider" / "2024q1_form345.zip")
    # pretend earlier quarters were already loaded so only 2024q1 is attempted
    for q in [(y, n) for y in (2023, 2024) for n in range(1, 5)]:
        if q != (2024, 1):
            eb._mark_job(conn, f"edgar_bulk:insider:{q[0]}q{q[1]}", "t", "pre-done")

    client = NoDownloadClient()
    rep = eb.backfill(conn, start_year=2023, client=client, bulk_dir=bulk, through=date(2024, 3, 31))
    assert client.calls == 0
    assert rep["submissions"]["filings"] > 0 and "2024q1" in rep["insider_quarters"]
    n_trades = conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0]
    assert n_trades > 0

    rep2 = eb.backfill(conn, start_year=2023, client=client, bulk_dir=bulk, through=date(2024, 3, 31))
    assert rep2["submissions"] is None and rep2["insider_quarters"] == {}
    assert "edgar_bulk:submissions:2023" in rep2["skipped"] and "edgar_bulk:insider:2024q1" in rep2["skipped"]
    assert conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0] == n_trades


class RangeResp:
    def __init__(self, status, chunks):
        self.status_code, self._chunks = status, chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield from self._chunks


class FlakySession:
    """First call dies mid-stream after writing a partial file; second must resume with Range."""

    def __init__(self):
        self.headers_seen = []

    def get(self, url, headers=None, stream=True, timeout=None):
        self.headers_seen.append(dict(headers or {}))
        if len(self.headers_seen) == 1:
            def boom():
                yield b"AAAA"
                raise ConnectionError("dropped")
            return RangeResp(200, boom())
        return RangeResp(206, [b"BBBB"])


class FakeClient:
    def __init__(self, session):
        self._session = session

    def _throttle(self):
        pass


def test_download_file_resumes_with_range(tmp_path, monkeypatch):
    monkeypatch.setattr(eb._time, "sleep", lambda s: None)
    sess = FlakySession()
    dest = tmp_path / "x" / "f.zip"
    assert eb.download_file(FakeClient(sess), "http://u", dest) is True
    assert dest.read_bytes() == b"AAAABBBB"
    assert sess.headers_seen == [{}, {"Range": "bytes=4-"}]
    assert not dest.with_name("f.zip.part").exists()
    assert eb.download_file(FakeClient(sess), "http://u", dest) is True  # already complete: no new request
    assert len(sess.headers_seen) == 2


def test_download_file_missing_returns_false(tmp_path):
    class S:
        def get(self, *a, **k):
            return RangeResp(404, [])
    assert eb.download_file(FakeClient(S()), "http://u", tmp_path / "n.zip") is False


def test_validate_purchases_output(conn):
    eb.load_insider_zip(conn, INSIDER_ZIP, eb.load_cik_map(conn))
    out = eb.validate_purchases(conn, ["OWPC", "ORGS", "ZZZZ"], limit=3)
    assert "=== OWPC  (CIK 0001622244)" in out and "own-disp?action=getissuer&CIK=0001622244" in out
    assert "-index.htm" in out
    assert "=== ZZZZ  (not in tickers table)" in out and "(no open-market purchases in DB)" in out
