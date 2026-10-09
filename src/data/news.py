"""Recent news headlines from Google News RSS (docs/PLAN.md section 4.6).

Yahoo's news endpoints return nothing from the cloud environment, so Google News is
the only source. Two queries are issued -- ``<TICKER> stock`` and the (cleaned)
company name -- merged, de-duplicated (same URL, or near-identical title) and filtered
to ``[as_of - days, as_of]`` (point-in-time: nothing published after ``as_of``).
Google's search is fuzzy, so only items whose *title* names the company are kept: the
ticker as a whole word, the cleaned company name, or its first distinctive word
(``is_relevant``); the number of dropped items is logged.

The RSS search also gets ``after:``/``before:`` operators so historical ``as_of`` dates
(backtests) are served from the right window instead of "now".
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import feedparser

from src.utils.http import TimeoutSession, get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
_NEAR_DUP_RATIO = 0.88
_NAME_SUFFIX = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|"
    r"llc|l\.p\.|lp|n\.v\.|nv|s\.a\.|sa|ag|se|holdings?|group|class\s+[a-c]|common stock|"
    r"\(.*?\))$",
    re.I,
)


def clean_company_name(name: str) -> str:
    """'Apple Inc.' -> 'Apple'; 'Alphabet Inc. Class A' -> 'Alphabet'."""
    out = (name or "").strip()
    for _ in range(4):  # strip stacked suffixes ("Foo Holdings, Inc.")
        new = _NAME_SUFFIX.sub("", out).strip(" ,")
        if new == out:
            break
        out = new
    return out or (name or "").strip()


# Words too common to identify a company on their own ("First Financial Bancorp" -> "Bancorp").
_GENERIC_WORDS = frozenset(
    "the and of for first second new old north south east west american america united national "
    "international global general financial bank bancorp bancshares trust capital group holdings "
    "energy oil gas petroleum resources industries industrial technologies technology tech "
    "systems solutions services software health healthcare medical pharma pharmaceuticals "
    "therapeutics biosciences bio networks communications media entertainment partners "
    "properties realty reit royalty acquisition enterprises brands products materials".split()
)


def name_terms(company_name: str) -> list[str]:
    """Phrases that identify the company in a headline: the cleaned name and its first
    distinctive word (>= 4 letters, not a generic business word)."""
    name = clean_company_name(company_name)
    if not name:
        return []
    terms = [name]
    for word in re.findall(r"[A-Za-z][A-Za-z0-9&'\-]*", name):
        if len(word) >= 4 and word.lower() not in _GENERIC_WORDS:
            terms.append(word)
            break
    return terms


def _whole_word(term: str, text: str, ignore_case: bool) -> bool:
    flags = re.I if ignore_case else 0
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, flags) is not None


def is_relevant(title: str, ticker: str, company_name: str) -> bool:
    """True when the headline contains the ticker as a whole word (case-sensitive, so
    "ON" does not match "on") or a distinctive part of the company name (whole word,
    case-insensitive)."""
    if ticker and _whole_word(ticker, title, ignore_case=False):
        return True
    return any(_whole_word(t, title, ignore_case=True) for t in name_terms(company_name))


def _norm_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9%$ ]+", " ", title.lower()).split())


def _strip_source_suffix(title: str, source: str) -> str:
    """Google appends ' - Publisher' to every headline."""
    title = title.strip()
    if source and title.endswith(" - " + source):
        return title[: -(len(source) + 3)].rstrip()
    m = re.match(r"^(.*\S)\s+-\s+[^-]{2,60}$", title)
    return m.group(1) if m and not source else title


def _norm_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, "", ""))


def _parse_feed(content: bytes | str) -> list[dict[str, Any]]:
    feed = feedparser.parse(content)
    items: list[dict[str, Any]] = []
    for e in feed.entries:
        published = e.get("published_parsed")
        link = e.get("link")
        raw_title = (e.get("title") or "").strip()
        if not (published and link and raw_title):
            continue
        source = ((e.get("source") or {}).get("title") or "").strip()
        items.append(
            {
                "title": _strip_source_suffix(raw_title, source),
                "source": source,
                "published": datetime.fromtimestamp(calendar.timegm(published), tz=timezone.utc),
                "url": link.strip(),
            }
        )
    return items


def _search(session: Any, query: str, start: date, end: date) -> list[dict[str, Any]]:
    q = f"{query} after:{start.isoformat()} before:{(end + timedelta(days=1)).isoformat()}"
    resp = session.get(
        GOOGLE_NEWS_RSS,
        params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
    )
    resp.raise_for_status()
    return _parse_feed(resp.content)


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Items must be sorted newest first; the newest of any duplicate group is kept."""
    kept: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    kept_titles: list[str] = []
    for it in items:
        url_key = _norm_url(it["url"])
        title_key = _norm_title(it["title"])
        if url_key in seen_urls:
            continue
        if any(
            title_key == t or SequenceMatcher(None, title_key, t).ratio() >= _NEAR_DUP_RATIO
            for t in kept_titles
        ):
            continue
        seen_urls.add(url_key)
        kept_titles.append(title_key)
        kept.append(it)
    return kept


def get_news(
    ticker: str,
    company_name: str,
    as_of: date,
    days: int = 14,
    session: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Headlines about a stock published in [as_of - days, as_of], newest first.

    Each item: ``{title, source, published_at (ISO-8601 UTC), url}`` (plus ``link`` and
    ``published`` aliases matching the names in src/contracts.py). A failing query is
    logged and skipped; if everything fails the result is ``[]`` -- news never blocks
    the pipeline.
    """
    session = session or TimeoutSession(get_session(browser_like=True))
    start = as_of - timedelta(days=days)
    cutoff = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    floor = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)

    queries = [f"{ticker} stock"]
    name = clean_company_name(company_name)
    if name:
        queries.append(f'"{name}"')

    collected: list[dict[str, Any]] = []
    for q in queries:
        try:
            collected.extend(_search(session, q, start, as_of))
        except Exception as exc:  # noqa: BLE001 -- news is best-effort
            log.warning("Google News query %r failed: %s", q, exc)

    in_window = [it for it in collected if floor <= it["published"] < cutoff]
    in_window.sort(key=lambda it: it["published"], reverse=True)
    relevant = [it for it in in_window if is_relevant(it["title"], ticker, company_name)]
    filtered_out = len(in_window) - len(relevant)
    in_window = relevant

    out = []
    for it in _dedupe(in_window):
        iso = it["published"].isoformat()
        out.append(
            {
                "title": it["title"],
                "source": it["source"],
                "published_at": iso,
                "url": it["url"],
                "link": it["url"],
                "published": iso,
            }
        )
    log.info("get_news %s as_of=%s: %d raw -> %d items (%d filtered as not naming the company)",
             ticker, as_of, len(collected), len(out), filtered_out)
    return out
