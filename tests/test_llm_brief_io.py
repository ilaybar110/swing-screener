"""brief_io: input writing/trimming, schema validation, output merging (round trip)."""

from __future__ import annotations

import json

import pytest

from src.llm import brief_io
from tests.report_helpers import AS_OF, GOOD_BRIEF, insert_rec, make_config, make_db, make_rec


def _filings(n=3, size=2000):
    rows = []
    for i in range(n):
        rows.append({"accession": f"0000-26-{i:06d}", "filed_date": f"2026-09-{10 + i:02d}", "items": ["2.02", "9.01"],
                     "section": "item_2.02", "text": f"item text {i} " + "x" * size})
        rows.append({"accession": f"0000-26-{i:06d}", "filed_date": f"2026-09-{10 + i:02d}", "items": ["2.02", "9.01"],
                     "section": "EX-99.1", "text": f"press release {i} " + "y" * size})
    return rows


def _news(n=5):
    return [{"title": f"Headline {i}", "link": f"https://n.example/{i}", "url": f"https://n.example/{i}",
             "published": f"2026-09-{20 + i:02d}T10:00:00+00:00", "published_at": f"2026-09-{20 + i:02d}T10:00:00+00:00",
             "source": "Wire"} for i in range(n)]


@pytest.fixture()
def cfg(tmp_path):
    return make_config(tmp_path)


def test_write_inputs_contents_and_instructions(tmp_path, cfg):
    conn = make_db(tmp_path)
    recs = [make_rec("AAA", rank=1), make_rec("BBB", rank=2), make_rec("CCC", rank=3, in_report=False)]
    for r in recs:
        insert_rec(conn, r)
    out = tmp_path / "work" / "brief_inputs"
    calls = {}

    def g8(conn_, ticker, as_of, days=30):
        calls.setdefault("8k", []).append((ticker, as_of, days))
        return _filings(1, 100)

    def gn(ticker, name, as_of, days=14):
        calls.setdefault("news", []).append((ticker, name, as_of, days))
        return _news(2)

    paths = brief_io.write_inputs(conn, recs, AS_OF, out, config=cfg, get_8k_texts=g8, get_news=gn)
    assert sorted(p.name for p in paths) == ["AAA.json", "BBB.json"]  # not-in-report excluded
    assert (out / "INSTRUCTIONS.md").exists() and not (out / "CCC.json").exists()
    data = json.loads((out / "AAA.json").read_text(encoding="utf-8"))
    assert data["ticker"] == "AAA" and data["company"] == "Alpha Corp"
    assert data["as_of"] == AS_OF.isoformat() and data["rec_id"] == recs[0].id
    assert {f["section"] for f in data["filings_8k"]} == {"item_2.02", "EX-99.1"}
    assert data["filings_8k"][0]["section"] == "EX-99.1"  # exhibit first
    assert [n["title"] for n in data["news"]] == ["Headline 1", "Headline 0"]  # newest first
    assert calls["8k"][0] == ("AAA", AS_OF, 30) and calls["news"][0][3] == 14

    text = (out / "INSTRUCTIONS.md").read_text(encoding="utf-8").lower()
    for needle in ("work/brief_outputs/<ticker>.json", "brief_schema.json", "only facts present in the input",
                   "no outside knowledge", "buy/sell", "price predictions", "empty lists", "accession"):
        assert needle in text, needle
    for key in ("summary", "upcoming_catalysts", "red_flags", "recent_positive_events", "sources", "600"):
        assert key in text


def test_write_inputs_respects_top_n_and_clears_stale(tmp_path, cfg):
    conn = make_db(tmp_path)
    cfg.llm.briefs_top_n = 2
    recs = [make_rec(t, rank=i) for i, t in enumerate(["AAA", "BBB", "CCC"], 1)]
    out = tmp_path / "in"
    out.mkdir()
    (out / "STALE.json").write_text("{}", encoding="utf-8")
    paths = brief_io.write_inputs(conn, recs, AS_OF, out, config=cfg,
                                  get_8k_texts=lambda *a, **k: [], get_news=lambda *a, **k: [])
    assert [p.stem for p in paths] == ["AAA", "BBB"]
    assert not (out / "STALE.json").exists()


def test_trim_respects_token_budget_and_priorities():
    filings, news = _filings(6, 8000), _news(40)
    max_tokens = 1500
    kept_f, kept_n, truncated = brief_io.trim_inputs(filings, news, max_tokens)
    total = len(json.dumps({"f": kept_f, "n": kept_n}))
    assert truncated and total <= max_tokens * brief_io.CHARS_PER_TOKEN
    assert kept_f[0]["section"] == "EX-99.1"
    assert kept_f[0]["filed_date"] == "2026-09-15"  # newest exhibit survives
    assert kept_f[0]["text"].endswith("[truncated]")
    assert kept_n and kept_n[0]["title"] == "Headline 39"


def test_trim_small_input_untouched():
    f, n, truncated = brief_io.trim_inputs(_filings(1, 50), _news(2), 6000)
    assert not truncated and len(f) == 2 and len(n) == 2 and "[truncated]" not in json.dumps(f)


