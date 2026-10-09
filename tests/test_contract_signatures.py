"""Audit: every cross-bot function named in src/contracts.py exists in its owning
module with the contracted parameters (same names, same order; extra trailing
parameters must be optional)."""
from __future__ import annotations

import importlib
import inspect

import pytest

from src import contracts

# contract stub -> (module, attribute)
MAPPING = {
    "_bot1_update_prices": ("src.data.prices", "update_prices"),
    "_bot1_update_splits": ("src.data.prices", "update_splits"),
    "_bot1_refresh_universe": ("src.universe", "refresh_universe"),
    "_bot1_build_historical_universe": ("src.universe", "build_historical_universe"),
    "_bot1_compute_regime": ("src.regime", "compute_regime"),
    "_bot1_update_upcoming_earnings": ("src.data.earnings", "update_upcoming_earnings"),
    "_bot2_fetch_company": ("src.data.fundamentals", "fetch_company"),
    "_bot2_backfill_bulk": ("src.data.fundamentals", "backfill_bulk"),
    "_bot2_update_summary": ("src.data.fundamentals", "update_summary"),
    "_bot2_guardrail_evaluate": ("src.guardrail", "evaluate"),
    "_bot3_process_day": ("src.data.edgar_daily", "process_day"),
    "_bot3_backfill": ("src.data.edgar_bulk", "backfill"),
    "_bot3_get_8k_texts": ("src.data.texts", "get_8k_texts"),
    "_bot3_get_news": ("src.data.news", "get_news"),
    "_bot4_get_enabled_modules": ("src.modules", "get_enabled_modules"),
    "_bot4_rank": ("src.ranking", "rank"),
    # contract says build(candidate, ...) -> dict; the dict-shaped function is plan_levels
    "_bot4_trade_plan_build": ("src.trade_plan", "plan_levels"),
    "_bot5_simulate": ("src.tracker", "simulate"),
    "_bot5_update_all": ("src.tracker", "update_all"),
    "_bot5_create_for": ("src.baseline", "create_for"),
    "_bot5_stats_summary": ("src.stats", "summary"),
    "_bot5_backtest_run": ("backtest.runner", "run"),
    "_bot6_write_inputs": ("src.llm.brief_io", "write_inputs"),
    "_bot6_merge_outputs": ("src.llm.brief_io", "merge_outputs"),
    "_bot6_report_build": ("src.report", "build"),
    "_bot6_telegram_send_report": ("notify.telegram", "send_report"),
    "_bot6_telegram_send_alert": ("notify.telegram", "send_alert"),
}


def test_mapping_covers_every_contract_stub():
    stubs = {n for n in dir(contracts) if n.startswith("_bot") and callable(getattr(contracts, n))}
    assert stubs == set(MAPPING)


@pytest.mark.parametrize("stub", sorted(MAPPING))
def test_signature_matches_contract(stub):
    mod_name, attr = MAPPING[stub]
    fn = getattr(importlib.import_module(mod_name), attr)
    want = list(inspect.signature(getattr(contracts, stub)).parameters.values())
    got = list(inspect.signature(fn).parameters.values())
    assert [p.name for p in got[: len(want)]] == [p.name for p in want]
    for p in got[len(want):]:
        assert p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD), (
            f"{mod_name}.{attr}: extra parameter {p.name} must be optional"
        )
    # defaults the contract declares must be preserved
    for w, g in zip(want, got):
        if w.default is not inspect.Parameter.empty:
            assert g.default == w.default
