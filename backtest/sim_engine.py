from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from backtest.data_loader import BacktestTick
from backtest.fee_model import FeeModel
from backtest.metrics import PerformanceMetrics, calculate_metrics
from backtest.portfolio import Portfolio, TradeRecord

from config.constants import DecisionAction, RegimeType
from config.settings import (
    BacktestSettings,
    ClearanceSettings,
    ConfirmationSettings,
    DepthErosionSettings,
    OBISettings,
    OptionsSettings,
    SpoofingSettings,
    SpreadSettings,
    VPINSettings,
)
from signals.clearance_detector import ClearanceDetector
from signals.confirmation import ConfirmationEngine, Decision
from signals.depth_erosion import DepthErosionMonitor
from signals.htf_trend import HTFTrendModule
from signals.market_structure import Candle, MarketStructureReader
from signals.momentum import MomentumModule
from signals.obi import OBIModule
from signals.options_layer import OptionsLayer, RegimeInfo
from signals.spoofing_detector import SpoofingDetector
from signals.spread_monitor import SpreadMonitor
from signals.volume_profile import VolumeProfileModule
from signals.vpin import VPINModule

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Complete output of a backtest run."""

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[float, float]] = field(default_factory=list)
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    decisions: list[dict] = field(default_factory=list)
    initial_balance: float = 10_000.0
    final_balance: float = 10_000.0
    total_ticks: int = 0
    veto_count: int = 0
    signal_count: int = 0
    params: dict = field(default_factory=dict)


class BacktestEngine:
    """
    Replays historical data through signal modules, Confirmation Engine,
    and Market Structure reader, simulating trades with realistic fees/slippage.

    Decision flow per 15-minute candle:
      1. Aggregate OB snapshots within the candle window
      2. Signal modules process the aggregated OB state
      3. CE calculates weighted score + spread multiplier
      4. Market Structure reader confirms direction (final gate)
      5. Position opens only if CE + Structure agree

    ATR is calculated from real 15-min candle high-low ranges.
    """

    def __init__(
        self,
        obi_settings: OBISettings | None = None,
        vpin_settings: VPINSettings | None = None,
        spread_settings: SpreadSettings | None = None,
        depth_settings: DepthErosionSettings | None = None,
        spoof_settings: SpoofingSettings | None = None,
        clearance_settings: ClearanceSettings | None = None,
        confirmation_settings: ConfirmationSettings | None = None,
        backtest_settings: BacktestSettings | None = None,
        options_settings: OptionsSettings | None = None,
        candles_15m: list[Candle] | None = None,
    ):
        bt = backtest_settings or BacktestSettings()
        self._bt = bt

        # P14: auto-scale time-based windows when ticks are sub-sampled.
        # At sample_interval_sec=100 (every 100th snapshot ≈ 100s per tick):
        #   spread baseline: 1440 → 864 samples  (24h of real data)
        #   OBI MA:          10   → 6   samples  (~10 min of real data)
        #   clearance depth: 5   → 3   samples  (~5 min lookback)
        si = bt.sample_interval_sec
        if si > 1:
            _obi_s = (obi_settings or OBISettings()).model_copy(
                update={"ma_window": max(5, 600 // si)}
            )
            _spread_s = (spread_settings or SpreadSettings()).model_copy(
                update={"baseline_window": max(10, 86400 // si)}
            )
            _cl_ob_depth = max(3, 300 // si)
            logger.info(
                "P14 window scaling (sample_interval=%ds): "
                "obi_ma=%d spread_baseline=%d cl_ob_depth=%d",
                si,
                _obi_s.ma_window,
                _spread_s.baseline_window,
                _cl_ob_depth,
            )
        else:
            _obi_s = obi_settings
            _spread_s = spread_settings
            _cl_ob_depth = (clearance_settings or ClearanceSettings()).ob_history_depth

        self._obi = OBIModule(_obi_s)
        self._vpin = VPINModule(vpin_settings)
        self._spread = SpreadMonitor(_spread_s)
        self._options = OptionsLayer(options_settings)

        de_cfg = depth_settings or DepthErosionSettings()
        self._depth = DepthErosionMonitor(
            check_interval=de_cfg.check_interval,
            erosion_threshold=de_cfg.erosion_threshold,
            price_stability=de_cfg.price_stability,
            depth=de_cfg.depth,
        )

        sp_cfg = spoof_settings or SpoofingSettings()
        self._spoof = SpoofingDetector(
            size_threshold=sp_cfg.size_threshold,
            cancel_window_ms=sp_cfg.cancel_window_ms,
        )

        cl_cfg = clearance_settings or ClearanceSettings()
        self._clearance = ClearanceDetector(
            one_sided_threshold=cl_cfg.one_sided_threshold,
            ask_slide_pct=cl_cfg.ask_slide_pct,
            large_trade_multiplier=cl_cfg.large_trade_multiplier,
            large_trade_min_cluster=cl_cfg.large_trade_min_cluster,
            bid_thin_pct=cl_cfg.bid_thin_pct,
            ob_history_depth=_cl_ob_depth,  # P14: scaled
            recent_trade_window=cl_cfg.recent_trade_window,
        )

        self._ce = ConfirmationEngine(confirmation_settings)
        self._recent_trades: deque[dict] = deque(maxlen=300)
        self._regime = self._build_regime(bt.options_regime)

        self._ms = MarketStructureReader(swing_lookback=3, min_swings=4)
        self._momentum = MomentumModule()
        self._vol_profile = VolumeProfileModule()
        self._htf_trend = HTFTrendModule()
        self._candles_15m = candles_15m or []
        self._candle_idx = 0
        self._candle_atr: deque[float] = deque(maxlen=14)

    @staticmethod
    def _build_regime(regime_str: str) -> RegimeInfo | None:
        """Build a static RegimeInfo from the backtest config string."""
        if regime_str == "NEUTRAL":
            return None
        opt_cfg = OptionsSettings()
        if regime_str == "SHORT_GAMMA":
            return RegimeInfo(
                regime=RegimeType.SHORT_GAMMA,
                gex_value=-1.0,
                pcr_value=1.0,
                flip_risk=False,
                pcr_signal="NORMAL",
                multiplier=opt_cfg.short_gamma_multiplier,
                description="Backtest assumption: MM SHORT_GAMMA",
            )
        return RegimeInfo(
            regime=RegimeType.LONG_GAMMA,
            gex_value=1.0,
            pcr_value=1.0,
            flip_risk=False,
            pcr_signal="NORMAL",
            multiplier=opt_cfg.long_gamma_multiplier,
            description="Backtest assumption: MM LONG_GAMMA",
        )

    _EVAL_INTERVAL = 900  # 15 minutes in seconds

    def run(self, ticks: list[BacktestTick], initial_balance: float | None = None) -> BacktestResult:
        balance = initial_balance if initial_balance is not None else self._bt.initial_balance
        fee_model = FeeModel(
            taker_fee_pct=self._bt.taker_fee_pct,
            slippage_pct=self._bt.slippage_pct,
        )
        portfolio = Portfolio(
            initial_balance=balance,
            fee_model=fee_model,
            cooldown_seconds=self._bt.cooldown_seconds,
            max_hold_seconds=self._bt.max_hold_hours * 3600,
            tp1_exit_pct=self._bt.tp1_exit_pct,
            tp2_exit_pct=self._bt.tp2_exit_pct,
        )

        self._reset_modules()
        veto_count = 0
        signal_count = 0
        structure_blocked = 0
        eval_count = 0
        decisions_log: list[dict] = []

        self._candle_idx = 0
        self._candle_atr.clear()
        self._ms.reset()

        first_ts = ticks[0].timestamp if ticks else 0.0
        self._warm_up_structure(first_ts)
        last_eval_boundary = (int(first_ts) // self._EVAL_INTERVAL) * self._EVAL_INTERVAL

        for tick in ticks:
            mid = tick.mid_price
            if mid <= 0:
                continue

            self._advance_candles(tick.timestamp)
            atr = self._current_atr(mid)

            # --- Every tick: feed trades to VPIN + manage open positions ---
            for t in tick.trades:
                self._recent_trades.append(t)
                self._vpin.process_trade(t["price"], t["qty"], mid,
                                         timestamp=tick.timestamp)

            portfolio.update(mid, tick.timestamp, atr)

            # --- Only evaluate signals at 15-min boundaries ---
            current_boundary = (int(tick.timestamp) // self._EVAL_INTERVAL) * self._EVAL_INTERVAL
            if current_boundary <= last_eval_boundary:
                continue
            last_eval_boundary = current_boundary
            eval_count += 1

            # --- Signal evaluation (once per 15 minutes) ---
            obi_out = self._obi.update({"bids": tick.bids, "asks": tick.asks})
            vpin_out = self._vpin.update({})

            spread_out = self._spread.update({
                "best_bid": tick.best_bid, "best_ask": tick.best_ask,
            })

            spoof_out = self._spoof.update({
                "bids": tick.bids, "asks": tick.asks,
                "timestamp_ms": int(tick.timestamp * 1000),
            })

            depth_out = self._depth.update({
                "bids": tick.bids, "asks": tick.asks,
                "mid_price": mid, "timestamp": tick.timestamp,
                "spoof_active": spoof_out.metadata.get("is_active", False),
            })

            clearance_out = self._clearance.update({
                "bids": tick.bids, "asks": tick.asks,
                "recent_trades": list(self._recent_trades)[-200:],
            })

            options_out = self._options.update({
                "options_chain": getattr(tick, "options_chain", []),
                "spot_price": mid,
            })

            regime = self._options.last_regime or self._regime
            vpin_val = vpin_out.metadata.get("vpin")
            spread_status = spread_out.metadata.get("status", "MM_ACTIVE")

            signals = {
                "OBI": obi_out.metadata.get("obi_ma", float(obi_out.score)),  # P6: float gradient
                "OBI_HISTORY": self._obi.history[-10:],
                "SPREAD_SCORE": spread_out.metadata.get("score", 0),
                "DEPTH_SCORE": depth_out.score,
                "SPOOF_LIST": spoof_out.metadata.get("spoofs", []),
                "SPOOF_SCORE": spoof_out.score,
                "CLEARANCE_STATUS": clearance_out.metadata.get("status", "NORMAL"),
                "CLEARANCE_SCORE": clearance_out.score,
                "OPTIONS_SCORE": options_out.score,
                "SR_PROXIMITY": self._ms.sr_proximity_score(mid),
                "MOMENTUM": self._momentum.score,
                "VOLUME_PROFILE": self._vol_profile.score,
                "HTF_TREND": self._htf_trend.score,
                "_READY": {
                    "SR_PROXIMITY": self._ms.is_ready,
                    "MOMENTUM": self._momentum.is_ready,
                    "VOLUME_PROFILE": self._vol_profile.is_ready,
                    "HTF_TREND": self._htf_trend.is_ready,
                },
            }

            decision = self._ce.evaluate(signals, vpin_val, spread_status, regime)

            if decision.action == DecisionAction.VETO:
                veto_count += 1
                decisions_log.append(self._log_decision(tick, decision, "veto_block_entry"))
                continue

            if decision.action in (DecisionAction.LONG, DecisionAction.SHORT):
                signal_count += 1

                direction = decision.direction
                if not self._ms.allows_direction(direction):
                    structure_blocked += 1
                    trend = self._ms.trend.value
                    decisions_log.append(self._log_decision(
                        tick, decision, f"blocked:structure_{trend}"))
                    continue

                can_open, block_reason = portfolio.can_open(tick.timestamp)
                if can_open:
                    stop_dist = max(atr * self._bt.stop_atr_multiplier, mid * self._bt.min_stop_pct)
                    stop_price = mid - direction * stop_dist
                    tp1_price = mid + direction * stop_dist * self._bt.tp1_rr

                    sr_tp2 = self._ms.find_next_sr_level(mid, direction)
                    if sr_tp2 is not None:
                        tp2_price = sr_tp2
                    else:
                        tp2_price = mid + direction * stop_dist * self._bt.tp2_rr

                    risk_usd = balance * 0.01
                    size = risk_usd / stop_dist if stop_dist > 0 else 0
                    max_size = balance * 0.10 / mid if mid > 0 else 0
                    size = min(size, max_size)

                    if size > 0:
                        portfolio.open_position(
                            price=mid, size=size, direction=direction,
                            stop_price=stop_price, tp1_price=tp1_price,
                            tp2_price=tp2_price, timestamp=tick.timestamp,
                        )
                        decisions_log.append(self._log_decision(tick, decision, "opened"))
                    else:
                        decisions_log.append(self._log_decision(tick, decision, "size_zero"))
                else:
                    decisions_log.append(self._log_decision(tick, decision, f"blocked:{block_reason}"))

        final_closed = portfolio.force_close_all(
            ticks[-1].mid_price if ticks else 0, ticks[-1].timestamp if ticks else 0, "end_of_data",
        )

        all_trades = portfolio.trades
        eq_curve = portfolio.equity_curve
        metrics = calculate_metrics(all_trades, eq_curve, balance)
        metrics.veto_count = veto_count

        logger.info(
            "15-min evaluations: %d | Signals: %d | Structure blocked: %d (%d%%)",
            eval_count, signal_count, structure_blocked,
            int(structure_blocked / max(signal_count, 1) * 100),
        )

        return BacktestResult(
            trades=all_trades,
            equity_curve=eq_curve,
            metrics=metrics,
            decisions=decisions_log,
            initial_balance=balance,
            final_balance=portfolio.balance,
            total_ticks=len(ticks),
            veto_count=veto_count,
            signal_count=signal_count,
        )

    def _warm_up_structure(self, first_tick_ts: float = 0.0) -> None:
        """Feed candles that closed BEFORE the first tick to seed all candle-based modules."""
        if not self._candles_15m:
            return
        self._candle_idx = 0
        count = 0
        for i, c in enumerate(self._candles_15m):
            candle_close_ts = c.timestamp + 900
            if candle_close_ts > first_tick_ts:
                break
            self._ms.add_candle(c)
            self._momentum.add_candle(c.close)
            self._vol_profile.add_candle(c.high, c.low, c.close, c.volume)
            self._htf_trend.add_candle_15m(c.open, c.high, c.low, c.close)
            self._candle_atr.append(c.high - c.low)
            self._candle_idx = i + 1
            count += 1
        logger.info(
            "Module warm-up: %d/%d candles before first tick, trend=%s, ready=%s",
            count, len(self._candles_15m), self._ms.trend.value, self._ms.is_ready,
        )

    def _advance_candles(self, timestamp: float) -> None:
        """Feed candles whose 15-min window has closed before this tick to all candle-based modules."""
        while self._candle_idx < len(self._candles_15m):
            c = self._candles_15m[self._candle_idx]
            candle_close_ts = c.timestamp + 900
            if candle_close_ts > timestamp:
                break
            self._ms.add_candle(c)
            self._momentum.add_candle(c.close)
            self._vol_profile.add_candle(c.high, c.low, c.close, c.volume)
            self._htf_trend.add_candle_15m(c.open, c.high, c.low, c.close)
            self._candle_atr.append(c.high - c.low)
            self._candle_idx += 1

    def _current_atr(self, mid: float) -> float:
        """ATR from real 15-min candle ranges, falling back to percentage."""
        if self._candle_atr:
            return sum(self._candle_atr) / len(self._candle_atr)
        return mid * 0.005

    def _reset_modules(self) -> None:
        self._obi.reset()
        self._vpin.reset()
        self._spread.reset()
        self._depth.reset()
        self._spoof.reset()
        self._clearance.reset()
        self._options.reset()
        self._momentum.reset()
        self._vol_profile.reset()
        self._htf_trend.reset()
        self._recent_trades.clear()

    @staticmethod
    def _log_decision(tick: BacktestTick, decision: Decision, action_taken: str) -> dict:
        return {
            "timestamp": tick.timestamp,
            "mid_price": tick.mid_price,
            "decision_action": decision.action.value,
            "decision_score": round(decision.score, 4),
            "decision_reason": decision.reason,
            "action_taken": action_taken,
        }
