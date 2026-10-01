"""Daily report builder (docs/PLAN.md section 12).

``build(conn, as_of)`` renders ``reports/YYYY-MM-DD.html`` (self-contained: inline CSS, no
external requests) and ``reports/YYYY-MM-DD.md`` from the live recommendations in the
database, copies them to ``reports/latest.html`` / ``reports/latest.md`` and returns the
two paths. The page is built from a plain-dict context (``build_context``) that both
Jinja templates in ``templates/`` render, so the HTML and Markdown carry the same content.

Sections: header (date, regime banner, data-quality note), today's recommendation cards,
catch-up list (new recommendations from days missed since the previous report), tracking
updates since the previous report, open recommendations, statistics, footer. No share
counts or position sizes appear anywhere.

Interpretation notes (see docs/status/BOT_6.md): "previous report" is the newest
``reports/YYYY-MM-DD.md`` before ``as_of`` (falling back to the previous ``regime_log``
date, then ``as_of - 1 day``); ``in_report`` is the single source of truth for which
recommendations are shown (ranking already applied the regime top-N and industry cap).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader

from src.config import REPO_ROOT, Config, load_config
from src.contracts import Recommendation
from src.utils.logging import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = REPO_ROOT / "templates"
BREADTH_OK_PCT = 50.0  # same threshold src/regime.py uses for "breadth OK"
FOOTER = "Research tool, not financial advice. All candidates are tracked, including ones not shown."
NOT_ENOUGH_DATA = "not enough data yet"
_REPORT_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

_EVENT_LABELS = {
    "triggered": "Triggered (entry filled)",
    "target_hit": "Target hit",
    "stopped": "Stopped out",
    "time_stop": "Time stop",
    "expired": "Expired (never triggered)",
}
_EXIT_REASONS = {
    "stop": "stop",
    "breakeven": "breakeven stop after partial",
    "sma20_exit": "closed below 20-day SMA",
    "time_stop": "20-day time stop",
    "setup_broken": "setup broke before entry",
    "entry_window_elapsed": "entry window elapsed",
}


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _price(v: Any) -> str:
    return f"${float(v):,.2f}" if v is not None else "n/a"


def _pct(v: Any, signed: bool = False, digits: int = 1) -> str:
    if v is None:
        return "n/a"
    return f"{float(v) * 100:+.{digits}f}%" if signed else f"{float(v) * 100:.{digits}f}%"


def _r(v: Any) -> str:
    return f"{float(v):+.2f}R" if v is not None else "n/a"


def _num(v: Any, digits: int = 2) -> str:
    return f"{float(v):.{digits}f}" if v is not None else "n/a"


def _usd_short(v: Any) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def module_label(name: Optional[str]) -> str:
    """'a_momentum_pullback' -> 'A - Momentum pullback'."""
    if not name:
        return "n/a"
    head, _, rest = name.partition("_")
    if len(head) == 1 and rest:
        return f"{head.upper()} - {rest.replace('_', ' ').capitalize()}"
    return name.replace("_", " ")


def _md_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\r", " ").replace("\n", " ")


def _to_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# data gathering
# ---------------------------------------------------------------------------


def _row_to_rec(row: dict[str, Any]) -> Recommendation:
    from src.tracker import row_to_rec

    return row_to_rec(row)


def _previous_report_date(reports_dir: Path, as_of: date) -> Optional[date]:
    found: list[date] = []
    if reports_dir.is_dir():
        for p in reports_dir.iterdir():
            m = _REPORT_FILE.match(p.name)
            if m:
                try:
                    d = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if d < as_of:
                    found.append(d)
    return max(found) if found else None


def _since_date(conn: sqlite3.Connection, reports_dir: Path, as_of: date) -> date:
    prev = _previous_report_date(reports_dir, as_of)
    if prev:
        return prev
    row = conn.execute("SELECT MAX(date) FROM regime_log WHERE date < ?", (as_of.isoformat(),)).fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return as_of - timedelta(days=1)


def _regime(conn: sqlite3.Connection, as_of: date, recs_today: list[Recommendation]) -> dict[str, Any]:
    rows = _query(conn, "SELECT * FROM regime_log WHERE date <= ? ORDER BY date DESC LIMIT 1",
                  (as_of.isoformat(),))
    if not rows:
        label = recs_today[0].regime if recs_today else "Unknown"
        return {"label": label, "known": False, "banner": "Regime data unavailable for this date.",
                "spy_close": None, "spy_sma200": None, "breadth_pct": None, "stale_date": None}
    row = rows[0]
    spy_close, sma, breadth = row["spy_close"], row["spy_sma200"], row["breadth_pct"]
    parts = []
    if spy_close is not None and sma is not None:
        rel = "above" if spy_close > sma else "below"
        parts.append(f"SPY {_price(spy_close)} is {rel} its 200-day average ({_price(sma)})")
    if breadth is not None:
        parts.append(f"{breadth:.0f}% of the universe is above its 50-day average")
    stale = row["date"] if row["date"] != as_of.isoformat() else None
    banner = "; ".join(parts) + ("." if parts else "")
    if stale:
        banner += f" (regime data from {stale})"
    return {
        "label": row["regime"] or "Unknown", "known": True, "banner": banner,
        "spy_close": spy_close, "spy_sma200": sma, "breadth_pct": breadth,
        "spy_ok": (spy_close > sma) if spy_close is not None and sma is not None else None,
        "breadth_ok": (breadth >= BREADTH_OK_PCT) if breadth is not None else None,
        "stale_date": stale,
    }


def _data_quality(conn: sqlite3.Connection, as_of: date) -> list[str]:
    """Notes about price-data problems today, read from job_log rows whose job starts
    with 'prices'. The message is either JSON ({"missing": [...], "nasdaq_filled": [...]},
    the shape of update_prices' report) or free text (shown when the job did not succeed)."""
    notes: list[str] = []
    try:
        rows = _query(conn, "SELECT job, status, message FROM job_log WHERE trading_date = ? "
                            "AND job LIKE 'prices%' ORDER BY started_at", (as_of.isoformat(),))
    except sqlite3.Error:
        return notes
    missing: list[str] = []
    filled: list[str] = []
    for row in rows:
        msg = row["message"] or ""
        try:
            data = json.loads(msg)
        except ValueError:
            data = None
        if isinstance(data, dict):
            missing += [str(t) for t in data.get("missing", [])]
            filled += [str(t) for t in data.get("nasdaq_filled", [])]
        elif msg and (row["status"] or "").lower() not in ("ok", "done", "success"):
            notes.append(f"Price update ({row['job']}): {msg}")

    def fmt(names: list[str]) -> str:
        names = sorted(set(names))
        return ", ".join(names[:12]) + (f" and {len(names) - 12} more" if len(names) > 12 else "")

    if missing:
        notes.insert(0, f"Prices were missing for {len(set(missing))} ticker(s) ({fmt(missing)}); "
                        "they may be absent from today's scan and tracking.")
    if filled:
        notes.insert(1 if missing else 0,
                     f"Prices for {len(set(filled))} ticker(s) ({fmt(filled)}) were filled from Nasdaq "
                     "instead of Yahoo Finance.")
    return notes


def _insider_buys(conn: sqlite3.Connection, ticker: str, as_of: date) -> list[str]:
    start = (as_of - timedelta(days=30)).isoformat()
    try:
        rows = _query(conn, "SELECT insider_name, officer_title, is_director, filed_date, value "
                            "FROM insider_trades WHERE ticker = ? AND trans_code = 'P' "
                            "AND filed_date >= ? AND filed_date <= ? ORDER BY filed_date DESC, row_num LIMIT 6",
                      (ticker, start, as_of.isoformat()))
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        role = r["officer_title"] or ("Director" if r["is_director"] else "")
        who = f"{r['insider_name']} ({role})" if role else str(r["insider_name"])
        out.append(f"{who} bought {_usd_short(r['value'])} (filed {r['filed_date']})")
    return out


def _company(conn: sqlite3.Connection, ticker: str) -> str:
    try:
        row = conn.execute("SELECT name FROM tickers WHERE ticker = ?", (ticker,)).fetchone()
    except sqlite3.Error:
        return ""
    return (row[0] or "") if row else ""


def _track_text(stats: Optional[dict[str, Any]], module: Optional[str]) -> str:
    if not stats or not module:
        return NOT_ENOUGH_DATA
    min_closed = stats.get("min_closed_for_confidence", 30)
    m = (stats.get("by_module_primary") or {}).get(module) or {}
    n = m.get("count") or 0
    if n < min_closed:
        return f"{NOT_ENOUGH_DATA} ({n}/{min_closed} closed live results)"
    return (f"{n} closed - win rate {_pct(m.get('win_rate'), digits=0)}, "
            f"avg {_r(m.get('avg_r'))}, expectancy {_r(m.get('expectancy'))}")


def _valid_brief(brief: Any) -> Optional[dict[str, Any]]:
    """Defensive re-check at render time (llm_brief could be hand-edited); returns None
    when the stored brief is unusable so the section is simply omitted."""
    from src.llm.brief_io import validate_brief

    if not isinstance(brief, dict) or validate_brief(brief):
        return None
    return {
        "summary": brief["summary"],
        "catalysts": [{"event": c["event"], "date": c.get("date")} for c in brief["upcoming_catalysts"]],
        "red_flags": list(brief["red_flags"]),
        "positives": list(brief["recent_positive_events"]),
        "sources": list(brief["sources"]),
    }


def _card(conn: sqlite3.Connection, rec: Recommendation, as_of: date, stats: Optional[dict[str, Any]],
          config: Config) -> dict[str, Any]:
    d = rec.details or {}
    stop_pct = d.get("stop_pct", (rec.entry - rec.stop) / rec.entry if rec.entry else None)
    target_pct = d.get("target_pct", (rec.target - rec.entry) / rec.entry if rec.entry else None)
    vi = rec.valuation_info or {}
    pct = next((vi[k] for k in ("ev_fcf_sector_pct", "ev_fcf_pct", "ev_fcf_percentile") if vi.get(k) is not None), None)
    if pct is not None and 0 <= float(pct) <= 1:
        pct = float(pct) * 100
    ev_fcf = None
    if pct is not None:
        ev_fcf = f"{float(pct):.0f}th percentile vs sector"
        if vi.get("ev_fcf") is not None:
            ev_fcf += f" (EV/FCF {float(vi['ev_fcf']):.1f})"
    window = config.ranking.earnings_window_trading_days
    earnings = None
    if rec.earnings_date:
        earnings = {"date": rec.earnings_date.isoformat(), "in_window": bool(rec.earnings_in_window),
                    "warning": (f"Earnings fall inside the ~{window}-trading-day holding window"
                                if rec.earnings_in_window else None)}
    status = (rec.guardrail_status or "unknown").lower()
    return {
        "id": rec.id,
        "ticker": rec.ticker,
        "company": _company(conn, rec.ticker),
        "rank": rec.rank,
        "sector": rec.sector,
        "industry": rec.industry,
        "modules": [module_label(m) for m in rec.modules],
        "primary_module": module_label(rec.primary_module),
        "rationale": rec.rationale,
        "entry": _price(rec.entry),
        "stop": _price(rec.stop),
        "stop_pct": _pct(-abs(stop_pct), digits=1) if stop_pct is not None else "n/a",
        "target": _price(rec.target),
        "target_pct": _pct(target_pct, signed=True) if target_pct is not None else "n/a",
        "target_r": d.get("target_r_multiple", config.trade_plan.target_r_multiple),
        "time_stop": f"close of trading day {config.trade_plan.time_stop_trading_days} after entry",
        "valid_until": rec.valid_until.isoformat() if rec.valid_until else "n/a",
        "guardrail": status,
        "guardrail_reasons": list(rec.guardrail_reasons or []),
        "ev_fcf": ev_fcf,
        "earnings": earnings,
        "insiders": _insider_buys(conn, rec.ticker, as_of),
        "brief": _valid_brief(rec.llm_brief),
        "track": _track_text(stats, rec.primary_module),
        "score": _num(rec.total_score, 1),
    }


def _short_row(conn: sqlite3.Connection, rec: Recommendation) -> dict[str, Any]:
    return {
        "date": rec.signal_date.isoformat(),
        "ticker": rec.ticker,
        "modules": ", ".join(module_label(m) for m in rec.modules),
        "entry": _price(rec.entry), "stop": _price(rec.stop), "target": _price(rec.target),
        "status": rec.status or "n/a",
        "current_r": _r(rec.current_r) if rec.status == "open" and rec.current_r is not None else "-",
        "valid_until": rec.valid_until.isoformat() if rec.valid_until else "n/a",
        "regime": rec.regime,
    }


def _tracking_updates(conn: sqlite3.Connection, as_of: date, since: date, config: Config) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    rows = _query(
        conn,
        "SELECT x.*, r.ticker, r.signal_date, COALESCE(x.exit_date, r.last_updated) AS event_date "
        "FROM recommendation_results x JOIN recommendations r ON r.id = x.rec_id "
        "WHERE r.source = 'live' AND COALESCE(x.exit_date, r.last_updated) > ? "
        "AND COALESCE(x.exit_date, r.last_updated) <= ? ORDER BY event_date, r.ticker",
        (since.isoformat(), as_of.isoformat()),
    )
    for x in rows:
        status = x["final_status"] or "expired"
        reason = _EXIT_REASONS.get(x["exit_reason"] or "", x["exit_reason"] or "")
        events.append({
            "ticker": x["ticker"], "event": status, "label": _EVENT_LABELS.get(status, status),
            "date": str(x["event_date"])[:10], "signal_date": x["signal_date"],
            "r": _r(x["r_multiple"]) if x["r_multiple"] is not None else "-",
            "pct": _pct(x["pct_return"], signed=True) if x["pct_return"] is not None else "-",
            "note": reason,
        })

    # triggered since the previous report but still open: entry date is not stored, so
    # re-simulate the open ones (cheap: only open positions)
    try:
        from src.tracker import PriceCache, simulate

        open_rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND status = 'open' "
                                 "AND signal_date <= ? ORDER BY signal_date, id", (as_of.isoformat(),))
        if open_rows:
            first = min(date.fromisoformat(r["signal_date"]) for r in open_rows)
            cache = PriceCache(conn, first - timedelta(days=60), as_of)
            for row in open_rows:
                rec = _row_to_rec(row)
                frame = cache.frame_for([rec.ticker], rec.signal_date)
                res = simulate(rec, frame, None, config)
                if res.entry_date and since < res.entry_date <= as_of:
                    events.append({
                        "ticker": rec.ticker, "event": "triggered", "label": _EVENT_LABELS["triggered"],
                        "date": res.entry_date.isoformat(), "signal_date": rec.signal_date.isoformat(),
                        "r": _r(res.r_multiple) if res.r_multiple is not None else "-", "pct": "-",
                        "note": f"filled at {_price(res.entry_fill)}",
                    })
    except Exception as exc:  # noqa: BLE001 -- the report must not fail on this
        log.warning("report: could not determine newly triggered positions: %s", exc)
    events.sort(key=lambda e: (e["date"], e["ticker"]))
    return events


