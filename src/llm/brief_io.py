"""File hand-off for the LLM briefs (docs/PLAN.md section 16).

Briefs are written by Claude itself (the cloud routine agent), not through an API:

1. ``write_inputs`` writes one JSON per reported recommendation into
   ``work/brief_inputs/`` (filtered 8-K text + news, trimmed to the token budget) plus
   an ``INSTRUCTIONS.md`` telling the agent exactly what to produce.
2. The agent writes ``work/brief_outputs/<TICKER>.json`` for each input, matching
   ``brief_schema.json``.
3. ``merge_outputs`` validates those files and stores the valid ones in
   ``recommendations.llm_brief``. It never raises: a missing or invalid brief is
   logged and simply omitted from the report.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Union

from src.config import Config, load_config
from src.contracts import Recommendation
from src.utils.logging import get_logger

log = get_logger(__name__)

StrPath = Union[str, Path]

SCHEMA_PATH = Path(__file__).with_name("brief_schema.json")
DEFAULT_INPUT_DIR = "work/brief_inputs"
DEFAULT_OUTPUT_DIR = "work/brief_outputs"
CHARS_PER_TOKEN = 4  # approximation used everywhere for token budgeting
NEWS_BUDGET_SHARE = 0.2  # share of the budget reserved for headlines
_FIXED_OVERHEAD_CHARS = 800  # ticker/company/as_of/rec_id keys etc.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_TICKER = re.compile(r"^[A-Za-z0-9._-]+$")

_BRIEF_KEYS = ("ticker", "summary", "upcoming_catalysts", "red_flags",
               "recent_positive_events", "sources")

INSTRUCTIONS = """\
# Brief-writing instructions

For **every** `<TICKER>.json` file in `work/brief_inputs/`, write exactly one file
`work/brief_outputs/<TICKER>.json` (same ticker, same case). Create the directory if it
does not exist. Do not write anything else into it.

Each input file holds: `ticker`, `company`, `as_of`, `rec_id`, `filings_8k` (extracted
8-K item / Exhibit 99.1 text, each with its `accession`, `filed_date`, `items`,
`section`, `text`) and `news` (headlines with `title`, `source`, `published_at`, `url`).

## Output format

A single JSON object that matches `src/llm/brief_schema.json`:

```json
{
  "ticker": "ABCD",
  "summary": "<= 600 characters, plain text, what happened recently according to the input",
  "upcoming_catalysts": [{"event": "Q3 earnings call", "date": "2026-11-04"}],
  "red_flags": ["Announced $500M convertible note offering (8-K 0001234567-26-000123)"],
  "recent_positive_events": ["Raised full-year revenue guidance (EX-99.1, 2026-09-22)"],
  "sources": ["0001234567-26-000123", "https://news.example.com/article"]
}
```

* `summary` - a string of at most 600 characters.
* `upcoming_catalysts` - list of `{"event": str, "date": "YYYY-MM-DD" or null}`. Use a
  date only when the input states an exact one; otherwise `null` (put any fuzzy timing,
  e.g. "second half", in `event`).
* `red_flags` - list of strings. Include any of: offerings, dilution, guidance cuts,
  litigation, executive departures, going-concern language, restatements, delisting
  notices, covenant breaches, material weaknesses, auditor changes.
* `recent_positive_events` - list of strings (guidance raises, contract wins, buybacks,
  upgrades, etc.) that are stated in the input.
* `sources` - list of strings; each is a URL or an accession number that appears in the
  input file. Cite what you used.

## Hard rules

1. Use **only facts present in the input file**. No outside knowledge, no guessing, no
   recalling anything you know about the company.
2. No buy/sell/hold opinions, no price predictions, no price targets, no judgement on
   whether the stock is attractive.
3. If nothing relevant is in the input, use empty lists (`[]`) and a short factual
   summary such as "No relevant filings or news in the input window."
4. Plain text only (no markdown, no HTML). No share counts or position sizes.
5. Output valid JSON only (no comments, no trailing commas).
6. A missing or invalid brief is dropped from the report (it never blocks it), so prefer
   a short correct brief over a long uncertain one.
