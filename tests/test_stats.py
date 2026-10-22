"""Tests for src/stats.py on hand-built recommendation / result / baseline rows."""

from __future__ import annotations

import pandas as pd
import pytest

from src.db import init_db
from src.stats import NOT_ENOUGH_DATA, rank_bucket, summary

# id, module(s), rank, regime, sector, earnings, final_status, entry_date, exit_date, R, pct,
# excess_spy, excess_sector
ROWS = [
    ("r1", ["a"], 1, "favorable", "Technology", 0, "target_hit", "2024-01-03", "2024-01-20", 2.0, 0.10, 0.05, 0.04),
    ("r2", ["a"], 5, "favorable", "Technology", 1, "stopped", "2024-01-04", "2024-01-10", -1.0, -0.05, -0.06, -0.05),
    ("r3", ["a", "b"], 15, "caution", "Financials", 0, "time_stop", "2024-01-05", "2024-02-10", 0.5, 0.02, 0.01, 0.0),
    ("r4", ["b"], 30, "caution", "Financials", 0, "stopped", "2024-01-08", "2024-01-15", -1.0, -0.04, -0.03, -0.03),
    ("r5", ["b"], 60, "unfavorable", "Energy", 0, "expired", None, "2024-01-15", None, None, None, None),
    ("r6", ["b"], 2, "favorable", "Energy", 0, None, None, None, None, None, None, None),  # still pending
]


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "s.db")
    import json
    for rid, mods, rank, regime, sector, earn, fs, ed, xd, r, pct, es, esec in ROWS:
        c.execute(
            "INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,rank,"
            "regime,sector,earnings_in_window,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "live", "2024-01-02", rid.upper(), json.dumps(mods), mods[0], rank, regime,
             sector, earn, fs or "pending"),
        )
        if fs:
            c.execute(
                "INSERT INTO recommendation_results (rec_id,final_status,entry_date,exit_date,"
                "r_multiple,pct_return,excess_vs_spy,excess_vs_sector) VALUES (?,?,?,?,?,?,?,?)",
                (rid, fs, ed, xd, r, pct, es, esec),
            )
    # baselines: two samples for r1 (R 0.0, 1.0), one for r2 (-1.0), one still open
    for bid, parent, r, pct, st in [("r1_B1", "r1", 0.0, 0.0, "time_stop"), ("r1_B2", "r1", 1.0, 0.05, "target_hit"),
                                    ("r2_B1", "r2", -1.0, -0.05, "stopped"), ("r3_B1", "r3", None, None, "open")]:
        c.execute(
            "INSERT INTO baselines (id,parent_rec_id,sample_no,source,signal_date,ticker,stop_pct,status,"
            "r_multiple,pct_return) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (bid, parent, 1, "live", "2024-01-02", "X", 0.05, st, r, pct),
        )
    c.commit()
    return c


def test_rank_buckets():
    assert [rank_bucket(x) for x in (1, 10, 11, 20, 21, 50, 51, 400, None)] == [
        "1-10", "1-10", "11-20", "11-20", "21-50", "21-50", "51+", "51+", "unranked"]


def test_counts_exclude_expired_and_pending_from_closed(conn):
    s = summary(conn, "live")
    assert s["counts"] == {"recommendations": 6, "open": 0, "pending": 1, "closed": 4, "expired": 1}


def test_overall_metrics(conn):
    o = summary(conn, "live")["overall"]
    assert o["count"] == 4
    assert o["win_rate"] == pytest.approx(0.5)
    assert o["avg_r"] == pytest.approx(0.125)
    assert o["median_r"] == pytest.approx(-0.25)
    assert o["expectancy"] == pytest.approx(o["avg_r"])
    assert o["profit_factor"] == pytest.approx(2.5 / 2.0)
    assert o["avg_excess_vs_spy"] == pytest.approx((0.05 - 0.06 + 0.01 - 0.03) / 4)


def test_by_primary_module_and_contains(conn):
    s = summary(conn, "live")
    assert s["by_module_primary"]["a"]["count"] == 3
    assert s["by_module_primary"]["b"]["count"] == 1
    # r3 contributes to both modules when counted by "contains"
    assert s["by_module_contains"]["b"]["count"] == 2
    assert s["by_module_contains"]["a"]["count"] == 3


def test_rank_bucket_groups_in_order_and_values(conn):
    g = summary(conn, "live")["by_rank_bucket"]
    assert list(g) == ["1-10", "11-20", "21-50"]  # 51+ has only the expired rec
    assert g["1-10"]["count"] == 2 and g["1-10"]["avg_r"] == pytest.approx(0.5)


