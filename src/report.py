"""Daily report builder (docs/PLAN.md section 12).

``build(conn, as_of)`` renders ``reports/YYYY-MM-DD.html`` (self-contained: inline CSS and
SVG, no external requests) and ``reports/YYYY-MM-DD.md`` from the live recommendations in
the database, copies them to ``reports/latest.html`` / ``reports/latest.md`` and returns the
two paths. The page is built from a plain-dict context (``build_context``) that both
Jinja templates in ``templates/`` render, so the HTML and Markdown carry the same content.

Sections: header (date, regime banner, track-record scoreboard, data-quality note),
today's recommendation cards (trade strip + sparkline first), catch-up list (new
recommendations from days missed since the previous report), tracking updates since the
previous report, open-position status board (pending vs open, "needs attention"),
statistics, footer. No share counts or position sizes appear anywhere.

Interpretation notes (see docs/status/BOT_6.md): "previous report" is the newest
``reports/YYYY-MM-DD.md`` before ``as_of`` (falling back to the previous ``regime_log``
date, then ``as_of - 1 day``); ``in_report`` is the single source of truth for which
recommendations are shown (ranking already applied the regime top-N and industry cap).

Prices: the trade strip / sparkline on each card and the status board read the
``prices`` table of ``conn`` (the daily run's DB holds them). A ticker without price rows
simply gets no strip, so the report never depends on them.

Telegram: ``summary_text`` returns a :class:`TelegramDigest` -- a plain-text ``str`` (what
``run_daily`` prints) that also carries ``.messages``: the parse_mode=HTML messages that
``notify.telegram.send_report`` sends, each below Telegram's 4096-character limit.
"""

from __future__ import annotations

import html as _html
import json
import re
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import numpy as np
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from src.config import REPO_ROOT, Config, load_config
from src.contracts import Recommendation
from src.utils.logging import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = REPO_ROOT / "templates"
FOOTER = "Research tool, not financial advice. All candidates are tracked, including ones not shown."
NOT_ENOUGH_DATA = "not enough data yet"
PRELIMINARY = "preliminary"
GUARDRAIL_LABELS = {"pass": "Pass", "pass_partial": "Pass (partial)", "fail": "Fail", "unknown": "Unknown"}
SPARK_SESSIONS = 60          # closes drawn in each card's sparkline
ATTENTION_R = 0.3            # "needs attention": within this many R of the stop or the target ...
ATTENTION_SESSIONS = 2       # ... or this many sessions (or fewer) before the time stop
TELEGRAM_SOFT_LIMIT = 3900   # characters packed into one Telegram message (hard limit 4096)
_REPORT_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CO_SUFFIX = re.compile(
    r"(?<=\S)[\s,-]+(?:(?:Class|Series)\s+[A-Z]\s+)?(?:Common\s+Stock|Ordinary\s+Shares|Common\s+Shares|"
    r"Subordinate\s+Voting\s+Shares|American\s+Depositary\s+Shares|Depositary\s+Shares|Capital\s+Stock)\b.*$",
    re.I)

# Single source of truth for module names: report, Telegram and dashboard all use it.
MODULE_NAMES: dict[str, tuple[str, str]] = {
    "momentum_pullback": ("A", "Momentum pullback"),
    "earnings_gap_drift": ("B", "Earnings gap drift"),
    "insider_cluster": ("C", "Insider cluster"),
    "base_breakout": ("D", "Base breakout"),
}

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


def sign_class(v: Any, eps: float = 0.005) -> str:
    """CSS class for a signed number: 'pos', 'neg' or 'zero' ('' when missing)."""
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return "pos" if f > eps else "neg" if f < -eps else "zero"


def _usd_short(v: Any) -> str:
    if v is None:
        return "n/a"
    v = float(v)
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:.0f}"


def _module_key(name: str) -> tuple[Optional[str], str]:
    """('a', 'momentum_pullback') for 'a_momentum_pullback'; (None, name) otherwise."""
    m = re.match(r"^([a-z])_(.+)$", name)
    return (m.group(1), m.group(2)) if m else (None, name)


def module_letter(name: Optional[str]) -> str:
    """'momentum_pullback' / 'a_momentum_pullback' -> 'A' ('' when unknown)."""
    if not name:
        return ""
    prefix, key = _module_key(name)
    if key in MODULE_NAMES:
        return MODULE_NAMES[key][0]
    return prefix.upper() if prefix else ""


def module_label(name: Optional[str]) -> str:
    """'momentum_pullback' or 'a_momentum_pullback' -> 'A - Momentum pullback'."""
    if not name:
        return "n/a"
    prefix, key = _module_key(name)
    if key in MODULE_NAMES:
        letter, title = MODULE_NAMES[key]
        return f"{letter} - {title}"
    if prefix:
        return f"{prefix.upper()} - {key.replace('_', ' ').capitalize()}"
    return name.replace("_", " ").capitalize()


def module_letter_from_label(label: str) -> str:
    """'A - Momentum pullback' -> 'A' (the label itself when it has no letter)."""
    head, sep, _ = label.partition(" - ")
    return head if sep and len(head) == 1 else label


def short_company(name: Optional[str]) -> str:
    """'Amplitude Inc. Class A Common Stock' -> 'Amplitude Inc.'"""
    if not name:
        return ""
    return _CO_SUFFIX.sub("", str(name)).strip() or str(name)


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


