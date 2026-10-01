"""End-to-end daily pipeline on the synthetic fixtures (network calls stubbed, everything
else real): prepare -> simulated brief outputs -> finalize."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone

import run_daily
from tests.e2e.conftest import make_root, now_after, seed_last_day, seed_recommendation, tree_bytes

TARGET = date(2024, 12, 31)


def write_briefs(work, summary_prefix="Brief for") -> list[str]:
    """Play the routine agent: one valid output JSON per input file."""
    out = work / "brief_outputs"
    out.mkdir(parents=True, exist_ok=True)
    tickers = []
    for f in sorted((work / "brief_inputs").glob("*.json")):
        t = f.stem
        tickers.append(t)
        (out / f"{t}.json").write_text(json.dumps({
            "ticker": t, "summary": f"{summary_prefix} {t}: no relevant filings or news in the input window.",
            "upcoming_catalysts": [], "red_flags": [], "recent_positive_events": [], "sources": [],
        }), encoding="utf-8")
    return tickers


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def all_recs(state):
    return [r for f in sorted((state / "recommendations").glob("*.csv")) for r in read_csv(f)]


def run_full(env, target=TARGET, last=date(2024, 12, 30), briefs=True):
    seed_last_day(env.state, last)
    now = now_after(target)
    assert run_daily.prepare(env.cfg, now=now) == 0
    tickers = write_briefs(env.work) if briefs else []
    assert run_daily.finalize(env.cfg, now=now) == 0
    return tickers


def test_prepare_finalize_produces_reports_state_and_briefs(env):
    tickers = run_full(env)

    for name in ("2024-12-31.html", "2024-12-31.md", "latest.html", "latest.md"):
        assert (env.reports / name).is_file(), name
    html = (env.reports / "2024-12-31.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()
    assert "src=\"http" not in html and "href=\"http" not in html.replace("href=\"https://www.sec.gov", "")
    assert (env.reports / "latest.html").read_text(encoding="utf-8") == html

    today = [r for r in all_recs(env.state) if r["signal_date"] == "2024-12-31"]
    assert any(r["ticker"] == "MOMA1" for r in today), "module A fixture signal missing"
    jobs = read_csv(env.state / "job_log.csv")
    assert {"day", "finalize"} <= {j["job"] for j in jobs}
    assert any(j["job"] == "day" and j["trading_date"] == "2024-12-31" and j["status"] == "ok" for j in jobs)
    assert any(r["parent_rec_id"] for f in (env.state / "baselines").glob("*.csv") for r in read_csv(f))

    assert tickers, "no brief inputs were written"
    assert (env.work / "brief_inputs" / "INSTRUCTIONS.md").is_file()
    for t in tickers:
        assert f"Brief for {t}" in html
    assert {r["ticker"] for r in today if r["llm_brief"]} == set(tickers)

    assert len(env.calls.report) == 1 and env.calls.report[0][0].endswith("2024-12-31.html")
    assert env.calls.alert == []


def test_missing_briefs_do_not_block_the_report(env):
    run_full(env, briefs=False)
    assert (env.reports / "2024-12-31.html").is_file()
    assert "Brief for" not in (env.reports / "2024-12-31.html").read_text(encoding="utf-8")


def test_invalid_brief_is_dropped_but_report_is_built(env):
    seed_last_day(env.state, date(2024, 12, 30))
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    out = env.work / "brief_outputs"
    out.mkdir(parents=True)
    for f in (env.work / "brief_inputs").glob("*.json"):
        (out / f.name).write_text("{not json", encoding="utf-8")
    assert run_daily.finalize(env.cfg, now=now) == 0
    assert (env.reports / "2024-12-31.html").is_file()
    assert not any(r["llm_brief"] for r in all_recs(env.state))


def test_rerun_changes_nothing(env):
    run_full(env)
    before = tree_bytes(env.state, env.reports)
    n_reports = len(env.calls.report)
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert run_daily.finalize(env.cfg, now=now) == 0
    assert tree_bytes(env.state, env.reports) == before
    assert len(env.calls.report) == n_reports  # not re-sent
    assert env.calls.edgar == [TARGET]  # prepare did no second pass


def test_forced_reprocessing_does_not_duplicate_rows(env):
    run_full(env)
    ids_before = [r["id"] for r in all_recs(env.state)]
    assert run_daily.prepare(env.cfg, now=now_after(TARGET), force=True) == 0
    ids_after = [r["id"] for r in all_recs(env.state)]
    assert sorted(ids_after) == sorted(ids_before) and len(ids_after) == len(set(ids_after))
    baselines = [r["id"] for f in (env.state / "baselines").glob("*.csv") for r in read_csv(f)]
    assert len(baselines) == len(set(baselines))


def test_state_is_deterministic_across_independent_runs(env, tmp_path_factory):
    run_full(env)
    first = tree_bytes(env.state, env.reports)

    root2 = tmp_path_factory.mktemp("second")
    cfg2 = make_root(root2)
    seed_last_day(root2 / "state", date(2024, 12, 30))
    now = now_after(TARGET)
    assert run_daily.prepare(cfg2, now=now) == 0
    write_briefs(root2 / "work")
    assert run_daily.finalize(cfg2, now=now) == 0
    second = tree_bytes(root2 / "state", root2 / "reports")
    strip = lambda d: {k.split("/", 1)[1]: v for k, v in d.items()}  # noqa: E731
    assert strip(first) == strip(second)


def test_catch_up_three_missed_days_in_order(env):
    last, target = date(2024, 12, 20), date(2024, 12, 26)
    seed_last_day(env.state, last)
    now = now_after(target)
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert env.calls.edgar == [date(2024, 12, 23), date(2024, 12, 24), date(2024, 12, 26)]
    assert len(env.calls.prices) == 1, "prices must be downloaded once for the whole range"

    jobs = read_csv(env.state / "job_log.csv")
    done = sorted(j["trading_date"] for j in jobs if j["job"] == "day" and j["status"] == "ok")
    assert done == ["2024-12-20", "2024-12-23", "2024-12-24", "2024-12-26"]
    regimes = {r["date"] for r in read_csv(env.state / "regime_log.csv")}
    assert {"2024-12-23", "2024-12-24", "2024-12-26"} <= regimes

    recs = all_recs(env.state)
    assert any(r["ticker"] == "MOMD1" and r["signal_date"] == "2024-12-24" for r in recs), \
        "module D fixture signal on a missed day was not found"

    for f in (env.work / "brief_inputs").glob("*.json"):  # briefs only for the latest day
        assert json.loads(f.read_text(encoding="utf-8"))["as_of"] == "2024-12-26"

    assert run_daily.finalize(env.cfg, now=now) == 0
    md = (env.reports / "2024-12-26.md").read_text(encoding="utf-8")
    assert "MOMD1" in md, "catch-up recommendation missing from the report"


def test_split_during_open_trade_is_handled(env):
    """A recommendation struck before SPLIT1's 2-for-1 split (2024-12-19, raw history in
    the fixture) must be tracked on split-adjusted levels, not stopped out by the price halving."""
    seed_last_day(env.state, date(2024, 12, 30))
    seed_recommendation(
        env.state, id="2024-12-17_SPLIT1", signal_date="2024-12-17", ticker="SPLIT1",
        entry=197.0, stop=193.0, target=205.0, valid_until="2024-12-24", sector="Technology",
        industry="Technology Industry", last_updated="2024-12-17")
    assert run_daily.prepare(env.cfg, now=now_after(TARGET)) == 0
    assert env.calls.splits and "SPLIT1" in env.calls.splits[0], "splits must be refreshed for open recommendations"

    rec = {r["id"]: r for r in all_recs(env.state)}["2024-12-17_SPLIT1"]
    assert rec["status"] != "stopped"
    results = {r["rec_id"]: r for f in (env.state / "recommendation_results").glob("*.csv") for r in read_csv(f)}
    if "2024-12-17_SPLIT1" in results:
        res = results["2024-12-17_SPLIT1"]
        assert abs(float(res["pct_return"])) < 0.25 and abs(float(res["r_multiple"])) < 8
    else:
        assert abs(float(rec["current_r"] or 0)) < 8


def test_dry_run_writes_no_state_or_reports_and_sends_nothing(env):
    seed_last_day(env.state, date(2024, 12, 30))
    before = tree_bytes(env.state)
    now = now_after(TARGET)
    assert run_daily.prepare(env.cfg, now=now, dry_run=True) == 0
    assert run_daily.finalize(env.cfg, now=now, dry_run=True) == 0
    assert tree_bytes(env.state) == before
    assert list(env.reports.iterdir()) == []
    assert env.calls.report == [] and env.calls.alert == []
    assert not (env.work / "brief_inputs").exists()


def test_unfinished_session_is_skipped(env):
    seed_last_day(env.state, date(2024, 12, 30))
    before = tree_bytes(env.state)
    # the 2024-12-31 session has not closed yet at 12:00 UTC on that day
    assert run_daily.prepare(env.cfg, target=TARGET,
                             now=datetime(2024, 12, 31, 12, 0, tzinfo=timezone.utc)) == 0
    assert tree_bytes(env.state) == before and env.calls.prices == []


def test_report_after_a_gap_lists_missed_days_in_the_catch_up_section(env):
    """Day 1 is finalised normally; the routine then misses three sessions. The next report
    must cover them (catch-up section) because 'since' is the last finalised day."""
    run_full(env, target=date(2024, 12, 20), last=date(2024, 12, 19))
    now = now_after(date(2024, 12, 26))
    assert run_daily.prepare(env.cfg, now=now) == 0
    assert run_daily.finalize(env.cfg, now=now) == 0
    md = (env.reports / "2024-12-26.md").read_text(encoding="utf-8")
    assert "Catch-up: recommendations from missed days" in md
    catch_up = md.split("Catch-up: recommendations from missed days", 1)[1]
    assert "MOMD1" in catch_up


def test_module_b_signal_flows_through_on_the_regenerated_fixture(env):
    """MOMB1's +7% opening gap (earnings 2024-12-03) triggers module B on 2024-12-10."""
    last, target = date(2024, 12, 9), date(2024, 12, 10)
    seed_last_day(env.state, last)
    assert run_daily.prepare(env.cfg, now=now_after(target)) == 0
    rec = next(r for r in all_recs(env.state) if r["ticker"] == "MOMB1")
    assert rec["signal_date"] == "2024-12-10" and "earnings_gap_drift" in rec["modules"]