def test_regime_sector_and_earnings_groups(conn):
    s = summary(conn, "live")
    assert s["by_regime"]["favorable"]["count"] == 2
    assert s["by_sector"]["Financials"]["avg_r"] == pytest.approx(-0.25)
    assert s["by_earnings_in_window"]["yes"]["count"] == 1
    assert s["by_earnings_in_window"]["no"]["count"] == 3


def test_baseline_comparison(conn):
    a = summary(conn, "live")["by_module_primary"]["a"]
    # baselines of a's closed recs: R 0, 1, -1  (the open baseline is ignored)
    assert a["baseline_count"] == 3
    assert a["baseline_avg_r"] == pytest.approx(0.0)
    assert a["excess_vs_baseline_r"] == pytest.approx(a["avg_r"] - 0.0)
    assert summary(conn, "live")["by_module_primary"]["b"]["baseline_count"] == 0


def test_equity_curve_is_cumulative_r_by_exit_date(conn):
    curve = summary(conn, "live")["equity_curve"]
    assert [p["date"] for p in curve] == ["2024-01-10", "2024-01-15", "2024-01-20", "2024-02-10"]
    assert [p["cum_r"] for p in curve] == pytest.approx([-1.0, -2.0, 0.0, 0.5])


def test_small_samples_flagged(conn):
    w = summary(conn, "live")["warnings"]
    assert set(w) == {"a", "b"} and all(NOT_ENOUGH_DATA in v for v in w.values())


def test_no_warning_once_enough_closed(tmp_path):
    c = init_db(tmp_path / "big.db")
    for i in range(30):
        c.execute("INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,rank,status)"
                  " VALUES (?,?,?,?,?,?,?,?)", (f"x{i}", "live", "2024-01-02", "T", '["a"]', "a", i + 1, "stopped"))
        c.execute("INSERT INTO recommendation_results (rec_id,final_status,entry_date,exit_date,r_multiple)"
                  " VALUES (?,?,?,?,?)", (f"x{i}", "stopped", "2024-01-03", "2024-01-09", -1.0))
    c.commit()
    s = summary(c, "live")
    assert s["warnings"] == {}
    assert s["overall"]["profit_factor"] == 0.0  # only losers
    assert s["overall"]["win_rate"] == 0.0


def test_empty_source_and_unknown_source(conn):
    s = summary(conn, "nope")
    assert s["counts"]["recommendations"] == 0 and s["overall"]["count"] == 0


# ---- rigor: CIs, paired excess, evidence, fill rate, forward-return horizons ----

from src.config import MeasurementConfig  # noqa: E402
from src.stats import cluster_bootstrap_ci, evidence_label, signal_week  # noqa: E402


def test_expectancy_is_an_alias_of_avg_r(conn):
    for g in summary(conn, "live")["by_module_primary"].values():
        assert g["expectancy"] == g["avg_r"]


def test_signal_week_is_monday():
    assert signal_week("2024-01-03") == "2024-01-01"
    assert signal_week("2024-01-07") == "2024-01-01"
    assert signal_week("2024-01-08") == "2024-01-08"
    assert signal_week(None) is None


def test_cluster_bootstrap_is_deterministic_and_brackets_the_mean():
    vals = [1.0, 2.0, 3.0, -1.0, 0.5, 2.5, 1.5, -0.5]
    weeks = ["w1", "w1", "w2", "w2", "w3", "w3", "w4", "w4"]
    a = cluster_bootstrap_ci(vals, weeks, 2000, 7)
    b = cluster_bootstrap_ci(list(reversed(vals)), list(reversed(weeks)), 2000, 7)
    assert a == b  # same data, any order, same seed -> same CI
    assert a[0] < sum(vals) / len(vals) < a[1]
    assert cluster_bootstrap_ci([1.0, 2.0], ["w1", "w1"], 2000, 7) is None  # one cluster
    assert cluster_bootstrap_ci([1.0], ["w1"], 2000, 7) is None


def test_clustering_widens_the_ci_for_correlated_weeks():
    # every week is internally identical: effective n = number of weeks
    vals = [v for v in (1.0, -0.5, 0.8, -0.2, 0.3) for _ in range(10)]
    clustered = cluster_bootstrap_ci(vals, [v for v in range(5) for _ in range(10)], 2000, 1)
    iid = cluster_bootstrap_ci(vals, list(range(50)), 2000, 1)
    assert clustered[1] - clustered[0] > iid[1] - iid[0]


def test_evidence_labels():
    m = MeasurementConfig(min_n_for_evidence=10, min_weeks_for_evidence=3)
    assert evidence_label(5, 5, [0.1, 0.2], m) == "insufficient"
    assert evidence_label(20, 2, [0.1, 0.2], m) == "insufficient"
    assert evidence_label(20, 5, None, m) == "insufficient"
    assert evidence_label(20, 5, [0.1, 0.2], m) == "positive"
    assert evidence_label(20, 5, [-0.3, -0.1], m) == "negative"
    assert evidence_label(20, 5, [-0.1, 0.2], m) == "no_edge"


