"""Streamlit rendering of the recommendation chart + "Why this setup" panel.

``render(row, stamp, is_live)`` is the only entry point; the detail page calls it in place
of a plain price chart.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from dashboard import data as dd
from dashboard import rec_chart, rec_data, rec_explain
from src.config import load_config
from src.data_access import DataAccess

CONFIG = load_config()


@st.cache_data(show_spinner="Fetching prices from Yahoo Finance...", ttl=3600)
def _prices(ticker: str, start: date, end: date) -> pd.DataFrame:
    return rec_data.fetch_ohlcv(ticker, start, end)


@st.cache_data(show_spinner=False, ttl=3600)
def _split_factor(ticker: str, signal: date) -> float:
    return dd.split_factor_after(ticker, signal)


@st.cache_data(show_spinner="Rebuilding annotations from price data up to the signal date...")
def _annotations(rec_id: str, module: str, stamp: float, is_live: bool, _row: pd.Series,
                 _prices_df: pd.DataFrame, _spy: pd.DataFrame, factor: float):
    """Cached per (recommendation, module, state files, split factor)."""
    conn = dd.connect_live() if is_live else dd.connect_research()
    if conn is None:
        return rec_data.module_details(_row, module), "unavailable"
    try:
        return rec_data.ensure_annotations(module, _row, _prices_df, _spy, DataAccess(conn), CONFIG,
                                           force_recompute=factor != 1.0)
    finally:
        conn.close()


def _mark(ok) -> str:
    return {True: "✅", False: "❌", None: "➖"}[ok]


def _opt(v):
    """None for NaN/empty so the chart code can test truthiness."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else v


def why_panel(row: pd.Series, annots: dict[str, dict], status: dict[str, str]) -> None:
    st.subheader("Why this setup")
    det = row["details"] if isinstance(row["details"], dict) else {}
    for module in row["modules"] or []:
        by = (det.get("by_module") or {}).get(module) or {}
        label = rec_chart.MODULE_LABEL.get(module, module)
        st.markdown(f"**{label}**" + (" (primary)" if module == row["primary_module"] else "")
                    + f" - setup score {by.get('setup_score', '-')}")
        d = annots.get(module)
        if not d:
            st.caption("Rule values are unavailable for this module (annotations could not be rebuilt).")
            continue
        checks = rec_explain.module_checks(module, d, by.get("entry", row["entry"]), by.get("stop", row["stop"]), CONFIG)
        st.markdown("\n".join(f"- {_mark(c.ok)} {c.rule}: **{c.actual}** (needs {c.threshold})" for c in checks))
        comps = rec_explain.score_components(module, d, CONFIG)
        if comps:
            with st.expander(f"Setup score components ({sum(p for _, _, p in comps):.1f} of 100)"):
                for lab, how, pts in comps:
                    st.write(f"{lab}: {how} -> {pts:.1f} pts")
        if status.get(module) == "recomputed":
            st.caption("Annotations recomputed from price data up to the signal date "
                       "(saved before annotations were stored).")
    st.markdown("**Guardrail**")
    reasons = row["guardrail_reasons"] if isinstance(row["guardrail_reasons"], list) else []
    st.write(f"{row['guardrail_status'] or '-'}" + (": " + "; ".join(reasons) if reasons else ""))
    sc = det.get("score_components")
    if sc:
        st.markdown("**Ranking components** (percentiles among that day's candidates)")
        rank = "-" if pd.isna(row["rank"]) else int(row["rank"])
        st.write(f"Setup percentile {sc.get('setup_percentile', '-')} (weight 45%), RS percentile "
                 f"{sc.get('rs_percentile_in_candidates', '-')} (35%), overlap {sc.get('overlap_score', '-')} (20%), "
                 f"track record {sc.get('track_score', '-')} (weight {sc.get('track_weight', 0)}) "
                 f"-> total {row['total_score']:.1f}, rank {rank}")


def render(row: pd.Series, stamp: float, is_live: bool = True) -> None:
    signal = date.fromisoformat(str(row["signal_date"])[:10])
    today = date.today()
    try:
        start = signal - timedelta(days=rec_data.HISTORY_DAYS)
        prices = _prices(row["ticker"], start, today)
        spy = _prices("SPY", start, today)
    except Exception as exc:  # noqa: BLE001 - network/ticker problems must not break the page
        st.warning(f"Could not load prices for {row['ticker']}: {exc}")
        return
    factor = _split_factor(row["ticker"], signal)
    modules = [m for m in (row["modules"] or []) if m in rec_data.MARKER_FIELD]
    annots, status = {}, {}
    for m in modules:
        annots[m], status[m] = _annotations(row["id"], m, stamp, is_live, row, prices, spy, factor)

    chart_col, why_col = st.columns([3, 1.35], gap="large")
    with chart_col:
        enabled = [m for m in modules if st.toggle(
            f"Show {rec_chart.MODULE_LABEL.get(m, m)} annotations", value=True, key=f"mod_{row['id']}_{m}")
            and annots.get(m)]
        for m in modules:
            if status[m] == "unavailable":
                st.warning(f"{rec_chart.MODULE_LABEL.get(m, m)}: annotations could not be rebuilt "
                           "(the module does not reproduce this signal on the fetched prices).")
        try:
            outcome = rec_data.outcome_events(row, prices, factor, CONFIG)
        except Exception:  # noqa: BLE001 - outcome markers are a nicety
            outcome = None
        rec = {
            "entry": row["entry"] / factor, "stop": row["stop"] / factor, "target": row["target"] / factor,
            "signal_date": signal, "valid_until": _opt(row["valid_until"]),
            "earnings_date": _opt(row["earnings_date"]),
        }
        fig = rec_chart.build_figure(rec, prices, spy, annots, enabled, outcome)
        st.plotly_chart(fig, width="stretch", key=f"fig_{row['id']}",
                        config={"displaylogo": False, "scrollZoom": True})
        notes = []
        if factor != 1.0:
            notes.append(f"Levels restated for a {factor:g}:1 split after the signal date; annotations were rebuilt "
                         "on split-adjusted prices.")
        if "recomputed" in status.values():
            notes.append("Annotations recomputed: this recommendation was saved before annotations were stored, so "
                         "they were rebuilt with the module's own code on prices up to the signal date only.")
        for n in notes:
            st.caption(n)
    with why_col:
        why_panel(row, annots, status)
