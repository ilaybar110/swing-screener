"""Tests for src/data/news.py (no network; saved Google News RSS sample)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.data import news

SAMPLE = (Path(__file__).resolve().parent / "fixtures" / "edgar" / "gnews_sample.xml").read_bytes()


class FakeResp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, by_query=None, default=SAMPLE, fail_queries=()):
        self.by_query, self.default, self.fail = by_query or {}, default, fail_queries
        self.queries: list[str] = []

    def get(self, url, params=None, **kw):
        q = params["q"]
        self.queries.append(q)
        if any(f in q for f in self.fail):
            raise RuntimeError("503")
        for key, content in self.by_query.items():
            if key in q:
                return FakeResp(content)
        return FakeResp(self.default)


def _rss(*items):
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate>"
        f"<source url='http://x'>{s}</source></item>" for t, u, d, s in items)
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>x</title>{body}</channel></rss>".encode()


def test_parse_real_sample_filters_window_and_orders():
    out = news.get_news("AAPL", "Apple Inc.", date(2026, 9, 30), days=14, session=FakeSession())
    assert out, "expected items"
    dates = [o["published_at"] for o in out]
    assert dates == sorted(dates, reverse=True)
    # as_of = 2026-09-30: the Oct 1 00:39 UTC Stocktwits item must be excluded (point-in-time)
    assert all(d < "2026-10-01" for d in dates)
    assert not any("Tim Cook Reportedly" in o["title"] for o in out)
    top = out[0]
    assert set(top) >= {"title", "source", "published_at", "url", "link", "published"}
    assert top["source"] == "Stocktwits" and top["title"].startswith("AAPL Stock On-Track")
    assert not top["title"].endswith(" - " + top["source"])  # publisher suffix stripped


def test_window_lower_bound():
    out = news.get_news("AAPL", "Apple", date(2026, 9, 30), days=2, session=FakeSession())
    assert out and all(o["published_at"] >= "2026-09-28" for o in out)


def test_near_duplicate_titles_and_urls_deduped():
    # the sample has the same headline from two publishers
    out = news.get_news("AAPL", "", date(2026, 9, 30), days=14, session=FakeSession())
    titles = [o["title"] for o in out]
    assert sum(t.startswith("Apple Just Gained 10% in a Month") for t in titles) == 1
    assert len({o["url"] for o in out}) == len(out)


def test_two_queries_merged_and_deduped_by_url():
    a = _rss(("Alpha beats estimates - Reuters", "https://n.example/a?utm=1", "Tue, 29 Sep 2026 10:00:00 GMT", "Reuters"),
             ("Alpha CEO steps down - CNBC", "https://n.example/b", "Mon, 28 Sep 2026 10:00:00 GMT", "CNBC"))
    b = _rss(("Alpha beats estimates - Reuters", "https://n.example/a?utm=2", "Tue, 29 Sep 2026 10:00:00 GMT", "Reuters"),
             ("Alpha opens new plant - WSJ", "https://n.example/c", "Wed, 30 Sep 2026 09:00:00 GMT", "WSJ"))
    sess = FakeSession(by_query={'"Alpha"': b, "ALPH stock": a})
    out = news.get_news("ALPH", "Alpha Holdings, Inc.", date(2026, 9, 30), session=sess)
    assert len(sess.queries) == 2
    assert sess.queries[0].startswith("ALPH stock after:2026-09-16 before:2026-10-01")
    assert sess.queries[1].startswith('"Alpha" after:')
    assert [o["title"] for o in out] == ["Alpha opens new plant", "Alpha beats estimates", "Alpha CEO steps down"]
    assert out[1]["source"] == "Reuters" and out[1]["published_at"] == "2026-09-29T10:00:00+00:00"


def test_one_failing_query_is_tolerated_all_failing_gives_empty():
    sess = FakeSession(fail_queries=('"Apple"',))
    assert news.get_news("AAPL", "Apple Inc.", date(2026, 9, 30), session=sess)
    assert news.get_news("AAPL", "Apple Inc.", date(2026, 9, 30), session=FakeSession(fail_queries=("a",))) == []


def test_clean_company_name():
    assert news.clean_company_name("Apple Inc.") == "Apple"
    assert news.clean_company_name("Alphabet Inc. Class A") == "Alphabet"
    assert news.clean_company_name("Foo Holdings, Inc.") == "Foo"
    assert news.clean_company_name("Inc") == "Inc"
    assert news.clean_company_name("") == ""
