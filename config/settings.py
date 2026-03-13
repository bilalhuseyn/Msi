from __future__ import annotations

import functools
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class OBISettings(BaseSettings):
    depth: int = Field(10)
    bullish_threshold: float = Field(0.65)
    bearish_threshold: float = Field(0.35)
    consistency_window: int = Field(3)
    ma_window: int = Field(10)


class VPINSettings(BaseSettings):
    bucket_size: float = Field(500.0)
    window: int = Field(50)
    veto_threshold: float = Field(0.65)
    warning_threshold: float = Field(0.55)
    spike_filter_pct: float = Field(0.30)
    min_buckets: int = Field(50)  # Reduced from 100: ~1h warm-up instead of 2h


class SpreadSettings(BaseSettings):
    baseline_window: int = Field(1440)
    mm_active_ratio: float = Field(1.20)
    mm_cautious_ratio: float = Field(1.80)
    mm_thinning_ratio: float = Field(3.00)


class OptionsSettings(BaseSettings):
    update_interval_sec: int = Field(300)
    gex_flip_momentum_factor: float = Field(0.30)
    short_gamma_multiplier: float = Field(1.20)
    long_gamma_multiplier: float = Field(0.85)
    flip_risk_multiplier: float = Field(0.60)
    extreme_call_pcr: float = Field(0.50)
    extreme_put_pcr: float = Field(2.00)
    pcr_score_bonus: float = Field(0.10)
    gex_history_window: int = Field(14)
    btc_contract_size: float = Field(0.1)


class DepthErosionSettings(BaseSettings):
    check_interval: int = Field(60)
    erosion_threshold: float = Field(0.35)
    price_stability: float = Field(0.003)
    depth: int = Field(10)


class SpoofingSettings(BaseSettings):
    size_threshold: float = Field(50.0)
    cancel_window_ms: int = Field(800)


class ClearanceSettings(BaseSettings):
    one_sided_threshold: float = Field(0.72)
    ask_slide_pct: float = Field(0.0015)  # P8: raised from 0.0008 to reduce false triggers
    large_trade_multiplier: float = Field(4.0)
    large_trade_min_cluster: int = Field(3)
    bid_thin_pct: float = Field(0.55)
    ob_history_depth: int = Field(5)
    recent_trade_window: int = Field(100)


class BacktestSettings(BaseSettings):
    taker_fee_pct: float = Field(0.001)
    slippage_pct: float = Field(0.0005)
    initial_balance: float = Field(10_000.0)
    train_ratio: float = Field(0.70)
    monte_carlo_iterations: int = Field(1000)
    max_hold_hours: float = Field(4.0)
    stop_atr_multiplier: float = Field(1.5)
    tp1_rr: float = Field(1.5)
    tp1_exit_pct: float = Field(0.50)
    tp2_rr: float = Field(2.5)  # Placeholder — Phase 5 replaces with PA Filter S/R detection
    tp2_exit_pct: float = Field(0.30)
    tp3_trailing_atr: float = Field(1.5)  # PRD spec: ATR × 1.5
    min_stop_pct: float = Field(0.005)
    max_stop_risk_pct: float = Field(0.015)
    cooldown_seconds: int = Field(900)
    options_regime: str = Field("NEUTRAL")  # LONG_GAMMA | SHORT_GAMMA | NEUTRAL
    sample_interval_sec: int = Field(1)  # P14: seconds between ticks; >1 triggers window auto-scaling


class MomentumSettings(BaseSettings):
    rsi_period: int = Field(14)
    ema_fast: int = Field(8)
    ema_slow: int = Field(21)
    rsi_ob: float = Field(70.0)
    rsi_os: float = Field(30.0)


class VolumeProfileSettings(BaseSettings):
    profile_window: int = Field(96)
    value_area_pct: float = Field(0.70)
    num_bins: int = Field(50)


class HTFTrendSettings(BaseSettings):
    aggregation_factor: int = Field(16)
    adx_period: int = Field(14)
    strong_trend: float = Field(25.0)
    weak_trend: float = Field(20.0)


class PAFilterSettings(BaseSettings):
    ema_fast: int = Field(8)
    ema_slow: int = Field(21)
    volume_multiplier: float = Field(1.5)
    sr_lookback: int = Field(100)
    sr_proximity_pct: float = Field(0.003)
    min_confidence: float = Field(0.4)