"""


# ---------------------------------------------------------------------------
# Schema validation (hand-rolled so no extra dependency is needed; mirrors
# brief_schema.json -- a test keeps the two in sync)
# ---------------------------------------------------------------------------


def load_schema() -> dict[str, Any]:
    """The JSON Schema every brief must satisfy."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _str_list(name: str, value: Any, schema: dict[str, Any], errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{name}: must be a list")
        return
    if len(value) > schema["maxItems"]:
        errors.append(f"{name}: more than {schema['maxItems']} items")
    item_schema = schema["items"]
    for i, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{name}[{i}]: must be a string")
        elif len(item) < item_schema.get("minLength", 0) or len(item) > item_schema["maxLength"]:
            errors.append(f"{name}[{i}]: length must be {item_schema.get('minLength', 0)}-{item_schema['maxLength']}")


def validate_brief(data: Any, schema: Optional[dict[str, Any]] = None) -> list[str]:
    """Return a list of schema violations (empty when ``data`` is a valid brief)."""
    schema = schema or load_schema()
    props = schema["properties"]
    if not isinstance(data, dict):
        return ["brief must be a JSON object"]
    errors: list[str] = []
    for key in schema["required"]:
        if key not in data:
            errors.append(f"missing required field: {key}")
    for key in data:
        if key not in props:
            errors.append(f"unexpected field: {key}")

    if "ticker" in data and not isinstance(data["ticker"], str):
        errors.append("ticker: must be a string")
    if "summary" in data:
        s = data["summary"]
        if not isinstance(s, str):
            errors.append("summary: must be a string")
        elif len(s) > props["summary"]["maxLength"]:
            errors.append(f"summary: {len(s)} chars exceeds {props['summary']['maxLength']}")

    if "upcoming_catalysts" in data:
        cs = data["upcoming_catalysts"]
        cat_schema = props["upcoming_catalysts"]
        if not isinstance(cs, list):
            errors.append("upcoming_catalysts: must be a list")
        else:
            if len(cs) > cat_schema["maxItems"]:
                errors.append(f"upcoming_catalysts: more than {cat_schema['maxItems']} items")
            for i, c in enumerate(cs):
                if not isinstance(c, dict):
                    errors.append(f"upcoming_catalysts[{i}]: must be an object")
                    continue
                extra = set(c) - {"event", "date"}
                if extra:
                    errors.append(f"upcoming_catalysts[{i}]: unexpected field(s) {sorted(extra)}")
                ev = c.get("event")
                if not isinstance(ev, str) or not ev or len(ev) > cat_schema["items"]["properties"]["event"]["maxLength"]:
                    errors.append(f"upcoming_catalysts[{i}].event: must be a non-empty string")
                if "date" not in c:
                    errors.append(f"upcoming_catalysts[{i}]: missing date (use null)")
                elif c["date"] is not None and not (isinstance(c["date"], str) and _ISO_DATE.match(c["date"])):
                    errors.append(f"upcoming_catalysts[{i}].date: must be YYYY-MM-DD or null")

    for key in ("red_flags", "recent_positive_events", "sources"):
        if key in data:
            _str_list(key, data[key], props[key], errors)
    return errors


# ---------------------------------------------------------------------------
# write_inputs
# ---------------------------------------------------------------------------


def _entry_size(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, ensure_ascii=False))


