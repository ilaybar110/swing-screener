"""8-K text extraction for the LLM briefs (docs/PLAN.md section 16).

``get_8k_texts`` returns, for 8-Ks of one ticker filed in ``[as_of - days, as_of]``,
one dict per extracted section: the body of each relevant item (1.01, 2.02, 3.02,
5.02, 7.01, 8.01) cut out of the primary document, plus Exhibit 99.1. HTML is
converted to plain text. Results are cached in ``filing_texts`` (keyed by accession +
section) so a filing is downloaded at most once; a ``_done`` marker row records that an
accession was fetched even if it yielded no sections.

Point-in-time: only filings with ``filed_date <= as_of`` are considered.
"""

from __future__ import annotations

import html as html_lib
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from lxml import html as lxml_html

from src.data.edgar_client import EdgarClient
from src.utils.logging import get_logger

log = get_logger(__name__)

WANTED_ITEMS = ("1.01", "2.02", "3.02", "5.02", "7.01", "8.01")
EX99_SECTION = "EX-99.1"
DONE_SECTION = "_done"
MAX_SECTION_CHARS = 50_000

_BLOCK_TAGS = {
    "p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "table",
    "ul", "ol", "pre", "blockquote", "section", "center",
}
_ITEM_HEADING = re.compile(r"^\s*item\s*(\d{1,2}\.\d{2})\b[\s.:\-–—]*(.*)$", re.I)
_SGML_WRAPPER = re.compile(r"^\s*<DOCUMENT>.*?<TEXT>\s*", re.S)
_SGML_TRAILER = re.compile(r"\s*</TEXT>\s*</DOCUMENT>\s*$")
_SIGNATURE = re.compile(r"^\s*signatures?\s*$", re.I)


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------


def html_to_text(raw: str) -> str:
    """Convert an SEC HTML (or plain-text) document to readable plain text: scripts,
    styles and hidden inline-XBRL headers dropped, block elements on their own line,
    whitespace normalised."""
    if not raw or not raw.strip():
        return ""
    # some documents arrive wrapped in the SGML <DOCUMENT><TYPE>..<TEXT> envelope
    raw = _SGML_WRAPPER.sub("", raw, count=1)
    raw = _SGML_TRAILER.sub("", raw, count=1)
    if "<" not in raw:  # plain-text filing
        text = raw
    else:
        try:
            # lxml refuses str input that carries an XML encoding declaration
            root = lxml_html.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", raw))
        except Exception:  # noqa: BLE001 -- unparseable markup: fall back to tag stripping
            text = re.sub(r"<[^>]+>", " ", raw)
        else:
            for el in list(root.iter()):
                if not isinstance(el.tag, str):  # comments / processing instructions
                    el.drop_tree()
            for el in list(root.iter()):
                tag = el.tag.lower() if isinstance(el.tag, str) else ""
                style = (el.get("style") or "").replace(" ", "").lower()
                if tag in ("script", "style", "head", "ix:header") or "display:none" in style:
                    if el.getparent() is not None:
                        el.drop_tree()
            for el in root.iter():
                tag = el.tag.lower() if isinstance(el.tag, str) else ""
                if tag in _BLOCK_TAGS:
                    el.text = "\n" + (el.text or "")
                    el.tail = "\n" + (el.tail or "")
                elif tag in ("td", "th"):  # keep table cells apart: "(94,575) (93,210)"
                    el.tail = " " + (el.tail or "")
            text = root.text_content()
    text = html_lib.unescape(text).replace("\xa0", " ").replace("​", "")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        lines.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


