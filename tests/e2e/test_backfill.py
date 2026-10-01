"""Backfill stages on fixtures (network stubbed)."""

from __future__ import annotations

import shutil
from datetime import date

import pandas as pd

import run_backfill
from src.data import universe_source
from src.db import init_db
from src.regime import compute_regime
from tests.conftest import FIXTURE_DB_PATH
from tests.e2e.conftest import install_stubs


def _screener(fix) -> pd.DataFrame:
    rows = fix.execute("SELECT ticker, name, sector, industry FROM tickers WHERE ticker NOT LIKE 'XL%' "
                       "AND ticker != 'SPY'").fetchall()
    return pd.DataFrame([{
        "ticker": r["ticker"], "name": r["name"], "exchange": "NASDAQ", "sector": r["sector"],
        "industry": r["industry"], "sic": None, "market_cap": 1e9 + i * 1e7, "price": 50.0,
        "cik": f"{i + 1:010d}"} for i, r in enumerate(rows)])


def test_trial_backfill_tickers_prices_regime_and_resume(tmp_path, monkeypatch):
    calls = install_stubs(monkeypatch)
    frame = _screener(calls.fix)
    monkeypatch.setattr(universe_source, "get_universe_source", lambda cache_dir, client: (frame, "nasdaq"))
    from src.config import load_config

    cfg = load_config(base_dir=tmp_path)
    out: list[str] = []
    db = tmp_path / "data" / "research_trial.db"
    stages = ["tickers", "prices", "regime"]
    kw = dict(stages=stages, limit=12, start=date(2022, 1, 3), end=date(2024, 12, 31), db_path=db, emit=out.append)
    assert run_backfill.run(cfg, **kw) == 0

    conn = init_db(db)
    n_tickers = conn.execute("SELECT COUNT(*) FROM tickers WHERE cik IS NOT NULL").fetchone()[0]
    assert n_tickers == 12  # --limit: only the 12 largest
    assert conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0] == 12 + 12  # + SPY + 11 ETFs
    assert conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0] >= 0
    assert conn.execute("SELECT COUNT(*) FROM regime_log").fetchone()[0] > 500
    assert any("ETA" in line for line in out), "progress lines must show an ETA"
    first_calls = len(calls.prices)
    conn.close()

    # resumable: a second run finds everything present and downloads nothing
    out.clear()
    assert run_backfill.run(cfg, **kw) == 0
    assert len(calls.prices) == first_calls
    assert any("0 missing, 0 stale" in line for line in out)


def test_vectorised_regime_matches_compute_regime(tmp_path):
    db = tmp_path / "fixture_copy.db"
    shutil.copyfile(FIXTURE_DB_PATH, db)
    conn = init_db(db)
    days = [date(2024, 3, 15), date(2024, 6, 14), date(2024, 9, 13), date(2024, 11, 22), date(2024, 12, 31)]
    expected = {d: compute_regime(conn, d) for d in days}
    conn.execute("DELETE FROM regime_log")
    conn.commit()
    from src.config import load_config

    run_backfill.stage_regime(conn, load_config(), date(2024, 1, 2), date(2024, 12, 31), emit=lambda m: None)
    for d, exp in expected.items():
        got = dict(conn.execute("SELECT * FROM regime_log WHERE date = ?", (d.isoformat(),)).fetchone())
        assert got["regime"] == exp["regime"], d
        assert abs(got["breadth_pct"] - exp["breadth_pct"]) < 1e-6, d
        assert abs(got["spy_sma200"] - exp["spy_sma200"]) < 1e-6, d
