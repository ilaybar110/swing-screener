"""Tests for src/data/fundamentals.py (parser + derived aggregates).

Uses small, hand-built companyfacts-shaped fixtures (same JSON shape SEC's
companyfacts API returns) rather than a real multi-MB download, per
docs/BOT_RULES.md rule 4 (network tests must be opt-in and <=5 tickers).
"""

from __future__ import annotations

from datetime import date

import pytest

from src.data import fundamentals


def _entry(start, end, val, accn, fy, fp, form, filed):
    e = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        e["start"] = start
    return e


def make_companyfacts() -> dict:
    """A small synthetic company with two fiscal years of revenue/opinc (income
    statement items tagged both as discrete quarters and, for FY2023, only via a
    YTD chain needing derivation), operating cash flow / capex tagged only as YTD
    cumulative (the common real-world pattern), plus instant shares/cash/debt."""
    return {
        "cik": 123456,
        "entityName": "TESTCO INC",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            # FY2023: each quarter tagged discretely (~90 days).
                            _entry("2023-01-01", "2023-03-31", 100.0, "acc-q1-23", 2023, "Q1", "10-Q", "2023-04-20"),
                            _entry("2023-04-01", "2023-06-30", 110.0, "acc-q2-23", 2023, "Q2", "10-Q", "2023-07-20"),
                            _entry("2023-07-01", "2023-09-30", 120.0, "acc-q3-23", 2023, "Q3", "10-Q", "2023-10-20"),
                            _entry("2023-01-01", "2023-12-31", 450.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                            # FY2024: only YTD-cumulative facts filed (no discrete
                            # quarter tags) -- must be derived by subtraction.
                            _entry("2024-01-01", "2024-03-31", 130.0, "acc-q1-24", 2024, "Q1", "10-Q", "2024-04-20"),
                            _entry("2024-01-01", "2024-06-30", 270.0, "acc-q2-24", 2024, "Q2", "10-Q", "2024-07-20"),
                            _entry("2024-01-01", "2024-09-30", 420.0, "acc-q3-24", 2024, "Q3", "10-Q", "2024-10-20"),
                            _entry("2024-01-01", "2024-12-31", 590.0, "acc-fy-24", 2024, "FY", "10-K", "2025-02-15"),
                        ]
                    }
                },
                "OperatingIncomeLoss": {
                    "units": {
                        "USD": [
                            _entry("2023-01-01", "2023-03-31", 10.0, "acc-q1-23", 2023, "Q1", "10-Q", "2023-04-20"),
                            _entry("2023-01-01", "2023-12-31", 45.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            # Only YTD-cumulative facts, as is typical for cash flow
                            # statement line items.
                            _entry("2023-01-01", "2023-03-31", 20.0, "acc-q1-23", 2023, "Q1", "10-Q", "2023-04-20"),
                            _entry("2023-01-01", "2023-06-30", 45.0, "acc-q2-23", 2023, "Q2", "10-Q", "2023-07-20"),
                            _entry("2023-01-01", "2023-09-30", 66.0, "acc-q3-23", 2023, "Q3", "10-Q", "2023-10-20"),
                            _entry("2023-01-01", "2023-12-31", 95.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            _entry("2023-01-01", "2023-03-31", 5.0, "acc-q1-23", 2023, "Q1", "10-Q", "2023-04-20"),
                            _entry("2023-01-01", "2023-06-30", 11.0, "acc-q2-23", 2023, "Q2", "10-Q", "2023-07-20"),
                            _entry("2023-01-01", "2023-09-30", 18.0, "acc-q3-23", 2023, "Q3", "10-Q", "2023-10-20"),
                            _entry("2023-01-01", "2023-12-31", 26.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
                "CommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            _entry(None, "2023-12-31", 1_000_000, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                            _entry(None, "2024-12-31", 1_150_000, "acc-fy-24", 2024, "FY", "10-K", "2025-02-15"),
                        ]
                    }
                },
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            _entry(None, "2023-12-31", 200.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
                "LongTermDebt": {
                    "units": {
                        "USD": [
                            _entry(None, "2023-12-31", 300.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
                "ShortTermBorrowings": {
                    "units": {
                        "USD": [
                            _entry(None, "2023-12-31", 40.0, "acc-fy-23", 2023, "FY", "10-K", "2024-02-15"),
                        ]
                    }
                },
            },
            "dei": {},
        },
    }


# ---------------------------------------------------------------------------
# resolve_tag
# ---------------------------------------------------------------------------


def test_resolve_tag_picks_first_tag_with_data():
    from src.data.xbrl_tags import REVENUE_TAGS

    facts = make_companyfacts()
    spec, raw = fundamentals.resolve_tag(facts, REVENUE_TAGS)
    assert spec.tag == "Revenues"
    assert len(raw) == 8


def test_resolve_tag_falls_back_when_first_tag_missing():
    from src.data.xbrl_tags import TagSpec

    facts = make_companyfacts()
    specs = [TagSpec("us-gaap", "NoSuchTag"), TagSpec("us-gaap", "Revenues")]
    spec, raw = fundamentals.resolve_tag(facts, specs)
    assert spec.tag == "Revenues"


def test_resolve_tag_returns_none_when_no_tag_has_data():
    from src.data.xbrl_tags import TagSpec

    facts = make_companyfacts()
    result = fundamentals.resolve_tag(facts, [TagSpec("us-gaap", "NoSuchTag")])
    assert result is None


# ---------------------------------------------------------------------------
# quarterization
# ---------------------------------------------------------------------------


def test_quarterize_keeps_direct_discrete_quarters():
    facts = make_companyfacts()
    _, raw = fundamentals.resolve_tag(facts, fundamentals.xbrl_tags.REVENUE_TAGS)
    rows = fundamentals.quarterize(raw)
    q1_2023 = next(r for r in rows if r["period_end"] == date(2023, 3, 31))
    assert q1_2023["value"] == pytest.approx(100.0)


def test_quarterize_derives_q4_from_fy_minus_9mo_ytd():
    """FY2023 revenue is tagged as three discrete quarters plus an annual total
    (no explicit YTD cumulative fact) -- Q4 = FY - sum(Q1, Q2, Q3)."""
    facts = make_companyfacts()
    _, raw = fundamentals.resolve_tag(facts, fundamentals.xbrl_tags.REVENUE_TAGS)
    rows = fundamentals.quarterize(raw)
    q4_2023 = next(r for r in rows if r["period_end"] == date(2023, 12, 31))
    assert q4_2023["value"] == pytest.approx(450.0 - (100.0 + 110.0 + 120.0))


def test_quarterize_derives_all_quarters_from_ytd_only_chain():
    """FY2024 revenue has no discrete-quarter facts at all -- Q2/Q3/Q4 must be
    derived from successive YTD differences, Q1 is already 3-month direct."""
    facts = make_companyfacts()
    _, raw = fundamentals.resolve_tag(facts, fundamentals.xbrl_tags.REVENUE_TAGS)
    rows = fundamentals.quarterize(raw)
    by_end = {r["period_end"]: r["value"] for r in rows}
    assert by_end[date(2024, 3, 31)] == pytest.approx(130.0)
    assert by_end[date(2024, 6, 30)] == pytest.approx(270.0 - 130.0)
    assert by_end[date(2024, 9, 30)] == pytest.approx(420.0 - 270.0)
    assert by_end[date(2024, 12, 31)] == pytest.approx(590.0 - 420.0)


def test_quarterize_cash_flow_ytd_only_chain():
    facts = make_companyfacts()
    _, raw = fundamentals.resolve_tag(facts, fundamentals.xbrl_tags.OPERATING_CASH_FLOW_TAGS)
    rows = fundamentals.quarterize(raw)
    by_end = {r["period_end"]: r["value"] for r in rows}
    assert by_end[date(2023, 3, 31)] == pytest.approx(20.0)
    assert by_end[date(2023, 6, 30)] == pytest.approx(25.0)
    assert by_end[date(2023, 9, 30)] == pytest.approx(21.0)
    assert by_end[date(2023, 12, 31)] == pytest.approx(29.0)


# ---------------------------------------------------------------------------
# parse_companyfacts (full row set)
# ---------------------------------------------------------------------------


def test_parse_companyfacts_produces_rows_for_every_resolvable_metric():
    facts = make_companyfacts()
    rows = fundamentals.parse_companyfacts(facts, cik="0000123456", ticker="TEST")
    metrics = {r["metric"] for r in rows}
    assert metrics == {
        "revenue", "operating_income", "operating_cash_flow", "capex",
        "shares_outstanding", "cash", "total_debt",
    }
    for r in rows:
        assert r["cik"] == "0000123456"
        assert r["ticker"] == "TEST"


def test_parse_companyfacts_total_debt_combines_long_term_and_short_term():
    facts = make_companyfacts()
    rows = fundamentals.parse_companyfacts(facts, cik="0000123456", ticker="TEST")
    debt_rows = [r for r in rows if r["metric"] == "total_debt"]
    assert len(debt_rows) == 1
    assert debt_rows[0]["value"] == pytest.approx(300.0 + 40.0)
    assert debt_rows[0]["period_end"] == date(2023, 12, 31)


def test_parse_companyfacts_marks_proxy_tags():
    facts = make_companyfacts()
    # Remove the exact operating income tag so only the proxy tag would apply.
    del facts["facts"]["us-gaap"]["OperatingIncomeLoss"]
    facts["facts"]["us-gaap"][
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    ] = {
        "units": {
            "USD": [
                _entry("2023-01-01", "2023-03-31", 9.0, "acc-q1-23", 2023, "Q1", "10-Q", "2023-04-20"),
            ]
        }
    }
    rows = fundamentals.parse_companyfacts(facts, cik="0000123456", ticker="TEST")
    op_rows = [r for r in rows if r["metric"] == "operating_income"]
    assert op_rows and all(r["is_proxy"] for r in op_rows)


def test_parse_companyfacts_missing_metric_yields_no_rows_not_an_error():
    facts = make_companyfacts()
    del facts["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]
    rows = fundamentals.parse_companyfacts(facts, cik="0000123456", ticker="TEST")
    assert not any(r["metric"] == "cash" for r in rows)


def test_parse_companyfacts_ifrs_only_filer_yields_no_rows():
    """A foreign-private-issuer filer with only ifrs-full tags (no us-gaap/dei)
    should resolve to zero rows, not an error."""
    facts = {"cik": 1, "facts": {"ifrs-full": {"Revenue": {"units": {"USD": []}}}}}
    rows = fundamentals.parse_companyfacts(facts, cik="0000000001", ticker="FOREIGN")
    assert rows == []


# ---------------------------------------------------------------------------
# TTM / FCF aggregates
# ---------------------------------------------------------------------------


def test_ttm_sum_of_last_four_quarters():
    quarters = [
        {"period_end": date(2024, 3, 31), "value": 100.0},
        {"period_end": date(2023, 12, 31), "value": 90.0},
        {"period_end": date(2023, 9, 30), "value": 80.0},
        {"period_end": date(2023, 6, 30), "value": 70.0},
        {"period_end": date(2023, 3, 31), "value": 60.0},
    ]
    ttm = fundamentals.ttm(quarters, as_of=date(2024, 5, 1))
    assert ttm == pytest.approx(100 + 90 + 80 + 70)


def test_ttm_returns_none_with_fewer_than_four_quarters():
    quarters = [
        {"period_end": date(2024, 3, 31), "value": 100.0},
        {"period_end": date(2023, 12, 31), "value": 90.0},
    ]
    assert fundamentals.ttm(quarters, as_of=date(2024, 5, 1)) is None


def test_ttm_ignores_quarters_after_as_of_point_in_time():
    quarters = [
        {"period_end": date(2024, 6, 30), "value": 999.0},  # not yet known as_of
        {"period_end": date(2024, 3, 31), "value": 100.0},
        {"period_end": date(2023, 12, 31), "value": 90.0},
        {"period_end": date(2023, 9, 30), "value": 80.0},
        {"period_end": date(2023, 6, 30), "value": 70.0},
    ]
    ttm = fundamentals.ttm(quarters, as_of=date(2024, 5, 1))
    assert ttm == pytest.approx(100 + 90 + 80 + 70)


def test_fcf_is_operating_cash_flow_minus_capex():
    assert fundamentals.fcf(120.0, 30.0) == pytest.approx(90.0)


def test_fcf_none_if_either_input_missing():
    assert fundamentals.fcf(None, 30.0) is None
    assert fundamentals.fcf(120.0, None) is None


# ---------------------------------------------------------------------------
# coverage report
# ---------------------------------------------------------------------------


def test_coverage_report_reports_percent_resolved_per_metric(writable_fixture_conn):
    conn = writable_fixture_conn
    now = date.today().isoformat()
    conn.execute(
        "INSERT INTO fundamentals (cik, ticker, metric, period_start, period_end, "
        "period_type, value, form, filed_date, accession, tag_used, is_proxy) "
        "VALUES ('0000000001','MOMA1','revenue','2024-01-01','2024-03-31','duration',"
        "100.0,'10-Q',?,'acc-1','Revenues',0)",
        (now,),
    )
    conn.commit()
    report = fundamentals.coverage_report(conn, tickers=["MOMA1", "MOMB1"])
    assert report["revenue"]["resolved"] == 1
    assert report["revenue"]["total"] == 2
    assert report["operating_income"]["resolved"] == 0


def test_quarterize_ignores_filing_fy_label_on_prior_year_comparatives():
    """Regression (found on real AAPL/CSCO data): every 10-Q/10-K carries its own fiscal
    year in ``fy`` even on prior-year comparative facts, so grouping by ``fy`` mixed two
    years into one running total and produced nonsense (e.g. a -$64B quarter)."""
    e = _entry
    raw = [
        # FY2025 as first reported (Sep fiscal year-end): YTD facts, all start 2024-09-29
        e("2024-09-29", "2024-12-28", 40.0, "a1", 2025, "Q1", "10-Q", "2025-01-31"),
        e("2024-09-29", "2025-03-29", 70.0, "a2", 2025, "Q2", "10-Q", "2025-05-02"),
        e("2024-09-29", "2025-06-28", 100.0, "a3", 2025, "Q3", "10-Q", "2025-08-01"),
        e("2024-09-29", "2025-09-27", 135.0, "a4", 2025, "FY", "10-K", "2025-10-31"),
        # FY2026 filings repeat the prior-year comparatives, labelled fy=2026
        e("2025-09-28", "2025-12-27", 50.0, "b1", 2026, "Q1", "10-Q", "2026-01-30"),
        e("2024-09-29", "2024-12-28", 40.0, "b1", 2026, "Q1", "10-Q", "2026-01-30"),
        e("2025-09-28", "2026-03-28", 90.0, "b2", 2026, "Q2", "10-Q", "2026-05-01"),
        e("2024-09-29", "2025-03-29", 71.0, "b2", 2026, "Q2", "10-Q", "2026-05-01"),  # restated comparative
        e("2025-09-28", "2026-06-27", 135.0, "b3", 2026, "Q3", "10-Q", "2026-07-31"),
    ]
    rows = fundamentals.quarterize(raw)
    by_end = {r["period_end"]: r for r in rows}
    assert by_end[date(2024, 12, 28)]["value"] == pytest.approx(40.0)
    assert by_end[date(2025, 3, 29)]["value"] == pytest.approx(30.0)  # first-filed value (point-in-time)
    assert by_end[date(2025, 3, 29)]["filed_date"] == "2025-05-02"
    assert by_end[date(2025, 6, 28)]["value"] == pytest.approx(30.0)
    assert by_end[date(2025, 9, 27)]["value"] == pytest.approx(35.0)
    assert by_end[date(2025, 12, 27)]["value"] == pytest.approx(50.0)
    assert by_end[date(2026, 3, 28)]["value"] == pytest.approx(40.0)
    assert by_end[date(2026, 6, 27)]["value"] == pytest.approx(45.0)
    assert sum(r["value"] for r in rows if r["period_end"] > date(2025, 6, 30)) == pytest.approx(35 + 50 + 40 + 45)


def test_quarterize_prefers_direct_quarter_over_ytd_difference():
    e = _entry
    raw = [
        e("2024-01-01", "2024-03-31", 10.0, "a", 2024, "Q1", "10-Q", "2024-04-30"),
        e("2024-01-01", "2024-06-30", 30.0, "b", 2024, "Q2", "10-Q", "2024-07-30"),
        e("2024-04-01", "2024-06-30", 20.5, "b", 2024, "Q2", "10-Q", "2024-07-30"),  # direct, slightly different rounding
    ]
    by_end = {r["period_end"]: r["value"] for r in fundamentals.quarterize(raw)}
    assert by_end[date(2024, 6, 30)] == pytest.approx(20.5)


def test_quarterize_skips_ytd_it_cannot_derive():
    raw = [_entry("2024-01-01", "2024-06-30", 30.0, "b", 2024, "Q2", "10-Q", "2024-07-30")]
    assert fundamentals.quarterize(raw) == []