def split_items(text: str, wanted: tuple[str, ...] = WANTED_ITEMS) -> dict[str, str]:
    """Cut an 8-K's plain text into {item_number: body}, for wanted items only. The
    body runs from the heading to the next ``Item x.xx`` heading or the signature
    block. If an item appears twice (e.g. a table of contents) the longest body wins."""
    lines = text.splitlines()
    heads: list[tuple[int, str, str]] = []  # (line_no, item, rest-of-heading-line)
    for i, line in enumerate(lines):
        m = _ITEM_HEADING.match(line)
        if m:
            heads.append((i, m.group(1), m.group(2).strip()))
    found: dict[str, str] = {}
    for pos, (i, item, rest) in enumerate(heads):
        if item not in wanted:
            continue
        end = heads[pos + 1][0] if pos + 1 < len(heads) else len(lines)
        body_lines = [rest] if rest else []
        for line in lines[i + 1:end]:
            if _SIGNATURE.match(line):
                break
            body_lines.append(line)
        body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines)).strip()
        if len(body) > len(found.get(item, "")):
            found[item] = body
    return found


# ---------------------------------------------------------------------------
# Cache + fetching
# ---------------------------------------------------------------------------


def _cached_sections(conn: sqlite3.Connection, accession: str) -> Optional[dict[str, str]]:
    rows = conn.execute(
        "SELECT section, text FROM filing_texts WHERE accession = ?", (accession,)
    ).fetchall()
    if not rows:
        return None
    return {r[0]: r[1] or "" for r in rows if r[0] != DONE_SECTION}


def _fetch_sections(
    client: EdgarClient, items: list[str], primary_url: Optional[str], ex99_url: Optional[str]
) -> dict[str, str]:
    """Download and clean the primary doc and EX-99.1; return {section: text}."""
    sections: dict[str, str] = {}
    wanted = tuple(i for i in items if i in WANTED_ITEMS)
    if primary_url and wanted:
        text = html_to_text(client.get_text(primary_url))
        for item, body in split_items(text, wanted).items():
            if body:
                sections[f"item_{item}"] = body[:MAX_SECTION_CHARS]
    if ex99_url:
        body = html_to_text(client.get_text(ex99_url))
        if body:
            sections[EX99_SECTION] = body[:MAX_SECTION_CHARS]
    return sections


def get_8k_texts(
    conn: sqlite3.Connection,
    ticker: str,
    as_of: date,
    days: int = 30,
    client: Optional[EdgarClient] = None,
) -> list[dict[str, Any]]:
    """Cleaned 8-K sections for ``ticker`` filed in [as_of - days, as_of].

    Returns a list of ``{accession, filed_date, items, section, text}`` (``items`` is
    the filing's list of item numbers; ``section`` is ``item_<n>`` or ``EX-99.1``),
    newest filing first. Fetch failures for a filing are logged and skipped (and not
    cached, so they are retried next time).
    """
    start = (as_of - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT accession, filed_date, items, primary_doc_url, exhibit_991_url FROM filings "
        "WHERE ticker = ? AND form IN ('8-K', '8-K/A') AND filed_date >= ? AND filed_date <= ? "
        "ORDER BY filed_date DESC, accession DESC",
        (ticker, start, as_of.isoformat()),
    ).fetchall()

    results: list[dict[str, Any]] = []
    for acc, filed, items_s, primary_url, ex99_url in rows:
        items = [i.strip() for i in (items_s or "").split(",") if i.strip()]
        if not any(i in WANTED_ITEMS for i in items):
            continue

        sections = _cached_sections(conn, acc)
        if sections is None:
            if not (primary_url or ex99_url):
                log.debug("no document URLs recorded for %s; cannot fetch text", acc)
                continue
            client = client or _default_client()
            try:
                sections = _fetch_sections(client, items, primary_url, ex99_url)
            except Exception as exc:  # noqa: BLE001 -- skip this filing, keep the rest
                log.warning("8-K text fetch failed for %s %s: %s", ticker, acc, exc)
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO filing_texts (accession, section, text) VALUES (?,?,?)",
                [(acc, s, t) for s, t in sections.items()] + [(acc, DONE_SECTION, "")],
            )
            conn.commit()

        for section in sorted(sections, key=lambda s: (s == EX99_SECTION, s)):
            text = sections[section]
            if text:
                results.append(
                    {"accession": acc, "filed_date": filed, "items": items,
                     "section": section, "text": text}
                )
    return results


def _default_client() -> EdgarClient:
    from src.data.edgar_daily import get_default_client

    return get_default_client()