def _fallback_stats(conn: sqlite3.Connection, config: Config) -> Optional[dict[str, Any]]:
    """Counts-only stand-in for stats.summary() when that call fails: no closed-trade
    metrics, every module flagged 'not enough data yet'."""
    try:
        min_closed = config.ranking.track_record_min_closed_trades
        rows = _query(conn, "SELECT r.primary_module, r.status, x.final_status, x.entry_date "
                            "FROM recommendations r LEFT JOIN recommendation_results x ON x.rec_id = r.id "
                            "WHERE r.source = 'live'")
        closed = [r for r in rows if r["final_status"] and r["entry_date"]]
        counts = {
            "recommendations": len(rows),
            "open": sum(r["status"] == "open" for r in rows),
            "pending": sum(r["status"] == "pending" for r in rows),
            "closed": len(closed),
            "expired": sum(r["final_status"] == "expired" for r in rows),
        }
        modules = sorted({r["primary_module"] for r in rows if r["primary_module"]})
        return {"min_closed_for_confidence": min_closed, "counts": counts, "overall": {"count": len(closed)},
                "by_module_primary": {}, "by_rank_bucket": {},
                "warnings": {m: f"{NOT_ENOUGH_DATA} (0/{min_closed} closed)" for m in modules}}
    except sqlite3.Error:
        return None


