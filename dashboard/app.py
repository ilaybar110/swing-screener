"""Local Streamlit dashboard.  Run:  streamlit run dashboard/app.py

Reads state/ CSVs (live) and data/research.db (backtests) - never writes to either.
There are no accounts, positions or sizing anywhere: only recommendations, the
hypothetical outcome of each, and statistics about them.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from dashboard import data as dd  # noqa: E402
from src.stats import NOT_ENOUGH_DATA, summary  # noqa: E402

st.set_page_config(page_title="Swing Screener", layout="wide")

VIEWS = [
    "Overview", "Modules vs baseline", "Rank buckets", "Breakdowns", "Equity curve",
    "Excess returns", "All recommendations", "Recommendation detail",
]
STAT_COLS = [
    "group", "count", "win_rate", "avg_r", "median_r", "expectancy", "profit_factor",
    "avg_excess_vs_spy", "avg_excess_vs_sector", "baseline_count", "baseline_avg_r",
    "excess_vs_baseline_r",
]
PCT_COLS = ["win_rate", "avg_excess_vs_spy", "avg_excess_vs_sector"]


# ---------------------------------------------------------------------------
# cached loading (keyed on file mtimes so edits show up after a refresh)
# ---------------------------------------------------------------------------


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _state_stamp() -> float:
    d = dd.state_dir()
    files = list(d.rglob("*.csv")) if d.exists() else []
    return max((_mtime(f) for f in files), default=0.0)


@st.cache_data(show_spinner=False)
def load_source(source: str, stamp: float) -> dict:
    """Everything the views need for one source, as plain frames/dicts."""
    if source == dd.LIVE:
        conn = dd.connect_live()
    else:
        conn = dd.connect_research()
        if conn is None:
            return {}
    try:
        return {
            "recs": dd.recommendations_table(conn, source),
            "stats": summary(conn, source),
            "regime": dd.regime_history(conn),
        }
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def load_runs(stamp: float) -> dict[str, str]:
    conn = dd.connect_research()
    try:
        return dd.source_options(conn)
    finally:
        if conn is not None:
            conn.close()


@st.cache_data(show_spinner="Fetching prices from Yahoo Finance...", ttl=3600)
def cached_prices(ticker: str, start: date, end: date) -> pd.DataFrame:
    return dd.fetch_prices(ticker, start, end)


@st.cache_data(show_spinner=False, ttl=3600)
def cached_split_factor(ticker: str, signal: date) -> float:
    return dd.split_factor_after(ticker, signal)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def stat_frame(stats: dict, section: str) -> pd.DataFrame:
    rows = [{"group": k, **v} for k, v in stats.get(section, {}).items()]
    df = pd.DataFrame(rows, columns=STAT_COLS) if rows else pd.DataFrame(columns=STAT_COLS)
    return df


def show_stats(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No closed trades in this group yet.")
        return
    fmt = {c: "{:.1%}" for c in PCT_COLS}
    fmt.update({c: "{:.2f}" for c in ("avg_r", "median_r", "expectancy", "profit_factor",
                                      "baseline_avg_r", "excess_vs_baseline_r")})
    st.dataframe(df.style.format(fmt, na_rep="-"), width="stretch", hide_index=True)


def warn_small(stats: dict) -> None:
    for module, msg in stats.get("warnings", {}).items():
        st.warning(f"**{module}**: {msg}")


def bar(df: pd.DataFrame, x: str, y: str, title: str) -> alt.Chart:
    """Bar chart of column ``y`` by ``x``; positive green, negative red."""
    d = df.dropna(subset=[y])
    return (
        alt.Chart(d, title=title)
        .mark_bar()
        .encode(x=alt.X(f"{x}:N", sort=None), y=alt.Y(f"{y}:Q"),
                color=alt.condition(alt.datum[y] >= 0, alt.value("#2a9d8f"), alt.value("#e76f51")),
                tooltip=list(d.columns))
    )


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


def view_overview(d: dict) -> None:
    stats, recs, regime = d["stats"], d["recs"], d["regime"]
    c = stats["counts"]
    cols = st.columns(5)
    for col, (label, key) in zip(cols, [("Recommendations", "recommendations"), ("Pending", "pending"),
                                        ("Open", "open"), ("Closed", "closed"), ("Expired", "expired")]):
        col.metric(label, c[key])
    ov = stats["overall"]
    if ov["count"]:
        cols = st.columns(4)
        cols[0].metric("Win rate", f"{ov['win_rate']:.0%}")
        cols[1].metric("Avg R", f"{ov['avg_r']:+.2f}")
        cols[2].metric("Expectancy (R)", f"{ov['expectancy']:+.2f}")
        pf = ov["profit_factor"]
        cols[3].metric("Profit factor", "-" if pf is None else f"{pf:.2f}")

    st.subheader("Open and pending recommendations")
    live = recs[recs["status"].isin(["pending", "open"])] if not recs.empty else recs
    if live.empty:
        st.info("Nothing open or pending.")
    else:
        t = live[["signal_date", "ticker", "modules_str", "rank", "status", "entry", "stop",
                  "target", "valid_until", "current_r", "regime", "earnings_in_window"]]
        st.dataframe(t, width="stretch", hide_index=True)

    st.subheader("Market regime history")
    if regime.empty:
        st.info("No regime history available.")
        return
    latest = regime.iloc[-1]
    st.caption(f"Latest ({latest['date']:%Y-%m-%d}): **{latest['regime']}** - breadth "
               f"{latest['breadth_pct']:.0f}% above SMA50, SPY {latest['spy_close']:.2f} vs SMA200 "
               f"{latest['spy_sma200']:.2f}")
    left, right = st.columns(2)
    spy = regime.melt("date", ["spy_close", "spy_sma200"], "series", "value")
    left.altair_chart(alt.Chart(spy, title="SPY vs 200-day SMA").mark_line().encode(
        x="date:T", y=alt.Y("value:Q", scale=alt.Scale(zero=False)), color="series:N"),
        width="stretch")
    right.altair_chart(alt.Chart(regime, title="Breadth (% of universe above SMA50)").mark_line().encode(
        x="date:T", y="breadth_pct:Q"), width="stretch")
    strip = alt.Chart(regime, title="Regime").mark_rect(height=18).encode(
        x="date:T", color=alt.Color("regime:N", scale=alt.Scale(
            domain=["Favorable", "Caution", "Unfavorable"], range=["#2a9d8f", "#e9c46a", "#e76f51"])))
    st.altair_chart(strip, width="stretch")
    st.dataframe(dd.regime_runs(regime).tail(20).iloc[::-1], width="stretch", hide_index=True)


def view_modules(d: dict) -> None:
    stats = d["stats"]
    warn_small(stats)
    st.subheader("By primary module")
    show_stats(stat_frame(stats, "by_module_primary"))
    st.subheader("By contributing module (recommendation signalled by the module, primary or not)")
    show_stats(stat_frame(stats, "by_module_contains"))
    df = stat_frame(stats, "by_module_primary")
    if not df.empty:
        long = df.melt("group", ["avg_r", "baseline_avg_r"], "series", "avg R")
        st.altair_chart(alt.Chart(long, title="Average R: module vs random-ticker baseline").mark_bar().encode(
            x="group:N", y="avg R:Q", color="series:N", xOffset="series:N"), width="stretch")
    ov = stats["overall"]
    st.caption(f"All modules: avg R {ov['avg_r'] if ov['avg_r'] is not None else '-'} vs baseline "
               f"{ov['baseline_avg_r'] if ov['baseline_avg_r'] is not None else '-'} "
               f"({ov['baseline_count']} baseline trades).")


def view_rank_buckets(d: dict) -> None:
    stats = d["stats"]
    st.write("Does the ranking add value? If it does, top buckets should beat lower ones "
             "and all of them should beat the baseline.")
    df = stat_frame(stats, "by_rank_bucket")
    show_stats(df)
    if df.empty:
        return
    long = df.melt("group", ["avg_r", "baseline_avg_r"], "series", "avg R")
    order = list(df["group"])
    st.altair_chart(alt.Chart(long, title="Average R by rank bucket").mark_bar().encode(
        x=alt.X("group:N", sort=order), y="avg R:Q", color="series:N", xOffset="series:N"),
        width="stretch")
    ok = df.dropna(subset=["avg_r"])
    if len(ok) >= 2 and ok["count"].min() < 30:
        st.warning(f"Some buckets have fewer than 30 closed trades - {NOT_ENOUGH_DATA}.")
    if len(ok) >= 2:
        top, bottom = ok.iloc[0], ok.iloc[-1]
        verdict = "higher" if top["avg_r"] > bottom["avg_r"] else "not higher"
        st.caption(f"Top bucket ({top['group']}) avg R is {verdict} than the lowest populated "
                   f"bucket ({bottom['group']}): {top['avg_r']:+.2f} vs {bottom['avg_r']:+.2f}.")


def view_breakdowns(d: dict) -> None:
    stats = d["stats"]
    tabs = st.tabs(["Regime", "Sector", "Earnings in window"])
    for tab, section, title in zip(
        tabs, ["by_regime", "by_sector", "by_earnings_in_window"],
        ["Regime at signal", "Sector", "Earnings inside the holding window"],
    ):
        with tab:
            df = stat_frame(stats, section)
            show_stats(df)
            if not df.empty:
                st.altair_chart(bar(df[["group", "avg_r", "count"]], "group", "avg_r", f"Average R - {title}"),
                                width="stretch")


def view_equity(d: dict) -> None:
    stats = d["stats"]
    st.write("Equal-risk curve: every trade risks 1R; cumulative R by exit date.")
    curve = pd.DataFrame(stats["equity_curve"])
    if curve.empty:
        st.info("No closed trades yet.")
        return
    curve["date"] = pd.to_datetime(curve["date"])
    st.altair_chart(alt.Chart(curve, title="All modules").mark_line(interpolate="step-after").encode(
        x="date:T", y="cum_r:Q", tooltip=["date:T", "cum_r:Q", "trades:Q"]), width="stretch")
    frames = []
    for m, pts in stats["equity_curve_by_module"].items():
        f = pd.DataFrame(pts)
        f["module"] = m
        frames.append(f)
    if frames:
        by_mod = pd.concat(frames)
        by_mod["date"] = pd.to_datetime(by_mod["date"])
        st.altair_chart(alt.Chart(by_mod, title="By primary module").mark_line(interpolate="step-after").encode(
            x="date:T", y="cum_r:Q", color="module:N", tooltip=["module", "date:T", "cum_r:Q"]),
            width="stretch")
    warn_small(stats)


def view_excess(d: dict) -> None:
    recs, stats = d["recs"], d["stats"]
    closed = recs[recs["r_multiple"].notna()] if not recs.empty else recs
    if closed.empty:
        st.info("No closed trades yet.")
        return
    df = stat_frame(stats, "by_module_primary")
    long = df.melt("group", ["avg_excess_vs_spy", "avg_excess_vs_sector"], "vs", "avg excess return")
    st.altair_chart(alt.Chart(long, title="Average excess return by module (net of costs)").mark_bar().encode(
        x="group:N", y=alt.Y("avg excess return:Q", axis=alt.Axis(format="%")), color="vs:N", xOffset="vs:N"),
        width="stretch")
    pts = closed.dropna(subset=["excess_vs_spy"])
    if not pts.empty:
        st.altair_chart(alt.Chart(pts, title="Per-trade excess return vs SPY").mark_circle(size=60).encode(
            x=alt.X("exit_date:T"), y=alt.Y("excess_vs_spy:Q", axis=alt.Axis(format="%")),
            color="primary_module:N", tooltip=["ticker", "primary_module", "exit_date", "excess_vs_spy",
                                               "excess_vs_sector", "r_multiple"]), width="stretch")
    show_stats(df[["group", "count", "avg_excess_vs_spy", "avg_excess_vs_sector"]])


def filtered_table(recs: pd.DataFrame) -> pd.DataFrame:
    c1, c2, c3, c4 = st.columns([2, 2, 2, 2])
    q = c1.text_input("Search (ticker, rationale, sector, module)")
    mods = c2.multiselect("Module", sorted({m for lst in recs["modules"].dropna() for m in lst}))
    status = c3.multiselect("Status", sorted(recs["status"].dropna().unique()))
    regimes = c4.multiselect("Regime", sorted(recs["regime"].dropna().unique()))
    out = recs
    if q:
        hay = (out["ticker"].fillna("") + " " + out["rationale"].fillna("") + " "
               + out["sector"].fillna("") + " " + out["modules_str"].fillna("") + " "
               + out["industry"].fillna(""))
        out = out[hay.str.contains(q, case=False, regex=False)]
    if mods:
        out = out[out["modules"].map(lambda lst: isinstance(lst, list) and any(m in lst for m in mods))]
    if status:
        out = out[out["status"].isin(status)]
    if regimes:
        out = out[out["regime"].isin(regimes)]
    return out


def view_all(d: dict) -> None:
    recs = d["recs"]
    if recs.empty:
        st.info("No recommendations in this source.")
        return
    out = filtered_table(recs)
    st.caption(f"{len(out)} of {len(recs)} recommendations")
    cols = ["signal_date", "ticker", "modules_str", "rank", "total_score", "status", "result_r",
            "exit_reason", "days_held", "mae_r", "mfe_r", "pct_return", "excess_vs_spy",
            "excess_vs_sector", "entry", "stop", "target", "regime", "sector",
            "earnings_in_window", "guardrail_status"]
    st.dataframe(out[cols], width="stretch", hide_index=True)
    st.download_button("Download CSV", out[cols].to_csv(index=False), "recommendations.csv", "text/csv")


def price_chart(row: pd.Series) -> None:
    signal = date.fromisoformat(str(row["signal_date"])[:10])
    start, end = dd.chart_window(row)
    prices = cached_prices(row["ticker"], start, end)
    factor = cached_split_factor(row["ticker"], signal)
    levels = pd.DataFrame({
        "level": ["entry", "stop", "target"],
        "price": [row["entry"] / factor, row["stop"] / factor, row["target"] / factor],
    })
    base = alt.Chart(prices).encode(x=alt.X("date:T", title=None))
    wick = base.mark_rule().encode(y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="price"), y2="high:Q")
    body = base.mark_bar(size=5).encode(
        y="open:Q", y2="close:Q",
        color=alt.condition("datum.close >= datum.open", alt.value("#2a9d8f"), alt.value("#e76f51")))
    rules = alt.Chart(levels).mark_rule(strokeDash=[6, 4], size=2).encode(
        y="price:Q", color=alt.Color("level:N", scale=alt.Scale(
            domain=["entry", "stop", "target"], range=["#457b9d", "#c1121f", "#2b9348"])),
        tooltip=["level", "price"])
    marks = [pd.DataFrame({"date": [pd.Timestamp(signal)], "what": ["signal"]})]
    for key, lab in (("entry_date", "entry"), ("exit_date", "exit")):
        if isinstance(row.get(key), str) and row[key]:
            marks.append(pd.DataFrame({"date": [pd.Timestamp(row[key])], "what": [lab]}))
    vlines = alt.Chart(pd.concat(marks)).mark_rule(color="gray", opacity=0.5).encode(
        x="date:T", tooltip=["what", "date:T"])
    st.altair_chart((wick + body + rules + vlines).properties(height=420), width="stretch")
    if factor != 1.0:
        st.caption(f"Levels restated for a {factor:g}:1 split after the signal date.")


def view_detail(d: dict) -> None:
    recs = d["recs"]
    if recs.empty:
        st.info("No recommendations in this source.")
        return
    labels = (recs["signal_date"] + "  " + recs["ticker"] + "  [" + recs["status"].fillna("?") + "]").tolist()
    pick = st.selectbox("Recommendation", range(len(recs)), format_func=lambda i: labels[i])
    row = recs.iloc[pick]
    st.header(f"{row['ticker']} - signalled {row['signal_date']}")
    cols = st.columns(5)
    cols[0].metric("Status", row["status"])
    r = row["result_r"]
    cols[1].metric("R" if pd.notna(row["r_multiple"]) else "Current R", "-" if pd.isna(r) else f"{r:+.2f}")
    cols[2].metric("Rank", "-" if pd.isna(row["rank"]) else int(row["rank"]))
    cols[3].metric("Modules", row["modules_str"] or "-")
    cols[4].metric("Regime", row["regime"] or "-")
    cols = st.columns(4)
    cols[0].metric("Entry", f"{row['entry']:.2f}")
    cols[1].metric("Stop", f"{row['stop']:.2f} ({(row['stop'] / row['entry'] - 1):+.1%})")
    cols[2].metric("Target", f"{row['target']:.2f} ({(row['target'] / row['entry'] - 1):+.1%})")
    cols[3].metric("Valid until", row["valid_until"] or "-")

    # fetched from Yahoo only when asked for
    if st.toggle("Show price chart (fetched from Yahoo Finance)", value=False, key=f"chart_{row['id']}"):
        try:
            price_chart(row)
        except Exception as exc:  # noqa: BLE001 - network/ticker problems must not break the page
            st.error(f"Could not load prices for {row['ticker']}: {exc}")

    if pd.notna(row["final_status"]) and pd.notna(row["entry_date"]):
        st.subheader("Outcome")
        out = {
            "entry date": row["entry_date"], "entry fill": row["entry_fill"], "exit date": row["exit_date"],
            "avg exit price": row["avg_exit_price"], "exit reason": row["exit_reason"],
            "days held": row["days_held"], "return (net)": row["pct_return"], "MAE (R)": row["mae_r"],
            "MFE (R)": row["mfe_r"], "SPY return": row["spy_return"], "sector ETF return": row["sector_etf_return"],
            "excess vs SPY": row["excess_vs_spy"], "excess vs sector": row["excess_vs_sector"],
        }
        st.dataframe(pd.DataFrame({"": out}).astype(str), width="stretch")

    st.subheader("Setup")
    st.write(row["rationale"] or "-")
    st.caption(f"Sector: {row['sector'] or '-'} | Industry: {row['industry'] or '-'} | Earnings: "
               f"{row['earnings_date'] or 'none known'}"
               f"{' (inside holding window)' if row['earnings_in_window'] else ''} | Guardrail: "
               f"{row['guardrail_status'] or '-'}")
    if row["guardrail_reasons"]:
        st.write("Guardrail notes: " + "; ".join(row["guardrail_reasons"]))

    st.subheader("Briefing")
    brief = dd.brief_sections(row["llm_brief"])
    if not brief:
        st.info("No briefing was written for this recommendation.")
    else:
        st.write(brief["summary"])
        for title, key in (("Upcoming catalysts", "upcoming_catalysts"), ("Red flags", "red_flags"),
                           ("Recent positive events", "recent_positive_events"), ("Sources", "sources")):
            if brief[key]:
                st.markdown(f"**{title}**")
                for item in brief[key]:
                    st.markdown(f"- {item}")
    with st.expander("Raw details"):
        st.json({"details": row["details"], "valuation_info": row["valuation_info"]})


VIEW_FUNCS = {
    "Overview": view_overview, "Modules vs baseline": view_modules, "Rank buckets": view_rank_buckets,
    "Breakdowns": view_breakdowns, "Equity curve": view_equity, "Excess returns": view_excess,
    "All recommendations": view_all, "Recommendation detail": view_detail,
}


def main() -> None:
    st.sidebar.title("Swing Screener")
    research_stamp = _mtime(dd.research_db_path())
    options = load_runs(research_stamp)
    label = st.sidebar.selectbox("Source", list(options))
    source = options[label]
    view = st.sidebar.radio("View", VIEWS)
    if st.sidebar.button("Reload data"):
        st.cache_data.clear()
        st.rerun()

    stamp = _state_stamp() if source == dd.LIVE else research_stamp
    data = load_source(source, stamp)
    if not data:
        st.error("data/research.db not found.")
        return
    if source != dd.LIVE:
        st.warning("Backtest results: survivorship bias is not corrected - treat as an upper bound "
                   "on real performance.")
    st.title(f"{view} - {label.split(' (')[0]}")
    if data["recs"].empty:
        st.info("This source has no recommendations yet.")
        if view != "Overview":
            return
    VIEW_FUNCS[view](data)


main()
