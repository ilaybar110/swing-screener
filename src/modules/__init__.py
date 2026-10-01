"""Strategy modules (owned by Bot 4): src/modules/a_momentum_pullback.py,
b_earnings_gap_drift.py, c_insider_cluster.py, d_base_breakout.py, plus
get_enabled_modules() here. Each implements src.contracts.StrategyModule.
"""

from __future__ import annotations

from src.contracts import StrategyModule
from src.modules.a_momentum_pullback import MomentumPullback
from src.modules.b_earnings_gap_drift import EarningsGapDrift
from src.modules.c_insider_cluster import InsiderCluster
from src.modules.d_base_breakout import BaseBreakout

# config.modules.<key> -> implementation
MODULE_CLASSES: dict[str, type] = {
    "a_momentum_pullback": MomentumPullback,
    "b_earnings_gap_drift": EarningsGapDrift,
    "c_insider_cluster": InsiderCluster,
    "d_base_breakout": BaseBreakout,
}


def get_enabled_modules(config) -> list[StrategyModule]:
    """get_enabled_modules(config) -> list[StrategyModule]. Instantiates the
    modules whose config.modules.<name>.enabled is true."""
    return [
        cls(config)
        for key, cls in MODULE_CLASSES.items()
        if getattr(config.modules, key).enabled
    ]
