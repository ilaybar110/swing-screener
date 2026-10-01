"""Unit tests for src/tracker.py: every rule of docs/PLAN.md section 11, driven by
hand-built price paths. Planned trade throughout: entry 100, stop 95 (R = 5),
target 110, cost 0.1% (= 0.02R at a 100 fill)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.config import load_config
from src.contracts import Recommendation
from src.db import init_db
from src.tracker import row_to_rec, simulate, update_all

CFG = load_config()
WARM = 25
COST_R = 0.02  # 0.001 * 100 / 5
TICKER = "TEST"


def _dates(n: int):
    return list(pd.bdate_range("2024-01-02", periods=n).date)


def mk_prices(post, warm_px=100.0, ticker=TICKER, scale=1.0):
    """Flat warm-up history then `post` (o,h,l,c) bars. Returns (df, signal_date)."""
    rows = [(warm_px,) * 4] * WARM + list(post)
    ds = _dates(len(rows))
    df = pd.DataFrame(
        {
            "date": [d.isoformat() for d in ds],
            "open": [r[0] * scale for r in rows],
            "high": [r[1] * scale for r in rows],
            "low": [r[2] * scale for r in rows],
            "close": [r[3] * scale for r in rows],
            "ticker": ticker,
        }
    )
    return df, ds[WARM - 1]


def mk_rec(signal_date, entry=100.0, stop=95.0, target=110.0, sector=None, rid="T1"):
    return Recommendation(
        id=rid, source="live", signal_date=signal_date, ticker=TICKER, modules=["a"],
        primary_module="a", setup_score=50, rs_pct=50, overlap_score=0, track_score=0,
        total_score=50, rank=1, in_report=True, entry=entry, stop=stop, target=target,
        valid_until=signal_date, regime="favorable", sector=sector, industry=None,
        earnings_date=None, earnings_in_window=False, guardrail_status="pass",
        guardrail_reasons=[], valuation_info={}, rationale="", details={}, llm_brief=None,
        status="pending", current_r=None, last_updated="",
    )


def run(post, **kw):
    df, sig = mk_prices(post)
    return simulate(mk_rec(sig, **kw), df, None, CFG)


FILL = (99, 101, 98, 100.5)  # fills at 100 on a normal bar, stays above the stop


# ---- pending / expiry -----------------------------------------------------


def test_no_bars_after_signal_is_pending():
    assert run([]).status == "pending"


def test_unfilled_inside_window_is_pending():
    res = run([(97, 99, 96, 98)] * 4)
    assert res.status == "pending" and not res.is_final


def test_expires_after_five_bars_without_fill():
    res = run([(97, 99, 96, 98)] * 5)
    assert res.status == "expired" and res.is_final
    assert res.exit_reason == "entry_window_elapsed"
    assert res.r_multiple is None and res.entry_date is None


def test_fill_on_fifth_bar_is_still_valid():
    res = run([(97, 99, 96, 98)] * 4 + [FILL])
    assert res.status == "open" and res.entry_fill == 100


def test_fill_on_sixth_bar_is_too_late():
    res = run([(97, 99, 96, 98)] * 5 + [FILL])
    assert res.status == "expired"


def test_setup_breaking_before_entry_expires_early():
    res = run([(97, 99, 96, 98), (96, 98, 93, 94), FILL])
    assert res.status == "expired" and res.exit_reason == "setup_broken"
    assert res.is_final


def test_close_below_stop_on_a_filling_bar_is_a_stop_out_not_expiry():
    # high reaches entry, low pierces the stop, closes under it: filled then stopped
    res = run([(97, 100.5, 93, 94)])
    assert res.status == "stopped"


# ---- entry fills ----------------------------------------------------------


def test_fill_at_entry_and_mark_to_market():
    res = run([FILL])
    assert res.status == "open" and not res.is_final
    assert res.entry_fill == 100
    assert res.r_multiple == pytest.approx(0.5 / 5 - COST_R)  # current_r


def test_gap_above_entry_fills_at_open():
    res = run([(102, 104, 101, 103)])
    assert res.entry_fill == 102
    assert res.status == "open"


# ---- stop rules -----------------------------------------------------------


def test_stop_touched_on_entry_day_counts():
    res = run([(99, 101, 94, 96)])
    assert res.status == "stopped" and res.exit_reason == "stop"
    assert res.avg_exit_price == 95
    assert res.r_multiple == pytest.approx(-1 - COST_R)
    assert res.days_held == 0


def test_gap_down_through_stop_fills_at_open():
    res = run([FILL, (92, 93, 90, 91)])
    assert res.status == "stopped"
    assert res.avg_exit_price == 92
    assert res.r_multiple == pytest.approx((92 - 100) / 5 - COST_R)
    assert res.mae_r < -1


def test_plain_stop_out_after_entry():
    res = run([FILL, (100, 101, 94, 95)])
    assert res.status == "stopped" and res.avg_exit_price == 95
    assert res.days_held == 1


def test_stop_and_target_same_day_stop_wins():
    res = run([FILL, (100, 111, 94, 100)])
    assert res.status == "stopped" and res.exit_reason == "stop"
    assert res.r_multiple == pytest.approx(-1 - COST_R)


def test_gap_entry_counts_planned_risk():
    # filled at 102 (gap), stop 95 hit: R uses planned risk 5 -> (95-102)/5
    res = run([(102, 104, 101, 103), (100, 101, 94, 95)])
    assert res.r_multiple == pytest.approx((95 - 102) / 5 - 0.001 * 102 / 5)


# ---- target, trailing, breakeven -----------------------------------------


def test_target_takes_half_and_stays_open():
    res = run([FILL, (100, 111, 99, 110)])
    assert res.status == "open"
    # half at 110, half marked at close 110 -> +2R gross
    assert res.r_multiple == pytest.approx(2.0 - COST_R)


def test_partial_then_breakeven_stop():
    res = run([FILL, (100, 111, 99, 110), (108, 109, 99.5, 101)])
    assert res.status == "target_hit" and res.exit_reason == "breakeven"
    assert res.avg_exit_price == pytest.approx(105)
    assert res.r_multiple == pytest.approx(1.0 - COST_R)
    assert res.mfe_r == pytest.approx(2.2)


def test_breakeven_stop_only_active_from_next_bar():
    # target bar itself trades down to 99 (< fill) yet the trade must not stop out
    res = run([FILL, (100, 111, 99, 110)])
    assert res.status == "open"


def test_gap_below_breakeven_exits_at_open():
    res = run([FILL, (100, 111, 99, 110), (97, 98, 96, 97)])
    assert res.status == "target_hit"
    assert res.avg_exit_price == pytest.approx(0.5 * 110 + 0.5 * 97)


def test_remainder_exits_on_close_below_sma20_at_next_open():
    path = [FILL, (100, 111, 100, 110)]
    path += [(118, 121, 117, 120)] * 14
    path += [(119, 119, 104, 105)]  # close 105 < SMA20 (~114) -> sell next open
    sig_only = run(path)
    assert sig_only.status == "open"  # exit not yet filled
    res = run(path + [(103, 104, 102, 103)])
    assert res.status == "target_hit" and res.exit_reason == "sma20_exit"
    assert res.avg_exit_price == pytest.approx(0.5 * 110 + 0.5 * 103)
    assert res.r_multiple == pytest.approx((106.5 - 100) / 5 - COST_R)
    assert res.days_held == 17


def test_sma_exit_not_used_before_target_is_hit():
    # price drifts above entry but below a high SMA: no partial, so no SMA exit
    df, sig = mk_prices([FILL] + [(103, 104, 102, 103)] * 5, warm_px=120)
    res = simulate(mk_rec(sig, entry=100, stop=95, target=110), df, None, CFG)
    # warm bars at 120 are above entry so the setup is odd; just ensure still open
    assert res.status == "open"


# ---- time stop -------------------------------------------------------------


def test_time_stop_at_close_of_20th_bar_after_entry():
    flat = (100, 102, 99, 101)
    res = run([FILL] + [flat] * 20)
    assert res.status == "time_stop" and res.days_held == 20
    assert res.avg_exit_price == 101
    assert res.r_multiple == pytest.approx(1 / 5 - COST_R)


def test_data_ending_before_time_stop_stays_open():
    flat = (100, 102, 99, 101)
    res = run([FILL] + [flat] * 19)
    assert res.status == "open" and not res.is_final


def test_time_stop_after_partial_blends_halves():
    path = [FILL, (100, 111, 99.5, 110)] + [(108, 111, 107, 110)] * 19
    res = run(path)
    assert res.status == "time_stop"
    assert res.avg_exit_price == pytest.approx(110)
    assert res.r_multiple == pytest.approx(2.0 - COST_R)


# ---- excursions & returns -------------------------------------------------


def test_mae_mfe_in_r():
    res = run([FILL, (100, 108, 97, 101), (101, 103, 94, 95)])
    assert res.status == "stopped"
    assert res.mfe_r == pytest.approx(1.6)  # high 108 on bar 2
    assert res.mae_r == pytest.approx(-1.0)  # stopped at 95


def test_entry_day_low_before_fill_is_not_counted_as_mae():
    res = run([(98, 101, 96, 100.5)])  # dipped to 96 before the buy-stop triggered
    assert res.mae_r == 0.0


def test_benchmark_returns_over_holding_period():
    df, sig = mk_prices([FILL, (100, 102, 99, 101), (100, 101, 94, 95)])
    ds = _dates(len(df))
    spy_rows, xlk_rows = [], []
    for i, d in enumerate(ds):
        spy_rows.append({"date": d.isoformat(), "open": 400 + i, "high": 410 + i,
                         "low": 390 + i, "close": 400 + i + 0.5, "ticker": "SPY"})
        xlk_rows.append({"date": d.isoformat(), "open": 200.0, "high": 201.0, "low": 199.0,
                         "close": 202.0, "ticker": "XLK"})
    full = pd.concat([df, pd.DataFrame(spy_rows), pd.DataFrame(xlk_rows)], ignore_index=True)
    res = simulate(mk_rec(sig, sector="Technology"), full, None, CFG)
    e, x = WARM, WARM + 2  # entry bar index, exit bar index
    assert res.spy_return == pytest.approx((400 + x + 0.5) / (400 + e) - 1)
    assert res.sector_etf_return == pytest.approx(202 / 200 - 1)
    assert res.excess_vs_spy == pytest.approx(res.pct_return - res.spy_return)
    assert res.excess_vs_sector == pytest.approx(res.pct_return - res.sector_etf_return)


def test_pct_return_includes_round_trip_cost():
    res = run([FILL, (100, 101, 94, 95)])
    assert res.pct_return == pytest.approx(95 / 100 - 1 - 0.001)


# ---- splits ----------------------------------------------------------------


def _split_path():
    # fill, run up, target bar - all in pre-split terms, then identical shape halved
    return [FILL, (101, 103, 100.5, 102), (102, 111, 101, 110)]


def _split_case(adjusted: bool):
    df, sig = mk_prices(_split_path())
    ds = _dates(len(df))
    split_day = ds[WARM + 2]  # the target bar is the first post-split session
    # post-split bars halved; split date bar and later
    post = df["date"] >= split_day.isoformat()
    df_raw = df.copy()
    for c in ("open", "high", "low", "close"):
        df_raw.loc[post, c] = df.loc[post, c] / 2
    if adjusted:  # yfinance style: whole history retro-adjusted
        for c in ("open", "high", "low", "close"):
            df_raw.loc[~post, c] = df.loc[~post, c] / 2
    splits = pd.DataFrame({"date": [split_day.isoformat()], "ratio": [2.0]})
    return simulate(mk_rec(sig), df_raw, splits, CFG), simulate(mk_rec(sig), df, None, CFG)


@pytest.mark.parametrize("adjusted", [True, False])
def test_split_during_open_trade_matches_unsplit_trade(adjusted):
    res, ref = _split_case(adjusted)
    assert res.status == ref.status == "open"
    assert res.r_multiple == pytest.approx(ref.r_multiple)
    assert res.entry_fill == pytest.approx(ref.entry_fill / 2)
    assert res.mfe_r == pytest.approx(ref.mfe_r)


def test_split_before_signal_does_not_adjust_levels():
    df, sig = mk_prices([FILL])
    splits = pd.DataFrame({"date": ["2023-06-01"], "ratio": [2.0]})
    res = simulate(mk_rec(sig), df, splits, CFG)
    assert res.entry_fill == 100


# ---- baseline entry mode ----------------------------------------------------


def test_open_mode_enters_next_open_with_same_stop_pct():
    df, sig = mk_prices([(50, 51, 49, 50.5), (50.5, 56, 50, 55)])
    rec = mk_rec(sig)  # stop distance 5%
    res = simulate(rec, df, None, CFG, entry_mode="open")
    assert res.entry_fill == 50
    # target = 50 + 2*2.5 = 55 -> hit on bar 2 (high 56); half out, still open
    assert res.status == "open"
    assert res.r_multiple > 1.5


def test_open_mode_stop_same_day():
    df, sig = mk_prices([(50, 51, 47, 48)])
    res = simulate(mk_rec(sig), df, None, CFG, entry_mode="open")
    assert res.status == "stopped"
    assert res.r_multiple == pytest.approx(-1 - 0.001 * 50 / 2.5)


# ---- determinism ------------------------------------------------------------


def test_simulate_is_pure_and_deterministic():
    df, sig = mk_prices([FILL, (100, 111, 99, 110), (108, 109, 99.5, 101)])
    before = df.copy()
    a = simulate(mk_rec(sig), df, None, CFG)
    b = simulate(mk_rec(sig), df, None, CFG)
    assert a == b
    pd.testing.assert_frame_equal(df, before)


# ---- update_all (DB layer) --------------------------------------------------


def _db(tmp_path, post, split=None):
    conn = init_db(tmp_path / "t.db")
    df, sig = mk_prices(post)
    for t in ("SPY",):
        d2 = df.copy()
        d2["ticker"] = t
        df = pd.concat([df, d2], ignore_index=True)
    conn.executemany(
        "INSERT INTO prices (ticker,date,open,high,low,close,adj_close,volume) "
        "VALUES (?,?,?,?,?,?,?,1)",
        [(r.ticker, r.date, r.open, r.high, r.low, r.close, r.close) for r in df.itertuples()],
    )
    rec = mk_rec(sig)
    conn.execute(
        "INSERT INTO recommendations (id,source,signal_date,ticker,modules,primary_module,"
        "entry,stop,target,valid_until,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rec.id, "live", sig.isoformat(), TICKER, '["a"]', "a", 100, 95, 110,
         sig.isoformat(), "pending"),
    )
    conn.commit()
    return conn, sig


def _status(conn):
    return dict(conn.execute("SELECT status, current_r, last_updated FROM recommendations").fetchone())


def test_update_all_advances_then_finalises_once(tmp_path):
    conn, sig = _db(tmp_path, [FILL, (100, 102, 99, 101)])
    ds = _dates(WARM + 2)
    update_all(conn, ds[WARM], "live")  # as_of = day of fill
    s = _status(conn)
    assert s["status"] == "open" and s["current_r"] == pytest.approx(0.5 / 5 - COST_R)
    assert conn.execute("SELECT COUNT(*) FROM recommendation_results").fetchone()[0] == 0

    # later the trade stops out
    conn.execute(
        "INSERT INTO prices (ticker,date,open,high,low,close,adj_close,volume) "
        "VALUES ('TEST',?,100,101,94,95,95,1)", (_dates(WARM + 3)[-1].isoformat(),))
    conn.commit()
    update_all(conn, _dates(WARM + 3)[-1], "live")
    s = _status(conn)
    assert s["status"] == "stopped" and s["current_r"] is None
    row = conn.execute("SELECT * FROM recommendation_results").fetchone()
    assert row["final_status"] == "stopped" and row["exit_reason"] == "stop"
    first = dict(row)

    # final results are never touched again, even if prices later change
    conn.execute("UPDATE prices SET low=1, close=1 WHERE ticker='TEST'")
    conn.commit()
    update_all(conn, _dates(WARM + 3)[-1], "live")
    update_all(conn, _dates(WARM + 3)[-1], "live")
    assert dict(conn.execute("SELECT * FROM recommendation_results").fetchone()) == first
    assert conn.execute("SELECT COUNT(*) FROM recommendation_results").fetchone()[0] == 1


def test_update_all_is_idempotent(tmp_path):
    conn, _ = _db(tmp_path, [FILL, (100, 102, 99, 101)])
    as_of = _dates(WARM + 2)[-1]
    update_all(conn, as_of, "live")
    snap = [tuple(r) for r in conn.execute("SELECT * FROM recommendations")]
    update_all(conn, as_of, "live")
    assert [tuple(r) for r in conn.execute("SELECT * FROM recommendations")] == snap


def test_update_all_uses_only_prices_up_to_as_of(tmp_path):
    conn, _ = _db(tmp_path, [FILL, (100, 102, 99, 101), (100, 101, 94, 95)])
    update_all(conn, _dates(WARM + 2)[-1], "live")  # stop-out bar is in the future
    assert _status(conn)["status"] == "open"


def test_update_all_ignores_other_sources(tmp_path):
    conn, _ = _db(tmp_path, [FILL])
    conn.execute("UPDATE recommendations SET source='bt1'")
    conn.commit()
    update_all(conn, _dates(WARM + 1)[-1], "live")
    assert _status(conn)["status"] == "pending"


def test_row_to_rec_roundtrip(tmp_path):
    conn, sig = _db(tmp_path, [FILL])
    rec = row_to_rec(conn.execute("SELECT * FROM recommendations").fetchone())
    assert rec.signal_date == sig and rec.modules == ["a"] and rec.entry == 100