def _regime(conn: sqlite3.Connection, as_of: date, recs_today: list[Recommendation],
            breadth_threshold_pct: float) -> dict[str, Any]:
    rows = _query(conn, "SELECT * FROM regime_log WHERE date <= ? ORDER BY date DESC LIMIT 1",
                  (as_of.isoformat(),))
    if not rows:
        label = recs_today[0].regime if recs_today else "Unknown"
        return {"label": label, "known": False, "banner": "Regime data unavailable for this date.",
                "short": "regime data unavailable",
                "spy_close": None, "spy_sma200": None, "breadth_pct": None, "stale_date": None}
    row = rows[0]
    spy_close, sma, breadth = row["spy_close"], row["spy_sma200"], row["breadth_pct"]
    parts, short = [], []
    if spy_close is not None and sma is not None:
        rel = "above" if spy_close > sma else "below"
        parts.append(f"SPY {_price(spy_close)} is {rel} its 200-day average ({_price(sma)})")
        short.append(f"SPY {rel} 200d avg")
    if breadth is not None:
        parts.append(f"{breadth:.0f}% of the universe is above its 50-day average")
        ok = breadth >= breadth_threshold_pct
        short.append(f"breadth {breadth:.0f}% ({'>=' if ok else '<'}{breadth_threshold_pct:.0f}%)")
    stale = row["date"] if row["date"] != as_of.isoformat() else None
    banner = "; ".join(parts) + ("." if parts else "")
    if stale:
        banner += f" (regime data from {stale})"
        short.append(f"data from {stale}")
    return {
        "label": row["regime"] or "Unknown", "known": True, "banner": banner, "short": ", ".join(short),
        "spy_close": spy_close, "spy_sma200": sma, "breadth_pct": breadth,
        "spy_ok": (spy_close > sma) if spy_close is not None and sma is not None else None,
        "breadth_ok": (breadth >= breadth_threshold_pct) if breadth is not None else None,
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
    return short_company(row[0] or "") if row else ""


def _bars(conn: sqlite3.Connection, ticker: str, as_of: date, n: int) -> list[tuple[str, float, float, float]]:
    """Last ``n`` (date, high, low, close) bars on or before ``as_of``, oldest first."""
    try:
        rows = conn.execute("SELECT date, high, low, close FROM prices WHERE ticker = ? AND date <= ? "
                            "AND close IS NOT NULL ORDER BY date DESC LIMIT ?",
                            (ticker, as_of.isoformat(), n)).fetchall()
    except sqlite3.Error:
        return []
    return [(str(r[0])[:10], float(r[1] if r[1] is not None else r[3]), float(r[2] if r[2] is not None else r[3]),
             float(r[3])) for r in reversed(rows)]


def _split_after(conn: sqlite3.Connection, ticker: str, d: date) -> bool:
    try:
        return conn.execute("SELECT 1 FROM splits WHERE ticker = ? AND date > ? LIMIT 1",
                            (ticker, d.isoformat())).fetchone() is not None
    except sqlite3.Error:
        return False


def _metric_ci(m: dict[str, Any]) -> Optional[str]:
    """Optional confidence interval of avg R (``avg_r_ci``: [lo, hi])."""
    return _ci_text(m.get("avg_r_ci"))


def _evidence(m: dict[str, Any]) -> Optional[str]:
    ev = m.get("evidence")
    if isinstance(ev, str) and ev:
        return ev
    if isinstance(ev, dict):
        label = ev.get("label") or ev.get("level") or ev.get("verdict")
        return str(label) if label else None
    return None


def _track(stats: Optional[dict[str, Any]], module: Optional[str]) -> dict[str, Any]:
    """Raw live record of a module, tagged 'preliminary' below the confidence threshold."""
    min_closed = (stats or {}).get("min_closed_for_confidence", 30)
    m = ((stats or {}).get("by_module_primary") or {}).get(module or "") or {}
    n = int(m.get("count") or 0)
    if n == 0:
        text = "no closed live trades yet"
    else:
        text = f"{n} closed, win {_pct(m.get('win_rate'), digits=0)}, avg {_r(m.get('avg_r'))}"
        if m.get("baseline_avg_r") is not None:
            text += f" vs baseline {_r(m.get('baseline_avg_r'))}"
        ci = _metric_ci(m)
        if ci:
            text += f" (95% CI {ci})"
    return {"text": text, "n": n, "min": min_closed, "preliminary": n < min_closed,
            "tag": f"{PRELIMINARY}, {n}/{min_closed} closed" if n < min_closed else "",
            "avg_r_cls": sign_class(m.get("avg_r")), "evidence": _evidence(m)}


def _track_text(stats: Optional[dict[str, Any]], module: Optional[str]) -> str:
    t = _track(stats, module)
    return t["text"] + (f" ({t['tag']})" if t["tag"] else "")


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


def _source_links(conn: sqlite3.Connection, sources: list[Any]) -> list[dict[str, Any]]:
    """Short labels for brief sources: an accession becomes its form type ('8-K') linking
    to EDGAR; a URL becomes 'news: <domain>' linking to it. ``url`` is only ever an
    http(s) URL (or None), so the template can put it in href safely (autoescaped)."""
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw in sources:
        s = str(raw or "").strip()
        if not s:
            continue
        label, url = s if len(s) <= 24 else s[:21] + "...", None
        if _ACCESSION.match(s):
            form, cik, doc = "SEC filing", None, None
            try:
                row = conn.execute("SELECT form, cik, primary_doc_url FROM filings WHERE accession = ?",
                                   (s,)).fetchone()
            except sqlite3.Error:
                row = None
            if row:
                form, cik, doc = row[0] or form, row[1], row[2]
            label = form
            if doc and str(doc).startswith(("http://", "https://")):
                url = str(doc)
            elif cik:
                try:
                    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{s.replace('-', '')}/"
                except ValueError:
                    url = None
        else:
            p = urlparse(s)
            if p.scheme in ("http", "https") and p.netloc:
                host = p.netloc.lower().split("@")[-1].split(":")[0]
                host = host[4:] if host.startswith("www.") else host
                label = "news: Google News" if host == "news.google.com" else f"news: {host}"
                url = s
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label = f"{label} #{seen[label]}"
        out.append({"label": label, "url": url, "title": s})
    return out


def _sparkline(closes: list[float], stop: float, entry: float, target: float,
               w: int = 320, h: int = 88) -> Markup:
    """Inline SVG: closes with stop/entry/target lines (numbers only -> safe markup)."""
    label_w, pad = 64, 6
    vals = [*closes, stop, entry, target]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or max(abs(hi) * 0.01, 0.01)
    pw = w - label_w

    def y(v: float) -> float:
        return pad + (hi - v) / span * (h - 2 * pad)

    n = len(closes)
    pts = " ".join(f"{(i / (n - 1) * pw if n > 1 else pw):.1f},{y(c):.1f}" for i, c in enumerate(closes))
    levels = sorted([("t", "T", target), ("e", "E", entry), ("s", "S", stop)], key=lambda t: y(t[2]))
    ys = [y(v) for _, _, v in levels]
    for i in range(1, 3):  # keep the right-hand labels from overlapping
        ys[i] = max(ys[i], ys[i - 1] + 11)
    shift = max(0.0, ys[-1] - (h - 2))
    ys = [max(9.0, v - shift) for v in ys]
    parts = [f'<svg class="spark" viewBox="0 0 {w} {h}" role="img" aria-label="Last {n} closes with stop, '
             f'entry and target">']
    for (cls, letter, v), ly in zip(levels, ys):
        parts.append(f'<line class="lv-{cls}" x1="0" x2="{pw:.0f}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        parts.append(f'<text class="tx-{cls}" x="{pw + 4:.0f}" y="{ly + 3.5:.1f}">{letter} {v:,.2f}</text>')
    parts.append(f'<polyline class="px" points="{pts}"/>')
    if n:
        lx = pw if n > 1 else pw
        parts.append(f'<circle class="dot" cx="{lx:.1f}" cy="{y(closes[-1]):.1f}" r="2.6"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def _strip(conn: sqlite3.Connection, rec: Recommendation, as_of: date) -> Optional[dict[str, Any]]:
    """Trade strip for a card: last close, % to the entry trigger, stop-entry-target bar
    and sparkline. None when the DB holds no prices for the ticker."""
    bars = _bars(conn, rec.ticker, as_of, SPARK_SESSIONS)
    if not bars or not rec.entry or rec.stop is None or rec.target is None:
        return None
    close = bars[-1][3]
    to_entry = rec.entry / close - 1 if close else None
    lo, hi = min(rec.stop, close), max(rec.target, close)
    span = (hi - lo) or 1.0

    def pos(v: float) -> float:
        return round(min(100.0, max(0.0, (v - lo) / span * 100)), 1)

    if to_entry is None:
        trigger = "n/a"
    elif to_entry > 0:
        trigger = f"{_pct(to_entry, signed=True)} to entry"
    else:
        trigger = f"at/above entry ({_pct(to_entry, signed=True)})"
    return {
        "close": _price(close), "close_date": bars[-1][0], "to_entry": trigger,
        "to_entry_val": to_entry, "to_entry_pct": _pct(to_entry, signed=True) if to_entry is not None else "n/a",
        "bar": {"stop": pos(rec.stop), "entry": pos(rec.entry), "target": pos(rec.target), "now": pos(close)},
        "spark": _sparkline([b[3] for b in bars], rec.stop, rec.entry, rec.target),
        "sessions": len(bars),
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
    brief = _valid_brief(rec.llm_brief)
    if brief:
        brief["source_links"] = _source_links(conn, brief["sources"])
    target_r = d.get("target_r_multiple", config.trade_plan.target_r_multiple)
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
        "target_r": target_r,
        "rr": f"{float(target_r):.1f}" if target_r is not None else "n/a",
        "time_stop": f"close of trading day {config.trade_plan.time_stop_trading_days} after entry",
        "valid_until": rec.valid_until.isoformat() if rec.valid_until else "n/a",
        "guardrail": status,
        "guardrail_label": GUARDRAIL_LABELS.get(status, status),
        "guardrail_reasons": list(rec.guardrail_reasons or []),
        "ev_fcf": ev_fcf,
        "earnings": earnings,
        "insiders": _insider_buys(conn, rec.ticker, as_of),
        "brief": brief,
        "red_flag_count": len(brief["red_flags"]) if brief else 0,
        "track": _track_text(stats, rec.primary_module),
        "track_info": _track(stats, rec.primary_module),
        "score": _num(rec.total_score, 1),
        "strip": _strip(conn, rec, as_of),
    }


def catchup_note(remaining: int) -> str:
    if remaining <= 0:
        return ""
    return f"catch-up in progress, {remaining} day{'s' if remaining != 1 else ''} remaining"


def _repeat_note(rec: Recommendation) -> str:
    """D3: later signals on a ticker that still had this live recommendation were folded
    into it instead of creating a new one."""
    repeats = (rec.details or {}).get("repeat_signals") or []
    if not repeats:
        return ""
    return (f"still valid from {rec.signal_date.isoformat()} "
            f"(repeat signal {', '.join(r['date'] for r in repeats)})")


def _short_row(conn: sqlite3.Connection, rec: Recommendation) -> dict[str, Any]:
    is_open = rec.status == "open"
    return {
        "note": _repeat_note(rec),
        "date": rec.signal_date.isoformat(),
        "ticker": rec.ticker,
        "modules": ", ".join(module_label(m) for m in rec.modules),
        "entry": _price(rec.entry), "stop": _price(rec.stop), "target": _price(rec.target),
        "status": rec.status or "n/a",
        "current_r": _r(rec.current_r) if is_open and rec.current_r is not None else "-",
        "r_cls": sign_class(rec.current_r) if is_open else "",
        # the entry window only matters while the buy-stop is still working
        "valid_until": "-" if is_open else (rec.valid_until.isoformat() if rec.valid_until else "n/a"),
        "regime": rec.regime,
        "shown": bool(rec.in_report),
    }


def _simulate_open(conn: sqlite3.Connection, as_of: date, config: Config) -> dict[str, dict[str, Any]]:
    """Re-simulate live open positions (cheap: only open ones) -> {rec_id: {rec, res}}.
    The entry date is not stored, so this is how the report learns it."""
    out: dict[str, dict[str, Any]] = {}
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
                try:
                    out[rec.id] = {"rec": rec, "res": simulate(rec, frame, None, config)}
                except Exception as exc:  # noqa: BLE001 -- one bad row must not hide the rest
                    log.warning("report: could not re-simulate %s: %s", rec.id, exc)
    except Exception as exc:  # noqa: BLE001 -- the report must not fail on this
        log.warning("report: could not re-simulate open positions: %s", exc)
    return out


def _tracking_updates(conn: sqlite3.Connection, as_of: date, since: date, config: Config,
                      sims: Optional[dict[str, dict[str, Any]]] = None) -> list[dict[str, Any]]:
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
            "r_cls": sign_class(x["r_multiple"]) if status != "expired" else "",
            "pct": _pct(x["pct_return"], signed=True) if x["pct_return"] is not None else "-",
            "note": reason,
        })

    # triggered since the previous report but still open
    sims = _simulate_open(conn, as_of, config) if sims is None else sims
    for s in sims.values():
        rec, res = s["rec"], s["res"]
        if res.entry_date and since < res.entry_date <= as_of:
            events.append({
                "ticker": rec.ticker, "event": "triggered", "label": _EVENT_LABELS["triggered"],
                "date": res.entry_date.isoformat(), "signal_date": rec.signal_date.isoformat(),
                "r": _r(res.r_multiple) if res.r_multiple is not None else "-",
                "r_cls": sign_class(res.r_multiple), "pct": "-",
                "note": f"filled at {_price(res.entry_fill)}",
            })
    events.sort(key=lambda e: (e["date"], e["ticker"]))
    return events


def _time_stop_date(dates: list[str], i0: int, sessions: int, left: int) -> tuple[str, bool]:
    """(ISO date of the time-stop session, is_estimate). Future sessions are estimated
    with a weekday calendar (exchange holidays ignored)."""
    if i0 + sessions < len(dates):
        return dates[i0 + sessions], False
    d = np.busday_offset(np.datetime64(dates[-1]), max(left, 0), roll="forward")
    return str(d), True


def _open_position(conn: sqlite3.Connection, rec: Recommendation, sim: Optional[dict[str, Any]],
                   as_of: date, config: Config) -> dict[str, Any]:
    """One row of the open-position status board."""
    row = _short_row(conn, rec)
    res = sim["res"] if sim else None
    cur_r = rec.current_r if rec.current_r is not None else (res.r_multiple if res and res.status == "open" else None)
    row.update({"current_r": _r(cur_r) if cur_r is not None else "n/a", "r_cls": sign_class(cur_r),
                "cur_r_val": cur_r, "close": "n/a", "to_stop": "n/a", "to_target": "n/a", "days": "n/a",
                "time_stop": "n/a", "stop_r": None, "target_r": None, "left": None, "partial": False,
                "attention": []})
    if res is None or res.entry_date is None or _split_after(conn, rec.ticker, rec.signal_date):
        return row
    tp = config.trade_plan
    since = res.entry_date.isoformat()
    try:
        bars = conn.execute("SELECT date, high, close FROM prices WHERE ticker = ? AND date >= ? AND date <= ? "
                            "AND close IS NOT NULL ORDER BY date", (rec.ticker, since, as_of.isoformat())).fetchall()
    except sqlite3.Error:
        bars = []
    if not bars or str(bars[0][0])[:10] != since:
        return row
    dates = [str(b[0])[:10] for b in bars]
    close = float(bars[-1][2])
    held = len(bars) - 1
    left = tp.time_stop_trading_days - held
    ts_date, est = _time_stop_date(dates, 0, tp.time_stop_trading_days, left)
    partial = max(float(b[1] if b[1] is not None else b[2]) for b in bars) >= rec.target
    risk = rec.entry - rec.stop
    eff_stop = float(res.entry_fill or rec.entry) if partial else rec.stop
    stop_r = (close - eff_stop) / risk if risk > 0 else None
    target_r = None if partial else ((rec.target - close) / risk if risk > 0 else None)
    att = []
    if stop_r is not None and stop_r <= ATTENTION_R:
        att.append(f"{stop_r:.2f}R above {'breakeven stop' if partial else 'stop'}" if stop_r >= 0
                   else f"close below {'breakeven stop' if partial else 'stop'}")
    if target_r is not None and target_r <= ATTENTION_R:
        att.append(f"{target_r:.2f}R below target" if target_r >= 0 else "close above target")
    if left <= ATTENTION_SESSIONS:
        att.append(f"time stop in {max(left, 0)} session{'s' if left != 1 else ''} ({ts_date})")
    row.update({
        "close": _price(close), "days": f"{held}/{tp.time_stop_trading_days}",
        "time_stop": ("~" if est else "") + date.fromisoformat(ts_date).strftime("%b %d"),
        "time_stop_date": ts_date, "left": left, "partial": partial,
        "to_stop": _pct(eff_stop / close - 1, signed=True) if close else "n/a",
        "to_target": "partial taken" if partial else (_pct(rec.target / close - 1, signed=True) if close else "n/a"),
        "stop_r": stop_r, "target_r": target_r, "attention": att,
        "entry_date": since,
    })
    return row


def _pending_row(conn: sqlite3.Connection, rec: Recommendation, as_of: date) -> dict[str, Any]:
    row = _short_row(conn, rec)
    bars = _bars(conn, rec.ticker, as_of, 1)
    close = bars[-1][3] if bars else None
    to_entry = rec.entry / close - 1 if close else None
    row.update({"close": _price(close), "to_entry": _pct(to_entry, signed=True) if to_entry is not None else "n/a",
                "to_entry_val": to_entry})
    return row


def _positions(conn: sqlite3.Connection, as_of: date, config: Config, sims: dict[str, dict[str, Any]],
               stats: Optional[dict[str, Any]], n_new: int) -> dict[str, Any]:
    rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND status IN ('open', 'pending') "
                        "AND signal_date < ? ORDER BY (status = 'open') DESC, signal_date, ticker",
                  (as_of.isoformat(),))
    recs = [_row_to_rec(r) for r in rows]
    pending = [_pending_row(conn, r, as_of) for r in recs if r.status == "pending"]
    opened = [_open_position(conn, r, sims.get(r.id), as_of, config) for r in recs if r.status == "open"]
    opened.sort(key=lambda p: (p["stop_r"] is None, p["stop_r"] if p["stop_r"] is not None else 0, p["ticker"]))
    attention = [p for p in opened if p["attention"]]

    note = ""
    counted = ((stats or {}).get("counts") or {}).get("pending")
    if counted is not None and counted != len(pending):
        try:
            today = conn.execute("SELECT COUNT(*), COALESCE(SUM(in_report), 0) FROM recommendations WHERE "
                                 "source = 'live' AND status = 'pending' AND signal_date = ?",
                                 (as_of.isoformat(),)).fetchone()
        except sqlite3.Error:
            today = (0, 0)
        n_today, shown_today = int(today[0] or 0), int(today[1] or 0)
        if n_today:
            hidden = n_today - shown_today
            note = (f"The statistics count {counted} pending: the {len(pending)} below plus {n_today} signalled "
                    f"today ({shown_today} shown above"
                    + (f", {hidden} tracked but not shown" if hidden else "")
                    + "), whose buy-stops can fill from the next session.")
    n_hidden = sum(1 for r in [*pending, *opened] if not r["shown"])
    return {"pending": pending, "open": opened, "attention": attention, "pending_note": note,
            "n_hidden": n_hidden}


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


