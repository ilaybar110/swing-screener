"""Audit (Bot 8) spec-compliance tests that no earlier bot had covered."""
from __future__ import annotations

import csv
from datetime import date

import pytest

from src import guardrail
from src.config import load_config
from src.contracts import Candidate
from src.data_access import DataAccess
from src.db import init_db
from src.modules import get_enabled_modules
from src.ranking import rank
from src.state_io import export_state, import_state

from tests.test_guardrail import _insert_fundamental, _seed_healthy_company

AS_OF = date(2024, 12, 31)


# --- ranking: same-day multi-module merge uses the primary (highest setup) module ----


def test_merge_uses_primary_modules_entry_and_stop(writable_fixture_conn):
    conn = writable_fixture_conn
    data = DataAccess(conn)
    bar = data.get_prices("MOMA1", AS_OF, AS_OF).iloc[0]
    hi = float(bar["high"])
    weak = Candidate("MOMA1", "base_breakout", AS_OF, hi, hi * 0.90, 40.0, "weak", {})
    strong = Candidate("MOMA1", "momentum_pullback", AS_OF, hi * 1.0, hi * 0.95, 80.0, "strong", {})
    for cands in ([weak, strong], [strong, weak]):  # order of input must not matter
        (rec,) = rank(cands, AS_OF, data, conn, load_config())
        assert rec.primary_module == "momentum_pullback"
        assert rec.entry == pytest.approx(strong.entry)
        assert rec.stop == pytest.approx(strong.stop)
        assert rec.target == pytest.approx(strong.entry + 2 * (strong.entry - strong.stop))
        assert set(rec.modules) == {"base_breakout", "momentum_pullback"}


# --- modules: stop distance outside 2-12 % is skipped ----------------------------------


def test_stop_distance_outside_band_is_skipped(data_access):
    cfg = load_config()
    mods = get_enabled_modules(cfg)
    base = [c for m in mods for c in m.scan_history(date(2024, 12, 1), AS_OF, data_access)]
    assert base, "fixture should yield at least one candidate"
    # widen the minimum above every real stop distance -> everything is skipped
    cfg_hi = load_config()
    cfg_hi.modules.stop_distance_pct_min = 0.50
    assert [c for m in get_enabled_modules(cfg_hi) for c in m.scan_history(date(2024, 12, 1), AS_OF, data_access)] == []
    # squeeze the maximum below every real stop distance -> everything is skipped
    cfg_lo = load_config()
    cfg_lo.modules.stop_distance_pct_min = 0.0
    cfg_lo.modules.stop_distance_pct_max = 0.0001
    assert [c for m in get_enabled_modules(cfg_lo) for c in m.scan_history(date(2024, 12, 1), AS_OF, data_access)] == []


# --- guardrail: point-in-time ----------------------------------------------------------


def test_guardrail_ignores_fundamentals_filed_after_as_of(writable_fixture_conn):
    conn = writable_fixture_conn
    t = _seed_healthy_company(conn)
    data = DataAccess(conn)
    before = guardrail.evaluate(t, AS_OF, data, fetch_fn=lambda *a: None)
    assert before.status == "pass"
    cik = "0000000099"
    # a restatement of an in-window quarter AND a later quarter, both filed after as_of
    for metric, val in (("operating_income", -5000.0), ("operating_cash_flow", -5000.0)):
        _insert_fundamental(conn, cik, t, metric, "2024-12-31", val, "2025-02-15")
        _insert_fundamental(conn, cik, t, metric, "2025-03-31", val, "2025-05-01")
    _insert_fundamental(conn, cik, t, "shares_outstanding", "2025-03-31", 9_000_000, "2025-05-01", period_type="instant")
    conn.commit()
    after = guardrail.evaluate(t, AS_OF, data, fetch_fn=lambda *a: None)
    assert after.status == before.status == "pass"
    assert after.metrics == before.metrics


def test_guardrail_missing_data_is_unknown_never_fail(writable_fixture_conn):
    conn = writable_fixture_conn
    res = guardrail.evaluate("CTRL01", AS_OF, DataAccess(conn), fetch_fn=lambda *a: None)
    # the fixture has no fundamentals rows at all for CTRL01
    assert res.status == "unknown"


# --- state_io retention ----------------------------------------------------------------