def _stats_section(stats: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not stats:
        return None
    min_closed = stats.get("min_closed_for_confidence", 30)
    warnings = stats.get("warnings") or {}

    def row(name: str, m: dict[str, Any], note: str = "") -> dict[str, Any]:
        return {
            "name": name, "count": m.get("count", 0),
            "win_rate": _pct(m.get("win_rate"), digits=0), "avg_r": _r(m.get("avg_r")),
            "expectancy": _r(m.get("expectancy")), "profit_factor": _num(m.get("profit_factor")),
            "vs_spy": _pct(m.get("avg_excess_vs_spy"), signed=True),
            "vs_sector": _pct(m.get("avg_excess_vs_sector"), signed=True),
            "baseline_r": _r(m.get("baseline_avg_r")),
            "vs_baseline": _r(m.get("excess_vs_baseline_r")), "note": note,
        }

    modules = [row(module_label(k), v, warnings.get(k, "") if (v.get("count") or 0) < min_closed else "")
               for k, v in sorted((stats.get("by_module_primary") or {}).items())]
    for k, msg in sorted(warnings.items()):  # modules with recommendations but no closed trades yet
        if k not in (stats.get("by_module_primary") or {}):
            modules.append(row(module_label(k), {"count": 0}, msg))
    buckets = [row(k, v) for k, v in (stats.get("by_rank_bucket") or {}).items()]
    return {
        "counts": stats.get("counts") or {},
        "overall": row("All modules", stats.get("overall") or {}),
        "modules": modules,
        "rank_buckets": buckets,
        "min_closed": min_closed,
        "excess_spy": _pct((stats.get("overall") or {}).get("avg_excess_vs_spy"), signed=True),
        "excess_sector": _pct((stats.get("overall") or {}).get("avg_excess_vs_sector"), signed=True),
    }


def build_context(conn: sqlite3.Connection, as_of: date, config: Optional[Config] = None,
                  since: Optional[date] = None) -> dict[str, Any]:
    """Everything the templates need, as plain dicts/strings."""
    config = config or load_config()
    reports_dir = Path(config.paths.reports_dir)
    since = since or _since_date(conn, reports_dir, as_of)

    try:
        from src import stats as stats_mod

        stats: Optional[dict[str, Any]] = stats_mod.summary(conn, "live")
    except Exception as exc:  # noqa: BLE001
        # e.g. stats.summary raises KeyError('id') while recommendations exist but none
        # has closed yet (docs/CHANGE_REQUESTS.md, 2026-10-01 Bot 6)
        log.warning("report: stats.summary failed (%s); using minimal statistics", exc)
        stats = _fallback_stats(conn, config)

    today_rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND in_report = 1 "
                              "AND signal_date = ? ORDER BY (rank IS NULL), rank, ticker", (as_of.isoformat(),))
    today = [_row_to_rec(r) for r in today_rows]
    regime = _regime(conn, as_of, today)
    suppressed = regime["label"] == "Unfavorable"
    cards = [] if suppressed else [_card(conn, r, as_of, stats, config) for r in today]

    missed_rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND in_report = 1 "
                               "AND signal_date > ? AND signal_date < ? ORDER BY signal_date, (rank IS NULL), rank, ticker",
                         (since.isoformat(), as_of.isoformat()))
    catch_up = [_short_row(conn, _row_to_rec(r)) for r in missed_rows]

    open_rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND status IN ('open', 'pending') "
                             "AND signal_date < ? ORDER BY (status = 'open') DESC, signal_date, ticker",
                       (as_of.isoformat(),))
    open_recs = [_short_row(conn, _row_to_rec(r)) for r in open_rows]

    updates = _tracking_updates(conn, as_of, since, config)

    return {
        "title": f"Swing Screener - {as_of.isoformat()}",
        "as_of": as_of.isoformat(),
        "since": since.isoformat(),
        "regime": regime,
        "suppressed": suppressed,
        "no_recs_today": not cards and not suppressed,
        "data_quality": _data_quality(conn, as_of),
        "cards": cards,
        "n_new": len(today) if not suppressed else 0,
        "catch_up": catch_up,
        "updates": updates,
        "open_recs": open_recs,
        "stats": _stats_section(stats),
        "footer": FOOTER,
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _env(autoescape: bool) -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=autoescape,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    env.filters["md"] = _md_escape
    return env


def render_html(ctx: dict[str, Any]) -> str:
    """Render the self-contained HTML report."""
    return _env(True).get_template("report.html.j2").render(**ctx)


def _tidy_markdown(text: str) -> str:
    """Guarantee a blank line between Markdown blocks (the template strips block-tag
    lines, which would otherwise glue paragraphs/headings together). Consecutive table
    rows and list items stay together."""
    def kind(line: str) -> str:
        if line.startswith("#"):
            return "h"
        if line.startswith("|"):
            return "t"
        if line.startswith("- "):
            return "l"
        return "p"

    out: list[str] = []
    prev: Optional[str] = None
    for line in text.splitlines():
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            prev = None
            continue
        k = kind(line)
        if prev is not None and not (k == prev and k in ("t", "l")):
            out.append("")
        out.append(line)
        prev = k
    return "\n".join(out).strip("\n") + "\n"


def render_markdown(ctx: dict[str, Any]) -> str:
    """Render the Markdown report (same content as the HTML)."""
    return _tidy_markdown(_env(False).get_template("report.md.j2").render(**ctx))


def summary_text(ctx: dict[str, Any], max_recs: int = 3, max_updates: int = 5) -> str:
    """Short plain-text digest for Telegram: regime, number of new recommendations, top
    tickers with entry/stop/target and notable tracking updates."""
    reg = ctx["regime"]
    lines = [f"Swing Screener {ctx['as_of']}", f"Regime: {reg['label']}"]
    if reg.get("banner"):
        lines[-1] += f" - {reg['banner']}"
    if ctx["suppressed"]:
        lines.append("No new recommendations (Unfavorable regime).")
    else:
        lines.append(f"New recommendations: {ctx['n_new']}")
        for c in ctx["cards"][:max_recs]:
            lines.append(f"{c['rank'] or '-'}. {c['ticker']}  entry {c['entry']} | stop {c['stop']} "
                         f"({c['stop_pct']}) | target {c['target']} ({c['target_pct']})")
    if ctx["catch_up"]:
        lines.append(f"Catch-up: {len(ctx['catch_up'])} recommendation(s) from missed days.")
    if ctx["updates"]:
        lines.append("Updates: " + "; ".join(
            f"{u['ticker']} {u['label'].lower()}" + (f" ({u['r']})" if u["r"] not in ("-", "n/a") else "")
            for u in ctx["updates"][:max_updates]))
        if len(ctx["updates"]) > max_updates:
            lines.append(f"...and {len(ctx['updates']) - max_updates} more in the report.")
    lines.append(FOOTER)
    return "\n".join(lines)


def build(conn: sqlite3.Connection, as_of: date, *, config: Optional[Config] = None,
          since: Optional[date] = None) -> tuple[str, str]:
    """Build the report for ``as_of`` and return ``(html_path, md_path)``.

    Also copies both files to ``reports/latest.html`` / ``reports/latest.md``. ``since``
    overrides the previous-report date used for "updates since the last report" and the
    catch-up section."""
    config = config or load_config()
    ctx = build_context(conn, as_of, config, since)
    reports_dir = Path(config.paths.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_path = reports_dir / f"{as_of.isoformat()}.html"
    md_path = reports_dir / f"{as_of.isoformat()}.md"
    html_path.write_text(render_html(ctx), encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(ctx), encoding="utf-8", newline="\n")
    shutil.copyfile(html_path, reports_dir / "latest.html")
    shutil.copyfile(md_path, reports_dir / "latest.md")
    log.info("report built for %s: %d new, %d catch-up, %d updates, %d open (%s)",
             as_of, ctx["n_new"], len(ctx["catch_up"]), len(ctx["updates"]), len(ctx["open_recs"]), html_path)
    return str(html_path), str(md_path)


def build_summary(conn: sqlite3.Connection, as_of: date, *, config: Optional[Config] = None,
                  since: Optional[date] = None) -> str:
    """Telegram digest for ``as_of`` (convenience wrapper over build_context + summary_text)."""
    return summary_text(build_context(conn, as_of, config, since))