def _scoreboard(stats: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """One-line live track record: closed n, win %, avg R vs baseline avg R."""
    if not stats:
        return None
    ov = stats.get("overall") or {}
    c = stats.get("counts") or {}
    min_closed = stats.get("min_closed_for_confidence", 30)
    n = int(ov.get("count") or 0)
    prelim = n < min_closed
    parts = [f"{n} closed"]
    if n:
        parts.append(f"{_pct(ov.get('win_rate'), digits=0)} wins")
        parts.append(f"avg {_r(ov.get('avg_r'))}")
        if ov.get("baseline_avg_r") is not None:
            parts.append(f"baseline {_r(ov.get('baseline_avg_r'))}")
    ci = _metric_ci(ov)
    text = ", ".join(parts)
    if ci:
        text += f" (95% CI {ci})"
    tag = f"{PRELIMINARY}: {n}/{min_closed} closed" if prelim else ""
    return {
        "n": n, "win": _pct(ov.get("win_rate"), digits=0) if n else "n/a",
        "avg_r": _r(ov.get("avg_r")) if n else "n/a", "avg_r_cls": sign_class(ov.get("avg_r")) if n else "",
        "baseline_r": _r(ov.get("baseline_avg_r")), "baseline_cls": sign_class(ov.get("baseline_avg_r")),
        "edge": _r(ov.get("excess_vs_baseline_r")), "edge_cls": sign_class(ov.get("excess_vs_baseline_r")),
        "ci": ci, "evidence": _evidence(ov), "preliminary": prelim,
        "module_evidence": [{"letter": module_letter(k), "label": module_label(k), "evidence": str(v)}
                            for k, v in sorted((stats.get("evidence_by_module") or {}).items(),
                                               key=lambda kv: module_label(kv[0])) if v]
                           if isinstance(stats.get("evidence_by_module"), dict) else [],
        "h20": _horizon_20d(stats), "tag": tag, "min_closed": min_closed,
        "open": c.get("open", 0), "pending": c.get("pending", 0),
        "text": f"Live record: {text}" + (f" ({tag})" if tag else "")
                + f". {c.get('open', 0)} open, {c.get('pending', 0)} pending.",
    }


def _ci_text(ci: Any, pct: bool = False) -> Optional[str]:
    """'[lo, hi]' -> '-0.10 to +0.30R' (or '%' when ``pct``); None when malformed."""
    lo = hi = None
    if isinstance(ci, (list, tuple)) and len(ci) == 2:
        lo, hi = ci
    elif isinstance(ci, dict):
        lo, hi = ci.get("lo", ci.get("low")), ci.get("hi", ci.get("high"))
    try:
        if lo is None or hi is None:
            return None
        if pct:
            return f"{float(lo) * 100:+.1f}% to {float(hi) * 100:+.1f}%"
        return f"{float(lo):+.2f} to {float(hi):+.2f}R"
    except (TypeError, ValueError):
        return None


def _horizon_cell(h: dict[str, Any]) -> dict[str, Any]:
    n = int(h.get("n") or 0)
    ex = h.get("mean_excess_vs_baseline")
    return {
        "n": n,
        "ret": _pct(h.get("mean_ret"), signed=True) if n else "n/a", "ret_cls": sign_class(h.get("mean_ret"), 0.0005),
        "vs_spy": _pct(h.get("mean_excess_vs_spy"), signed=True) if n else "n/a",
        "vs_spy_cls": sign_class(h.get("mean_excess_vs_spy"), 0.0005),
        "vs_base": _pct(ex, signed=True) if n and ex is not None else "n/a", "vs_base_cls": sign_class(ex, 0.0005),
        "ci": _ci_text(h.get("excess_vs_baseline_ci"), pct=True) if n else None,
        "evidence": h.get("evidence") if isinstance(h.get("evidence"), str) else None,
    }


def _horizon_rows(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Optional ``by_horizon`` forward returns: {"horizons": [5, ...], "overall": {"5d": H},
    "by_module": {...}} where H has n, mean_ret, mean_excess_vs_spy,
    mean_excess_vs_baseline, excess_vs_baseline_ci, evidence. Overall rows only."""
    bh = stats.get("by_horizon")
    if not isinstance(bh, dict) or not isinstance(bh.get("overall"), dict):
        return []
    out = []
    for k, h in bh["overall"].items():
        if isinstance(h, dict):
            out.append({"name": str(k), **_horizon_cell(h)})
    return out


def _horizon_20d(stats: dict[str, Any]) -> Optional[dict[str, Any]]:
    bh = stats.get("by_horizon")
    h = (bh.get("overall") or {}).get("20d") if isinstance(bh, dict) and isinstance(bh.get("overall"), dict) else None
    if not isinstance(h, dict) or not int(h.get("n") or 0):
        return None
    return _horizon_cell(h)


def _stats_section(stats: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not stats:
        return None
    min_closed = stats.get("min_closed_for_confidence", 30)
    warnings = stats.get("warnings") or {}

    ev_by = stats.get("evidence_by_module") if isinstance(stats.get("evidence_by_module"), dict) else {}

    def row(name: str, m: dict[str, Any], note: str = "", key: Optional[str] = None) -> dict[str, Any]:
        n = m.get("count", 0) or 0
        return {
            "name": name, "count": n,
            "win_rate": _pct(m.get("win_rate"), digits=0), "avg_r": _r(m.get("avg_r")),
            "avg_r_cls": sign_class(m.get("avg_r")), "avg_r_ci": _metric_ci(m),
            "expectancy": _r(m.get("expectancy")), "exp_cls": sign_class(m.get("expectancy")),
            "profit_factor": _num(m.get("profit_factor")),
            "vs_spy": _pct(m.get("avg_excess_vs_spy"), signed=True), "vs_spy_cls": sign_class(m.get("avg_excess_vs_spy"), 0.0005),
            "vs_sector": _pct(m.get("avg_excess_vs_sector"), signed=True),
            "baseline_r": _r(m.get("baseline_avg_r")), "baseline_cls": sign_class(m.get("baseline_avg_r")),
            "vs_baseline": _r(m.get("excess_vs_baseline_r")), "edge_cls": sign_class(m.get("excess_vs_baseline_r")),
            "note": note, "preliminary": n < min_closed, "evidence": _evidence(m) or ev_by.get(key or ""),
        }

    modules = [row(module_label(k), v, warnings.get(k, "") if (v.get("count") or 0) < min_closed else "", k)
               for k, v in (stats.get("by_module_primary") or {}).items()]
    for k, msg in sorted(warnings.items()):  # modules with recommendations but no closed trades yet
        if k not in (stats.get("by_module_primary") or {}):
            modules.append(row(module_label(k), {"count": 0}, msg, k))
    modules.sort(key=lambda m: m["name"])
    buckets = [row(k, v) for k, v in (stats.get("by_rank_bucket") or {}).items()]
    overall = row("All modules", stats.get("overall") or {})
    notes = [f"{m['name']}: {m['note']}" for m in modules if m["note"]]
    prelim = [f"{module_letter_from_label(m['name'])} {m['count']}/{min_closed}" for m in modules if m["preliminary"]]
    return {
        "counts": stats.get("counts") or {},
        "overall": overall,
        "modules": modules,
        "rank_buckets": buckets,
        "min_closed": min_closed,
        "notes": notes,
        "prelim_counts": ", ".join(prelim),
        "any_ci": any(m["avg_r_ci"] for m in [overall, *modules]),
        "horizons": _horizon_rows(stats),
        "excess_spy": _pct((stats.get("overall") or {}).get("avg_excess_vs_spy"), signed=True),
        "excess_sector": _pct((stats.get("overall") or {}).get("avg_excess_vs_sector"), signed=True),
    }


def build_context(conn: sqlite3.Connection, as_of: date, config: Optional[Config] = None,
                  since: Optional[date] = None, catchup_remaining: int = 0) -> dict[str, Any]:
    """Everything the templates need, as plain dicts/strings. ``catchup_remaining`` is the
    number of missed trading days the daily run left for its next run (R2)."""
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
    regime = _regime(conn, as_of, today, config.regime.breadth_threshold_pct)
    suppressed = regime["label"] == "Unfavorable"
    cards = [] if suppressed else [_card(conn, r, as_of, stats, config) for r in today]

    missed_rows = _query(conn, "SELECT * FROM recommendations WHERE source = 'live' AND in_report = 1 "
                               "AND signal_date > ? AND signal_date < ? ORDER BY signal_date, (rank IS NULL), rank, ticker",
                         (since.isoformat(), as_of.isoformat()))
    catch_up = [_short_row(conn, _row_to_rec(r)) for r in missed_rows]

    sims = _simulate_open(conn, as_of, config)
    positions = _positions(conn, as_of, config, sims, stats, len(cards))
    updates = _tracking_updates(conn, as_of, since, config, sims)

    return {
        "title": f"Swing Screener - {as_of.isoformat()}",
        "as_of": as_of.isoformat(),
        "since": since.isoformat(),
        "regime": regime,
        "suppressed": suppressed,
        "no_recs_today": not cards and not suppressed,
        "data_quality": _data_quality(conn, as_of),
        "catchup_note": catchup_note(catchup_remaining),
        "cards": cards,
        "n_new": len(today) if not suppressed else 0,
        "catch_up": catch_up,
        "updates": updates,
        "positions": positions,
        "open_recs": [*positions["open"], *positions["pending"]],
        "scoreboard": _scoreboard(stats),
        "stats": _stats_section(stats),
        "attention_r": ATTENTION_R,
        "attention_sessions": ATTENTION_SESSIONS,
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


# ---------------------------------------------------------------------------
# Telegram digest
# ---------------------------------------------------------------------------


class TelegramDigest(str):
    """Plain-text digest (a ``str``, printed by run_daily) that also carries
    ``messages``: the parse_mode=HTML messages for Telegram."""

    messages: list[str]

    def __new__(cls, plain: str, messages: list[str]) -> "TelegramDigest":
        obj = super().__new__(cls, plain)
        obj.messages = list(messages)
        return obj


def _esc(v: Any) -> str:
    return _html.escape("" if v is None else str(v), quote=False)


def _card_html(c: dict[str, Any]) -> str:
    head = f"<b>{'#' + _esc(c['rank']) + ' ' if c.get('rank') else ''}{_esc(c['ticker'])}</b> · {_esc(c['primary_module'])}"
    s = c.get("strip")
    lines = [head]
    if s:
        lines.append(f"Close {_esc(s['close'])} · entry {_esc(c['entry'])} (<b>{_esc(s['to_entry_pct'])}</b> to trigger)")
    else:
        lines.append(f"Entry {_esc(c['entry'])} (buy-stop)")
    lines.append(f"Stop {_esc(c['stop'])} ({_esc(c['stop_pct'])}) · Target {_esc(c['target'])} "
                 f"({_esc(c['target_pct'])}) · R/R {_esc(c['rr'])}")
    flags = []
    e = c.get("earnings")
    if e and e.get("in_window"):
        flags.append(f"⚠ earnings {_esc(e['date'])} in window")
    if c.get("red_flag_count"):
        n = c["red_flag_count"]
        flags.append(f"⚑ {n} red flag{'s' if n != 1 else ''}")
    if c.get("guardrail") not in ("pass", None):
        flags.append(f"guardrail {_esc(c['guardrail_label'])}")
    if flags:
        lines.append(" · ".join(flags))
    return "\n".join(lines)


def _pack(blocks: list[str], limit: int = TELEGRAM_SOFT_LIMIT) -> list[str]:
    """Join blocks with blank lines into as few messages as fit ``limit``."""
    msgs: list[str] = []
    cur = ""
    for b in blocks:
        if len(b) > limit:  # cannot happen with the blocks built here; keep it safe anyway
            b = _esc(re.sub(r"<[^>]+>", "", _html.unescape(b)))[: limit - 1] + "…"
        cand = f"{cur}\n\n{b}" if cur else b
        if len(cand) > limit and cur:
            msgs.append(cur)
            cur = b
        else:
            cur = cand
    if cur:
        msgs.append(cur)
    return msgs


def telegram_messages(ctx: dict[str, Any], max_updates: int = 8) -> list[str]:
    """parse_mode=HTML messages: header (regime one-liner, scoreboard), one compact block
    per idea, positions needing attention, tracking updates, footer. All dynamic text is
    HTML-escaped."""
    reg = ctx["regime"]
    head = [f"<b>Swing Screener · {_esc(ctx['as_of'])}</b>",
            f"Regime: <b>{_esc(reg['label'])}</b>" + (f" · {_esc(reg['short'])}" if reg.get("short") else "")]
    sb = ctx.get("scoreboard")
    if sb:
        line = f"Record: {sb['n']} closed"
        if sb["n"]:
            line += f" · {_esc(sb['win'])} wins · avg {_esc(sb['avg_r'])} vs baseline {_esc(sb['baseline_r'])}"
        if sb["preliminary"]:
            line += f" <i>({_esc(sb['tag'])})</i>"
        head.append(line)
        if sb.get("h20"):
            h = sb["h20"]
            head.append(f"20d vs baseline {_esc(h['vs_base'])}" + (f" (CI {_esc(h['ci'])}, n={h['n']})" if h["ci"] else ""))
        if sb.get("module_evidence"):
            head.append("Evidence: " + ", ".join(f"{_esc(e['letter'] or e['label'])} {_esc(e['evidence'].replace('_', ' '))}"
                                                 for e in sb["module_evidence"]))
    if ctx.get("catchup_note"):
        head.append(_esc(ctx["catchup_note"][0].upper() + ctx["catchup_note"][1:] + "."))
    if ctx.get("data_quality"):
        n = len(ctx["data_quality"])
        head.append(f"⚠ {n} data-quality note{'s' if n != 1 else ''} in the report")
    blocks = ["\n".join(head)]
    if ctx["suppressed"]:
        blocks.append("No new recommendations (Unfavorable regime).")
    elif not ctx["cards"]:
        blocks.append("No new recommendations today.")
    else:
        blocks.append(f"<b>New ideas: {ctx['n_new']}</b>")
        blocks += [_card_html(c) for c in ctx["cards"]]
    if ctx.get("catch_up"):
        blocks.append(f"Catch-up: {len(ctx['catch_up'])} recommendation(s) from missed days: "
                      + _esc(", ".join(r["ticker"] for r in ctx["catch_up"][:15])))
    pos = ctx.get("positions") or {}
    if pos.get("attention"):
        lines = ["<b>Needs attention</b>"]
        for p in pos["attention"]:
            lines.append(f"{_esc(p['ticker'])} {_esc(p['current_r'])}: {_esc('; '.join(p['attention']))}")
        blocks.append("\n".join(lines))
    if ctx.get("updates"):
        ups = ctx["updates"]
        lines = ["<b>Updates</b>"]
        for u in ups[:max_updates]:
            lines.append(f"{_esc(u['ticker'])} {_esc(u['label'].lower())}"
                         + (f" ({_esc(u['r'])})" if u["r"] not in ("-", "n/a") else ""))
        if len(ups) > max_updates:
            lines.append(f"...and {len(ups) - max_updates} more in the report.")
        blocks.append("\n".join(lines))
    if pos:
        blocks.append(f"{len(pos.get('open', []))} open, {len(pos.get('pending', []))} pending from earlier days.")
    blocks.append(f"<i>{_esc(FOOTER)}</i>")
    return _pack(blocks)


def summary_text(ctx: dict[str, Any], max_recs: int = 3, max_updates: int = 5) -> TelegramDigest:
    """Short plain-text digest (regime, scoreboard, top tickers with entry/stop/target and
    warnings, notable tracking updates) that also carries the Telegram HTML messages."""
    reg = ctx["regime"]
    lines = [f"Swing Screener {ctx['as_of']}", f"Regime: {reg['label']}"]
    if reg.get("banner"):
        lines[-1] += f" - {reg['banner']}"
    if ctx.get("scoreboard"):
        lines.append(ctx["scoreboard"]["text"])
    if ctx.get("catchup_note"):
        lines.append(ctx["catchup_note"][0].upper() + ctx["catchup_note"][1:] + ".")
    if ctx["suppressed"]:
        lines.append("No new recommendations (Unfavorable regime).")
    else:
        lines.append(f"New recommendations: {ctx['n_new']}")
        for c in ctx["cards"][:max_recs]:
            warn = []
            if (c.get("earnings") or {}).get("in_window"):
                warn.append("earnings in window")
            if c.get("red_flag_count"):
                warn.append(f"{c['red_flag_count']} red flag(s)")
            lines.append(f"{c['rank'] or '-'}. {c['ticker']}  entry {c['entry']} | stop {c['stop']} "
                         f"({c['stop_pct']}) | target {c['target']} ({c['target_pct']})"
                         + (f" [{', '.join(warn)}]" if warn else ""))
    if ctx["catch_up"]:
        lines.append(f"Catch-up: {len(ctx['catch_up'])} recommendation(s) from missed days.")
    if ctx["updates"]:
        lines.append("Updates: " + "; ".join(
            f"{u['ticker']} {u['label'].lower()}" + (f" ({u['r']})" if u["r"] not in ("-", "n/a") else "")
            for u in ctx["updates"][:max_updates]))
        if len(ctx["updates"]) > max_updates:
            lines.append(f"...and {len(ctx['updates']) - max_updates} more in the report.")
    lines.append(FOOTER)
    return TelegramDigest("\n".join(lines), telegram_messages(ctx))


def build(conn: sqlite3.Connection, as_of: date, *, config: Optional[Config] = None,
          since: Optional[date] = None, catchup_remaining: int = 0) -> tuple[str, str]:
    """Build the report for ``as_of`` and return ``(html_path, md_path)``.

    Also copies both files to ``reports/latest.html`` / ``reports/latest.md``. ``since``
    overrides the previous-report date used for "updates since the last report" and the
    catch-up section."""
    config = config or load_config()
    ctx = build_context(conn, as_of, config, since, catchup_remaining)
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
                  since: Optional[date] = None, catchup_remaining: int = 0) -> TelegramDigest:
    """Telegram digest for ``as_of`` (convenience wrapper over build_context + summary_text)."""
    return summary_text(build_context(conn, as_of, config, since, catchup_remaining))