def test_write_inputs_survives_fetch_failures(tmp_path, cfg):
    conn = make_db(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("network down")

    paths = brief_io.write_inputs(conn, [make_rec("AAA")], AS_OF, tmp_path / "in", config=cfg,
                                  get_8k_texts=boom, get_news=boom)
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["filings_8k"] == [] and data["news"] == []


def test_validate_brief_matches_schema_file():
    assert brief_io.validate_brief(GOOD_BRIEF) == []
    schema = brief_io.load_schema()
    assert set(schema["required"]) == {"summary", "upcoming_catalysts", "red_flags", "recent_positive_events", "sources"}
    assert schema["properties"]["summary"]["maxLength"] == 600
    jsonschema = pytest.importorskip("jsonschema")
    cases = [
        GOOD_BRIEF, {**GOOD_BRIEF, "summary": "x" * 600}, {k: v for k, v in GOOD_BRIEF.items() if k != "ticker"},
        {}, [], {**GOOD_BRIEF, "summary": "x" * 601}, {**GOOD_BRIEF, "extra": 1},
        {**GOOD_BRIEF, "red_flags": "none"}, {**GOOD_BRIEF, "sources": [1]},
        {**GOOD_BRIEF, "upcoming_catalysts": [{"event": "x"}]},
        {**GOOD_BRIEF, "upcoming_catalysts": [{"event": "x", "date": "soon"}]},
        {**GOOD_BRIEF, "upcoming_catalysts": ["x"]},
        {**GOOD_BRIEF, "upcoming_catalysts": [{"event": "x", "date": None, "extra": 1}]},
    ]
    validator = jsonschema.Draft202012Validator(schema)
    for case in cases:
        assert (not brief_io.validate_brief(case)) == validator.is_valid(case), case


def _setup_round_trip(tmp_path, cfg):
    conn = make_db(tmp_path)
    recs = [make_rec("AAA", rank=1), make_rec("BBB", rank=2), make_rec("CCC", rank=3)]
    for r in recs:
        insert_rec(conn, r)
    conn.commit()
    work = tmp_path / "work"
    brief_io.write_inputs(conn, recs, AS_OF, work / "brief_inputs", config=cfg,
                          get_8k_texts=lambda *a, **k: _filings(1, 50), get_news=lambda *a, **k: _news(1))
    (work / "brief_outputs").mkdir()
    return conn, work


def test_round_trip(tmp_path, cfg):
    conn, work = _setup_round_trip(tmp_path, cfg)
    out = work / "brief_outputs"
    (out / "AAA.json").write_text(json.dumps(GOOD_BRIEF), encoding="utf-8")
    (out / "BBB.json").write_text(json.dumps({k: v for k, v in GOOD_BRIEF.items() if k != "ticker"}), encoding="utf-8")
    rep = brief_io.merge_outputs(conn, out)
    assert rep["merged"] == ["AAA", "BBB"] and rep["missing"] == ["CCC"] and not rep["invalid"]
    stored = json.loads(conn.execute("SELECT llm_brief FROM recommendations WHERE ticker='AAA'").fetchone()[0])
    assert stored["summary"] == GOOD_BRIEF["summary"] and stored["ticker"] == "AAA"
    assert conn.execute("SELECT llm_brief FROM recommendations WHERE ticker='CCC'").fetchone()[0] is None
    assert json.loads(conn.execute("SELECT llm_brief FROM recommendations WHERE ticker='BBB'").fetchone()[0])["ticker"] == "BBB"

    # the stored brief renders in the report; the stock without one simply has no brief
    from src import report
    ctx = report.build_context(conn, AS_OF, cfg)
    by_ticker = {c["ticker"]: c for c in ctx["cards"]}
    assert by_ticker["AAA"]["brief"]["red_flags"] and by_ticker["CCC"]["brief"] is None


def test_merge_logs_and_skips_invalid_and_never_raises(tmp_path, cfg, caplog):
    conn, work = _setup_round_trip(tmp_path, cfg)
    out = work / "brief_outputs"
    (out / "AAA.json").write_text("{not json", encoding="utf-8")
    (out / "BBB.json").write_text(json.dumps({**GOOD_BRIEF, "ticker": "BBB", "summary": "x" * 601}), encoding="utf-8")
    (out / "CCC.json").write_text(json.dumps({**GOOD_BRIEF, "ticker": "WRONG"}), encoding="utf-8")
    (out / "ZZZ.json").write_text(json.dumps({**GOOD_BRIEF, "ticker": "ZZZ"}), encoding="utf-8")  # no such rec
    with caplog.at_level("WARNING"):
        rep = brief_io.merge_outputs(conn, out)
    assert set(rep["invalid"]) == {"AAA", "BBB", "CCC"} and rep["merged"] == [] and rep["unmatched"] == ["ZZZ"]
    assert any("AAA" in r.getMessage() for r in caplog.records)
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE llm_brief IS NOT NULL").fetchone()[0] == 0


def test_merge_with_missing_directories_and_closed_db(tmp_path):
    conn = make_db(tmp_path)
    assert brief_io.merge_outputs(conn, tmp_path / "nope")["merged"] == []
    conn.close()
    rep = brief_io.merge_outputs(conn, tmp_path / "nope2")  # closed connection must not raise
    assert isinstance(rep, dict)


def test_merge_targets_rec_from_input_file_on_catch_up(tmp_path, cfg):
    """Two live in-report recs for one ticker: the brief goes to the one named in the input."""
    conn = make_db(tmp_path)
    older = make_rec("AAA", signal=AS_OF.replace(day=28))
    newer = make_rec("AAA", signal=AS_OF)
    insert_rec(conn, older)
    insert_rec(conn, newer)
    conn.commit()
    work = tmp_path / "work"
    brief_io.write_inputs(conn, [older], AS_OF, work / "brief_inputs", config=cfg,
                          get_8k_texts=lambda *a, **k: [], get_news=lambda *a, **k: [])
    (work / "brief_outputs").mkdir()
    (work / "brief_outputs" / "AAA.json").write_text(json.dumps(GOOD_BRIEF), encoding="utf-8")
    brief_io.merge_outputs(conn, work / "brief_outputs")
    rows = dict(conn.execute("SELECT id, llm_brief IS NOT NULL FROM recommendations").fetchall())
    assert rows[older.id] == 1 and rows[newer.id] == 0