class HedgerSettings(BaseSettings):
    size_threshold_usd: float = Field(5000.0)
    vpin_threshold: float = Field(0.55)
    time_threshold_minutes: float = Field(120.0)
    adverse_pct: float = Field(0.012)


class RiskSettings(BaseSettings):
    base_risk_pct: float = Field(0.01)
    max_daily_loss_pct: float = Field(0.03)
    max_weekly_loss_pct: float = Field(0.07)
    max_position_pct: float = Field(0.10)
    max_open_positions: int = Field(2)
    max_daily_trades: int = Field(8)
    cooldown_minutes: int = Field(15)
    correlation_cap_threshold: float = Field(0.85)
    correlation_combined_cap: float = Field(1.5)
    consecutive_loss_reduce_at: int = Field(3)
    consecutive_loss_cooldown_at: int = Field(5)
    funding_rate_warning_annualized: float = Field(0.30)
    var_limit_pct: float = Field(0.02)

    drawdown_tiers: list[tuple[float, float]] = Field(
        default=[
            (0.01, 1.0),
            (0.02, 0.7),
            (0.025, 0.5),
            (0.03, 0.3),
        ]
    )


class ConfirmationSettings(BaseSettings):
    long_threshold: float = Field(0.35)
    short_threshold: float = Field(-0.35)
    weights: dict[str, float] = Field(
        default={
            "OBI": 0.15,
            "DEPTH": 0.15,
            "SPOOF": 0.00,
            "CLEARANCE": 0.15,
            "OPTIONS": 0.10,
            "SR_PROXIMITY": 0.15,
            "MOMENTUM": 0.10,
            "VOLUME_PROFILE": 0.10,
            "HTF_TREND": 0.10,
        }
    )


class BinanceSettings(BaseSettings):
    ws_base_url: str = "wss://stream.binance.com:9443/ws"
    rest_base_url: str = "https://api.binance.com"
    depth_levels: int = 20
    depth_update_ms: int = 100


class BybitSettings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    testnet: bool = Field(False, alias="BYBIT_TESTNET")
    ws_base_url: str = "wss://stream.bybit.com/v5/public/linear"
    rest_base_url: str = "https://api.bybit.com"
    orderbook_depth: int = 200

    @model_validator(mode="after")
    def _apply_testnet_urls(self) -> "BybitSettings":
        if self.testnet:
            self.ws_base_url = "wss://stream-testnet.bybit.com/v5/public/linear"
            self.rest_base_url = "https://api-testnet.bybit.com"
        return self


class DeribitSettings(BaseSettings):
    rest_base_url: str = "https://www.deribit.com"
    currency: str = "BTC"


class InfluxDBSettings(BaseSettings):
    url: str = Field("http://localhost:8086", alias="INFLUXDB_URL")
    token: str = Field("ofi-pro-dev-token", alias="INFLUXDB_TOKEN")
    org: str = Field("ofi", alias="INFLUXDB_ORG")
    bucket: str = Field("ofi_metrics", alias="INFLUXDB_BUCKET")


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    symbols: list[str] = Field(default=["BTCUSDT", "ETHUSDT"])
    loop_interval_ms: int = Field(100)

    obi: OBISettings = Field(default_factory=OBISettings)
    vpin: VPINSettings = Field(default_factory=VPINSettings)
    spread: SpreadSettings = Field(default_factory=SpreadSettings)
    depth_erosion: DepthErosionSettings = Field(default_factory=DepthErosionSettings)
    spoofing: SpoofingSettings = Field(default_factory=SpoofingSettings)
    clearance: ClearanceSettings = Field(default_factory=ClearanceSettings)
    options: OptionsSettings = Field(default_factory=OptionsSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    confirmation: ConfirmationSettings = Field(default_factory=ConfirmationSettings)
    pa_filter: PAFilterSettings = Field(default_factory=PAFilterSettings)
    hedger: HedgerSettings = Field(default_factory=HedgerSettings)
    binance: BinanceSettings = Field(default_factory=BinanceSettings)
    bybit: BybitSettings = Field(default_factory=BybitSettings)
    deribit: DeribitSettings = Field(default_factory=DeribitSettings)
    influxdb: InfluxDBSettings = Field(default_factory=InfluxDBSettings)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