def _rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_state_export_retention_windows_and_latest_summary(tmp_path):
    conn = init_db(tmp_path / "r.db")
    asof = date(2024, 12, 31)
    # insider: 120 days -> 2024-09-02 kept, 2024-09-01 dropped
    for i, fd in enumerate(["2024-09-01", "2024-09-02", "2024-12-30"]):
        conn.execute("INSERT INTO insider_trades (accession, row_num, ticker, filed_date) VALUES (?,?,?,?)", (f"a{i}", 1, "T", fd))
    # earnings: 180 days -> 2024-07-04 kept, 2024-07-03 dropped, future kept
    for d in ["2024-07-03", "2024-07-04", "2025-01-15"]:
        conn.execute("INSERT INTO earnings_dates (ticker, event_date, source) VALUES ('T', ?, 's')", (d,))
    for i, fd in enumerate(["2024-09-01", "2024-09-02"]):
        conn.execute("INSERT INTO filings (accession, cik, form, filed_date) VALUES (?, '1', '8-K', ?)", (f"f{i}", fd))
    for ao in ["2024-12-20", "2024-12-27"]:
        conn.execute("INSERT INTO fundamentals_summary (as_of, ticker) VALUES (?, 'T')", (ao,))
    conn.commit()
    out = tmp_path / "state"
    export_state(conn, out, as_of=asof)
    assert [r["accession"] for r in _rows(out / "insider_trades.csv")] == ["a1", "a2"]
    assert [r["event_date"] for r in _rows(out / "earnings_dates.csv")] == ["2024-07-04", "2025-01-15"]
    assert [r["accession"] for r in _rows(out / "filings.csv")] == ["f1"]
    assert {r["as_of"] for r in _rows(out / "fundamentals_summary.csv")} == {"2024-12-27"}


def test_recommendations_are_sharded_by_month_and_only_live(tmp_path):
    conn = init_db(tmp_path / "s.db")
    for rid, src, d in [("2024-11-29_A", "live", "2024-11-29"), ("2024-12-02_B", "live", "2024-12-02"),
                        ("run1/2024-12-03_C", "run1", "2024-12-03")]:
        conn.execute("INSERT INTO recommendations (id, source, signal_date, ticker) VALUES (?,?,?,?)", (rid, src, d, rid[-1]))
    conn.commit()
    out = tmp_path / "state"
    export_state(conn, out, as_of=date(2024, 12, 31))
    shards = sorted(p.name for p in (out / "recommendations").glob("*.csv"))
    assert shards == ["2024-11.csv", "2024-12.csv"]
    assert [r["id"] for r in _rows(out / "recommendations" / "2024-12.csv")] == ["2024-12-02_B"]
    # round trip is byte-identical
    c2 = init_db(tmp_path / "s2.db")
    import_state(c2, out)
    out2 = tmp_path / "state2"
    export_state(c2, out2, as_of=date(2024, 12, 31))
    for p in sorted(out.rglob("*.csv")):
        assert p.read_bytes() == (out2 / p.relative_to(out)).read_bytes(), p


# --- --base-dir / SWING_BASE_DIR override ----------------------------------------------


def test_base_dir_override_redirects_all_paths(tmp_path, monkeypatch):
    from src.config import BASE_DIR_ENV, REPO_ROOT

    monkeypatch.delenv(BASE_DIR_ENV, raising=False)
    assert load_config().paths.state_dir == REPO_ROOT / "state"
    monkeypatch.setenv(BASE_DIR_ENV, str(tmp_path))
    p = load_config().paths
    for name in ("run_db", "research_db", "state_dir", "reports_dir", "raw_cache_dir", "logs_dir"):
        assert str(getattr(p, name)).startswith(str(tmp_path.resolve())), name


def test_entrypoints_accept_base_dir(tmp_path, monkeypatch):
    import run_weekly
    from src.config import BASE_DIR_ENV

    monkeypatch.delenv(BASE_DIR_ENV, raising=False)
    seen = {}
    monkeypatch.setattr(run_weekly, "run", lambda cfg, **kw: seen.update(state=cfg.paths.state_dir) or 0)
    assert run_weekly.main(["--base-dir", str(tmp_path), "--dry-run"]) == 0
    assert seen["state"] == tmp_path.resolve() / "state"
    monkeypatch.delenv(BASE_DIR_ENV, raising=False)


# --- guardrail: share counts are split-adjusted --------------------------------------