def trim_inputs(
    filings: list[dict[str, Any]],
    news: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Fit filings + news into ``max_tokens`` (chars/4). Newest first; Exhibit 99.1 text
    is prioritised over item text. A text that does not fit is cut to the remaining
    room (a section with under ~200 chars of room is dropped). Returns
    (filings, news, truncated)."""
    budget = max(0, max_tokens * CHARS_PER_TOKEN - _FIXED_OVERHEAD_CHARS)
    news_budget = int(budget * NEWS_BUDGET_SHARE)
    filing_budget = budget - news_budget

    def newest_first(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda r: str(r.get(key) or ""), reverse=True)

    ordered = newest_first(filings, "filed_date")  # stable within a date
    ordered = sorted(ordered, key=lambda r: r.get("section") != "EX-99.1")  # EX-99.1 first
    truncated = False
    kept_f: list[dict[str, Any]] = []
    used = 0
    for row in ordered:
        row = dict(row)
        text = row.get("text") or ""
        room = filing_budget - used - _entry_size({**row, "text": ""})
        if room < 200:
            truncated = True
            continue
        if len(text) > room:
            row["text"] = text[:room].rstrip() + " [truncated]"
            truncated = True
        used += _entry_size(row)
        kept_f.append(row)

    # leftover filing budget flows to news
    news_room = news_budget + max(0, filing_budget - used)
    kept_n: list[dict[str, Any]] = []
    used_n = 0
    for item in newest_first(news, "published_at"):
        slim = {k: item.get(k) for k in ("title", "source", "published_at", "url") if item.get(k) is not None}
        if "url" not in slim and item.get("link"):
            slim["url"] = item["link"]
        size = _entry_size(slim)
        if used_n + size > news_room:
            truncated = True
            break
        kept_n.append(slim)
        used_n += size
    return kept_f, kept_n, truncated


def _company_name(conn: sqlite3.Connection, ticker: str) -> str:
    try:
        row = conn.execute("SELECT name FROM tickers WHERE ticker = ?", (ticker,)).fetchone()
    except sqlite3.Error:
        return ticker
    return row[0] if row and row[0] else ticker


def write_inputs(
    conn: sqlite3.Connection,
    recs: list[Recommendation],
    as_of: date,
    out_dir: StrPath = DEFAULT_INPUT_DIR,
    *,
    config: Optional[Config] = None,
    get_8k_texts: Optional[Callable[..., list[dict[str, Any]]]] = None,
    get_news: Optional[Callable[..., list[dict[str, Any]]]] = None,
) -> list[Path]:
    """Write one input JSON per ``in_report`` recommendation (top ``llm.briefs_top_n``
    by rank) plus ``INSTRUCTIONS.md`` into ``out_dir``. Stale ``*.json`` inputs from an
    earlier run are removed first. Returns the written input paths.

    ``get_8k_texts`` / ``get_news`` default to Bot 3's implementations and can be
    stubbed. A failure while fetching one stock's data is logged and that stock gets an
    input with empty lists (so the agent still writes a valid, empty brief)."""
    config = config or load_config()
    if get_8k_texts is None:
        from src.data.texts import get_8k_texts as _g8
        get_8k_texts = _g8
    if get_news is None:
        from src.data.news import get_news as _gn
        get_news = _gn

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.json"):
        stale.unlink()
    (out / "INSTRUCTIONS.md").write_text(INSTRUCTIONS, encoding="utf-8")

    chosen = sorted((r for r in recs if r.in_report),
                    key=lambda r: (r.rank is None, r.rank or 0, r.ticker))
    chosen = chosen[: config.llm.briefs_top_n]
    max_tokens = config.llm.max_input_tokens_per_stock

    written: list[Path] = []
    seen: set[str] = set()
    for rec in chosen:
        ticker = rec.ticker
        if ticker in seen or not _SAFE_TICKER.match(ticker):
            log.warning("write_inputs: skipping duplicate/unsafe ticker %r", ticker)
            continue
        seen.add(ticker)
        company = _company_name(conn, ticker)
        try:
            filings = list(get_8k_texts(conn, ticker, as_of, days=30))
        except Exception as exc:  # noqa: BLE001 -- best effort, brief just gets less context
            log.warning("write_inputs: 8-K texts failed for %s: %s", ticker, exc)
            filings = []
        try:
            news = list(get_news(ticker, company, as_of, days=14))
        except Exception as exc:  # noqa: BLE001
            log.warning("write_inputs: news failed for %s: %s", ticker, exc)
            news = []
        kept_f, kept_n, truncated = trim_inputs(filings, news, max_tokens)
        payload = {
            "ticker": ticker,
            "company": company,
            "as_of": as_of.isoformat(),
            "rec_id": rec.id,
            "truncated": truncated,
            "filings_8k": kept_f,
            "news": kept_n,
        }
        path = out / f"{ticker}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        written.append(path)
        log.info("write_inputs: %s -> %d filing sections, %d headlines (~%d tokens)%s",
                 ticker, len(kept_f), len(kept_n), path.stat().st_size // CHARS_PER_TOKEN,
                 ", truncated" if truncated else "")
    return written


# ---------------------------------------------------------------------------
# merge_outputs
# ---------------------------------------------------------------------------


def _find_rec_id(conn: sqlite3.Connection, ticker: str, inputs_dir: Path) -> Optional[str]:
    """The recommendation a brief belongs to: the ``rec_id`` recorded in its input file,
    else the most recent live in-report recommendation for the ticker."""
    input_file = inputs_dir / f"{ticker}.json"
    if input_file.exists():
        try:
            rec_id = json.loads(input_file.read_text(encoding="utf-8")).get("rec_id")
            if rec_id and conn.execute("SELECT 1 FROM recommendations WHERE id = ?", (rec_id,)).fetchone():
                return rec_id
        except (OSError, ValueError, AttributeError, sqlite3.Error):
            pass
    row = conn.execute(
        "SELECT id FROM recommendations WHERE ticker = ? AND source = 'live' AND in_report = 1 "
        "ORDER BY signal_date DESC LIMIT 1", (ticker,)
    ).fetchone()
    return row[0] if row else None


def merge_outputs(
    conn: sqlite3.Connection,
    in_dir: StrPath = DEFAULT_OUTPUT_DIR,
    *,
    inputs_dir: Optional[StrPath] = None,
) -> dict[str, Any]:
    """Validate ``<in_dir>/<TICKER>.json`` briefs and store the valid ones in
    ``recommendations.llm_brief``. Never raises.

    ``inputs_dir`` (default: ``brief_inputs`` next to ``in_dir``) is used to find which
    inputs were requested (-> ``missing``) and which recommendation each belongs to.
    Returns ``{"merged": [tickers], "invalid": {ticker: [errors]}, "missing": [tickers],
    "unmatched": [tickers]}``."""
    report: dict[str, Any] = {"merged": [], "invalid": {}, "missing": [], "unmatched": []}
    try:
        out_dir = Path(in_dir)
        inputs = Path(inputs_dir) if inputs_dir is not None else out_dir.parent / "brief_inputs"
        schema = load_schema()

        requested = sorted(p.stem for p in inputs.glob("*.json")) if inputs.is_dir() else []
        outputs = {p.stem: p for p in sorted(out_dir.glob("*.json"))} if out_dir.is_dir() else {}

        for ticker in requested:
            if ticker not in outputs:
                report["missing"].append(ticker)
                log.warning("brief missing for %s (no %s)", ticker, out_dir / f"{ticker}.json")

        for ticker, path in outputs.items():
            try:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    report["invalid"][ticker] = [f"unreadable JSON: {exc}"]
                    log.warning("brief for %s is not valid JSON: %s", ticker, exc)
                    continue
                errors = validate_brief(data, schema)
                if not errors and data.get("ticker") not in (None, ticker):
                    errors.append(f"ticker field {data.get('ticker')!r} does not match file name")
                if errors:
                    report["invalid"][ticker] = errors
                    log.warning("brief for %s invalid: %s", ticker, "; ".join(errors))
                    continue
                rec_id = _find_rec_id(conn, ticker, inputs)
                if rec_id is None:
                    report["unmatched"].append(ticker)
                    log.warning("brief for %s has no matching recommendation", ticker)
                    continue
                brief = {k: data[k] for k in _BRIEF_KEYS if k in data}
                brief["ticker"] = ticker
                conn.execute("UPDATE recommendations SET llm_brief = ? WHERE id = ?",
                             (json.dumps(brief, ensure_ascii=False), rec_id))
                report["merged"].append(ticker)
            except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the rest
                report["invalid"][ticker] = [f"unexpected error: {exc}"]
                log.warning("brief for %s failed to merge: %s", ticker, exc)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- merging never blocks the pipeline
        log.error("merge_outputs failed: %s", exc)
        report["error"] = str(exc)
    log.info("merge_outputs: %d merged, %d invalid, %d missing, %d unmatched",
             len(report["merged"]), len(report["invalid"]), len(report["missing"]),
             len(report["unmatched"]))
    return report
