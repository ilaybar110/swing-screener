"""Typed access to config.yaml plus secrets from environment variables.

Load once via load_config() and pass the resulting Config object down; do not
re-read config.yaml from inside modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR_ENV = "SWING_BASE_DIR"


def data_root() -> Path:
    """Directory that holds state/, reports/, data/ and work/: the repo root, unless the
    SWING_BASE_DIR environment variable (the --base-dir flag of the entrypoints) points
    elsewhere -- used for trial runs against a temporary copy of the state."""
    override = os.environ.get(BASE_DIR_ENV)
    return Path(override).resolve() if override else REPO_ROOT


class PathsConfig(BaseModel):
    run_db: Path
    research_db: Path
    state_dir: Path
    reports_dir: Path
    raw_cache_dir: Path
    logs_dir: Path


class DailyConfig(BaseModel):
    max_catchup_days: int


class EdgarConfig(BaseModel):
    max_requests_per_second: float
    daily_index_lookback_days: int


class PricesConfig(BaseModel):
    live_lookback_days: int
    research_start_date: str
    retry_pause_seconds: int
    chunk_size: int


class UniverseConfig(BaseModel):
    min_price: float
    min_market_cap: float
    min_adv20_dollars: float
    min_history_days: int
    exclude_sic_codes: list[int]
    exclude_name_patterns: list[str]


class RegimeConfig(BaseModel):
    spy_sma_days: int
    breadth_sma_days: int
    breadth_threshold_pct: float


class ModuleAConfig(BaseModel):
    enabled: bool
    rs_lookback_days: int
    rs_skip_recent_days: int
    rs_top_pct: float
    pct_of_52w_high_max: float
    sma_fast: int
    sma_slow: int
    pullback_touch_window: int = 5
    pullback_touch_pct: float = 0.02
    pullback_close_floor: float = 0.97
    sma_slope_sessions: int = 10
    stop_atr_buffer: float = 0.1
    max_pullback_volume_ratio: float = 0.8
    max_pullback_depth_atr: float = 3.5
    min_close_location: float = 0.7


class ModuleBConfig(BaseModel):
    enabled: bool
    min_gap_pct: float
    min_volume_multiple: float
    hold_days_min: int
    hold_days_max: int
    tight_range_atr_multiple: float = 1.5
    stop_atr_buffer: float = 0.1
    gap_close_min_range_position: float = 0.5
    prefer_8k_window_days: int = 3
    pullback_trigger_enabled: bool = True
    pullback_window_sessions: int = 30
    pullback_sma_days: list[int] = Field(default_factory=lambda: [10, 20])
    pullback_touch_pct: float = 0.02
    surprise_max_points: float = 10.0
    surprise_eps_saturation: float = 0.20
    surprise_sue_saturation: float = 2.0


class ModuleCConfig(BaseModel):
    enabled: bool
    min_distinct_insiders: int
    min_txn_value: float
    min_cluster_total_value: float
    cluster_window_days: int
    sma_confirm_days: int
    atr_stop_multiple: float
    completion_window_sessions: int = 20
    stop_lookback_sessions: int = 10
    cooldown_days: int = 90


class ModuleDConfig(BaseModel):
    enabled: bool
    pct_of_52w_high_max: float
    base_min_weeks: int
    base_max_weeks: int
    min_volume_multiple: float
    atr_stop_multiple: float
    contraction_ratio: float = 0.7
    trend_template: bool = True
    trend_sma_mid: int = 150
    trend_sma_long: int = 200
    trend_sma_slope_sessions: int = 20
    max_base_depth_pct: float = 0.25


class ModulesConfig(BaseModel):
    a_momentum_pullback: ModuleAConfig
    b_earnings_gap_drift: ModuleBConfig
    c_insider_cluster: ModuleCConfig
    d_base_breakout: ModuleDConfig
    stop_distance_pct_min: float
    stop_distance_pct_max: float


class GuardrailConfig(BaseModel):
    shares_growth_yoy_max: float
    debt_to_op_income_max: float


class RankingConfig(BaseModel):
    weight_setup_quality: float
    weight_relative_strength: float
    weight_overlap: float
    overlap_score_1_module: int
    overlap_score_2_modules: int
    overlap_score_3plus_modules: int
    track_record_min_closed_trades: int
    track_record_weight_once_unlocked: float
    max_per_industry: int
    earnings_window_trading_days: int
    min_setup_score: float = 20.0
    min_slots_per_module: int = 1
    stopout_cooldown_sessions: int = 10


class TradePlanConfig(BaseModel):
    target_r_multiple: float
    partial_exit_fraction: float
    time_stop_trading_days: int
    entry_validity_trading_days: int
    exit_sma_days: int


class EarningsConfig(BaseModel):
    """Upcoming-earnings policy for new recommendations (src/trade_plan.py)."""

    policy: Literal["skip", "flag"] = "skip"
    min_sessions_before: int = 10


class TrackingConfig(BaseModel):
    round_trip_cost_pct: float
    sector_etfs: dict[str, str]


class MeasurementConfig(BaseModel):
    """Measurement / statistics settings (fixed-horizon forward returns, bootstrap
    confidence intervals, evidence labels). Every field has a default so older
    config files without a ``measurement`` section still load."""

    forward_horizons: list[int] = Field(default_factory=lambda: [5, 10, 20, 40, 60])
    bootstrap_iterations: int = 2000
    bootstrap_seed: int = 20240101
    ci_level: float = 0.95
    min_n_for_evidence: int = 30       # observations needed before an evidence label is given
    min_weeks_for_evidence: int = 4    # distinct signal weeks (bootstrap clusters) needed too


class BaselineConfig(BaseModel):
    samples_per_recommendation: int
    random_seed: int


class ReportConfig(BaseModel):
    top_n_favorable: int
    top_n_caution: int
    top_n_unfavorable: int
    max_per_industry: int


class LLMConfig(BaseModel):
    max_input_tokens_per_stock: int
    briefs_top_n: int


class SecretsConfig(BaseModel):
    sec_email: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


class Config(BaseModel):
    paths: PathsConfig
    daily: DailyConfig
    edgar: EdgarConfig
    prices: PricesConfig
    universe: UniverseConfig
    regime: RegimeConfig
    modules: ModulesConfig
    guardrail: GuardrailConfig
    ranking: RankingConfig
    trade_plan: TradePlanConfig
    earnings: EarningsConfig = Field(default_factory=EarningsConfig)
    tracking: TrackingConfig
    baseline: BaselineConfig
    measurement: MeasurementConfig = Field(default_factory=MeasurementConfig)
    report: ReportConfig
    llm: LLMConfig
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    def resolve_paths(self, base_dir: Path) -> None:
        """Make all configured paths absolute, rooted at base_dir, in place."""
        for field_name in PathsConfig.model_fields:
            value: Path = getattr(self.paths, field_name)
            if not value.is_absolute():
                setattr(self.paths, field_name, base_dir / value)


def load_config(path: Optional[Path] = None, base_dir: Optional[Path] = None) -> Config:
    """Load config.yaml and merge in secrets from environment variables.

    path: override location of config.yaml (defaults to <repo root>/config.yaml).
    base_dir: root that relative paths in the `paths` section are resolved against
        (defaults to the repo root). Pass a temp dir in tests to sandbox file output.
    """
    config_path = path or (REPO_ROOT / "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw["secrets"] = {
        "sec_email": os.environ.get("SEC_EMAIL"),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }

    config = Config.model_validate(raw)
    config.resolve_paths(base_dir or data_root())
    return config