def _seed_shares(conn, ticker, cik, first, last):
    from tests.test_guardrail import _insert_ticker

    _insert_ticker(conn, ticker, cik)
    for end in ["2023-12-31", "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]:
        _insert_fundamental(conn, cik, ticker, "operating_income", end, 10.0, end)
        _insert_fundamental(conn, cik, ticker, "operating_cash_flow", end, 15.0, end)
        _insert_fundamental(conn, cik, ticker, "capex", end, 3.0, end)
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2023-12-31", first, "2023-12-31", period_type="instant")
    _insert_fundamental(conn, cik, ticker, "shares_outstanding", "2024-12-31", last, "2024-12-31", period_type="instant")
    conn.commit()


def test_stock_split_is_not_counted_as_dilution(writable_fixture_conn):
    conn = writable_fixture_conn
    _seed_shares(conn, "SPLT", "0000000081", 1_000_000, 2_040_000)  # 2:1 split + 2 % real growth
    data = DataAccess(conn)
    unadjusted = guardrail.evaluate("SPLT", AS_OF, data, fetch_fn=lambda *a: None)
    assert unadjusted.status == "fail"  # no split known -> looks like +104 %
    conn.execute("INSERT INTO splits (ticker, date, ratio) VALUES ('SPLT', '2024-06-10', 2.0)")
    conn.commit()
    adjusted = guardrail.evaluate("SPLT", AS_OF, data, fetch_fn=lambda *a: None)
    assert adjusted.status == "pass_partial"  # debt not seeded -> that check is unevaluated (D2)
    assert adjusted.metrics["shares_growth_yoy"] == pytest.approx(0.02)
    # a split dated after as_of is not known yet and must not change the result
    conn.execute("DELETE FROM splits WHERE ticker='SPLT'")
    conn.execute("INSERT INTO splits (ticker, date, ratio) VALUES ('SPLT', '2025-02-10', 2.0)")
    conn.commit()
    assert guardrail.evaluate("SPLT", AS_OF, data, fetch_fn=lambda *a: None).status == "fail"


def test_fundamentals_found_for_second_share_class_via_cik(writable_fixture_conn):
    conn = writable_fixture_conn
    _seed_shares(conn, "CLSA", "0000000071", 1_000_000, 1_010_000)
    conn.execute(
        "INSERT INTO tickers (ticker, cik, name, exchange, sector, industry, sic, sector_etf, is_active, updated_at) "
        "VALUES ('CLSB','0000000071','x','NYSE','Technology','x',NULL,'XLK',1,'2024-12-31')")
    conn.commit()
    data = DataAccess(conn)
    assert not data.get_fundamentals("CLSB", AS_OF).empty
    assert guardrail.evaluate("CLSB", AS_OF, data, fetch_fn=lambda *a: None).status == "pass_partial"  # D2: no debt data


# --- Module B: earnings timing -> reaction session; NULL timing / NULL titles (NaN) ----


def _b_sessions(rows):
    import pandas as pd

    from src.modules.b_earnings_gap_drift import EarningsGapDrift

    cal = pd.DatetimeIndex(pd.bdate_range("2024-12-02", "2024-12-13"))  # Mon..Fri x2
    ev = pd.DataFrame(rows, columns=["event_date", "timing", "source"])
    return [cal[p].date().isoformat() for p in EarningsGapDrift(load_config())._gap_sessions(ev, cal)]


def test_module_b_maps_event_timing_to_reaction_session():
    # 8-K derived rows already carry the reaction session, whatever the timing label
    assert _b_sessions([("2024-12-04", "AMC", "8k_2.02")]) == ["2024-12-04"]
    assert _b_sessions([("2024-12-04", "BMO", "8k_2.02")]) == ["2024-12-04"]
    assert _b_sessions([("2024-12-04", None, "8k_2.02")]) == ["2024-12-04"]
    # Nasdaq calendar rows carry the report date: AMC reacts the next session
    assert _b_sessions([("2024-12-04", "BMO", "nasdaq_calendar")]) == ["2024-12-04"]
    assert _b_sessions([("2024-12-04", "AMC", "nasdaq_calendar")]) == ["2024-12-05"]
    assert _b_sessions([("2024-12-06", "AMC", "nasdaq_calendar")]) == ["2024-12-09"]  # Friday -> Monday
    assert _b_sessions([("2024-12-04", None, "nasdaq_calendar")]) == ["2024-12-04", "2024-12-05"]


def test_module_b_and_c_tolerate_null_text_columns():
    import numpy as np
    import pandas as pd

    from src.modules.c_insider_cluster import _seniority

    assert _b_sessions([("2024-12-04", np.nan, "nasdaq_calendar")]) == ["2024-12-04", "2024-12-05"]
    assert _seniority(pd.Series({"officer_title": np.nan, "is_officer": 1})) == 0.6
    assert _seniority(pd.Series({"officer_title": "Chief Executive Officer", "is_officer": 1})) == 1.0


# --- earnings flag: only reactions still ahead of the signal day count --------------------


def test_next_earnings_ignores_reaction_already_behind_the_signal_day(writable_fixture_conn):
    from src.trade_plan import next_earnings

    conn = writable_fixture_conn
    conn.execute("DELETE FROM earnings_dates WHERE ticker='CTRL05'")
    rows = [
        ("CTRL05", "2024-12-31", "AMC", "8k_2.02", "2024-12-30T16:10:00"),          # reaction = signal day: behind
        ("CTRL05", "2024-12-31", "BMO", "nasdaq_calendar", None),                    # reported before the open: behind
        ("CTRL05", "2025-01-30", "AMC", "nasdaq_calendar", None),                    # genuinely ahead
    ]
    conn.executemany("INSERT INTO earnings_dates (ticker, event_date, timing, source, acceptance_datetime) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    data = DataAccess(conn)
    assert next_earnings(data, "CTRL05", AS_OF) == date(2025, 1, 30)
    # a calendar row on the signal day itself with AMC/unknown timing reacts tomorrow: still ahead
    conn.execute("INSERT INTO earnings_dates (ticker, event_date, timing, source) VALUES ('CTRL05','2024-12-31','AMC','nasdaq_calendar2')")
    conn.commit()
    assert next_earnings(data, "CTRL05", AS_OF) == date(2024, 12, 31)