def test_paired_excess_fill_rate_and_new_keys(conn):
    s = summary(conn, "live")
    a = s["by_module_primary"]["a"]
    # r1: 2.0 - mean(0, 1) = 1.5 ; r2: -1 - (-1) = 0 ; r3 has no final baseline
    assert a["paired_count"] == 2
    assert a["paired_excess_r"] == pytest.approx(0.75)
    assert a["evidence"] == "insufficient" and a["evidence_basis"] is None
    assert a["signal_weeks"] == 1 and a["avg_r_ci"] is None  # one signal week
    o = s["overall"]
    # 4 filled, 1 expired, 1 pending of 6
    assert o["fill_rate"] == pytest.approx(4 / 5)
    assert o["expired_share"] == pytest.approx(1 / 6)
    assert o["filled"] == 4 and o["expired_unfilled"] == 1
    assert "median_mae_r" in o and "median_mfe_r" in o
    assert s["evidence_by_module"] == {"a": "insufficient", "b": "insufficient"}
    assert s["ci_level"] == 0.95 and s["bootstrap"]["cluster"] == "signal_week"
    # no forward_returns table yet: stable empty shape
    bh = s["by_horizon"]
    assert bh["horizons"] == [5, 10, 20, 40, 60]
    assert bh["overall"]["5d"]["n"] == 0 and bh["overall"]["5d"]["evidence"] == "insufficient"
    assert set(bh["by_module"]) == {"a", "b"}


def _big(tmp_path, n=40, r_of=lambda i: 1.0 + (i % 3) * 0.1, base_r=-0.2):
    c = init_db(tmp_path / "big2.db")
    days = pd.bdate_range("2024-01-01", periods=n)
    for i in range(n):
        sd = days[i].date().isoformat()
        c.execute("INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,rank,status)"
                  " VALUES (?,?,?,?,?,?,?,?)", (f"x{i}", "live", sd, "T", '["a"]', "a", 1, "target_hit"))
        c.execute("INSERT INTO recommendation_results (rec_id,final_status,entry_date,exit_date,r_multiple,"
                  "mae_r,mfe_r) VALUES (?,?,?,?,?,?,?)",
                  (f"x{i}", "target_hit", sd, sd, r_of(i), -0.4, 2.1))
        c.execute("INSERT INTO baselines (id,parent_rec_id,sample_no,source,signal_date,ticker,stop_pct,"
                  "status,r_multiple,pct_return) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (f"x{i}_B1", f"x{i}", 1, "live", sd, "U", 0.05, "stopped", base_r, -0.01))
    c.commit()
    return c, days


def test_positive_evidence_from_paired_excess(tmp_path):
    c, _ = _big(tmp_path)
    s = summary(c, "live")
    a = s["by_module_primary"]["a"]
    assert a["evidence_basis"] == "paired_excess_r" and a["evidence"] == "positive"
    assert a["paired_excess_r_ci"][0] > 0
    assert a["median_mae_r"] == pytest.approx(-0.4) and a["median_mfe_r"] == pytest.approx(2.1)
    assert s["evidence_by_module"] == {"a": "positive"}
    assert summary(c, "live") == s  # deterministic


def test_by_horizon_pairs_each_rec_with_its_own_baselines(tmp_path):
    from src import forward_returns as fr

    c, days = _big(tmp_path, n=8)
    fr.ensure_table(c)
    for i in range(8):
        sd = days[i].date().isoformat()
        for sid, kind, ret in ((f"x{i}", "rec", 0.05 + 0.01 * i), (f"x{i}_B1", "baseline", 0.01),
                               (f"x{i}_B2", "baseline", 0.03)):
            c.execute(
                "INSERT INTO forward_returns (subject_id,horizon,kind,parent_rec_id,source,signal_date,"
                "ticker,ret,spy_return,excess_vs_spy) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, 5, kind, f"x{i}", "live", sd, "T", ret, 0.01, ret - 0.01))
    c.commit()
    bh = summary(c, "live")["by_horizon"]
    h5 = bh["overall"]["5d"]
    assert h5["n"] == 8 and h5["n_paired"] == 8
    assert h5["baseline_mean_ret"] == pytest.approx(0.02)
    assert h5["mean_excess_vs_baseline"] == pytest.approx(0.05 + 0.035 - 0.02)
    assert h5["mean_excess_vs_spy"] == pytest.approx(0.085 - 0.01)
    assert h5["excess_vs_baseline_ci"][0] > 0
    assert h5["evidence"] == "insufficient"  # 8 < min_n_for_evidence
    assert bh["by_module"]["a"]["5d"] == h5
    assert bh["overall"]["10d"]["n"] == 0
