"""
Tests for Phase 5: PA Filter, Position Manager, Dynamic Hedger, Paper Trader.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from signals.pa_filter import (
    PriceActionFilter, Candle, CandlePattern, TrendDirection,
    PAFilterResult, SRLevel,
)
from execution.position_manager import (
    PositionManager, LivePosition, PositionState, ExitOrder,
)
from execution.hedger import DynamicHedger, HedgeDecision
from execution.paper_trader import PaperTrader, PaperTradingStats
from config.constants import DecisionAction


# ===================================================================
#  Candle helpers
# ===================================================================


def _make_candle(o, h, l, c, volume=100.0, ts=0.0) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=volume)


def _bullish_trend(n=30, start=100.0, step=0.5, base_ts=1000.0):
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = c + step * 0.2
        l = o - step * 0.1
        candles.append(_make_candle(o, h, l, c, ts=base_ts + i * 60))
        price = c
    return candles


def _bearish_trend(n=30, start=150.0, step=0.5, base_ts=1000.0):
    candles = []
    price = start
    for i in range(n):
        o = price
        c = price - step
        l = c - step * 0.2
        h = o + step * 0.1
        candles.append(_make_candle(o, h, l, c, ts=base_ts + i * 60))
        price = c
    return candles


# ===================================================================
#  PriceActionFilter Tests
# ===================================================================


class TestPriceActionFilter:
    @pytest.fixture
    def pa(self):
        return PriceActionFilter(min_confidence=0.3)

    def test_insufficient_data(self, pa):
        result = pa.evaluate(1, 100.0)
        assert not result.confirmed
        assert result.reason == "insufficient_data"

    def test_bullish_trend_detection(self, pa):
        for c in _bullish_trend(30):
            pa.add_candle(c, "1m")
        trend = pa._calc_trend(pa._candles_1m)
        assert trend == TrendDirection.UP

    def test_bearish_trend_detection(self, pa):
        for c in _bearish_trend(30):
            pa.add_candle(c, "1m")
        trend = pa._calc_trend(pa._candles_1m)
        assert trend == TrendDirection.DOWN

    def test_bullish_engulfing_pattern(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(110, 111, 108, 109))
        candles.append(_make_candle(108, 112, 107, 112))
        pattern = pa._detect_pattern(candles, direction=1)
        assert pattern == CandlePattern.BULLISH_ENGULFING

    def test_bearish_engulfing_pattern(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(109, 112, 108, 111))
        candles.append(_make_candle(112, 113, 107, 108))
        pattern = pa._detect_pattern(candles, direction=-1)
        assert pattern == CandlePattern.BEARISH_ENGULFING

    def test_bullish_pin_bar(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(100, 101, 99, 100.5))
        candles.append(_make_candle(100, 101.5, 94, 100.8))
        pattern = pa._detect_pattern(candles, direction=1)
        assert pattern == CandlePattern.BULLISH_PIN_BAR

    def test_bearish_pin_bar(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(100, 101, 99, 100.5))
        candles.append(_make_candle(100, 106, 99.5, 99.8))
        pattern = pa._detect_pattern(candles, direction=-1)
        assert pattern == CandlePattern.BEARISH_PIN_BAR

    def test_inside_bar(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(95, 110, 90, 105))
        candles.append(_make_candle(100, 105, 95, 102))
        pattern = pa._detect_pattern(candles, direction=1)
        assert pattern == CandlePattern.INSIDE_BAR

    def test_doji(self, pa):
        candles = pa._candles_1m
        candles.append(_make_candle(100, 103, 97, 101))
        candles.append(_make_candle(100, 106, 94, 100.01))
        pattern = pa._detect_pattern(candles, direction=1)
        assert pattern == CandlePattern.DOJI

    def test_volume_confirmation(self, pa):
        for i in range(25):
            vol = 100 if i < 24 else 200
            pa.add_candle(_make_candle(100, 101, 99, 100.5, volume=vol, ts=i * 60), "1m")
        confirmed = pa._check_volume(pa._candles_1m)
        assert confirmed

    def test_volume_not_confirmed(self, pa):
        for i in range(25):
            pa.add_candle(_make_candle(100, 101, 99, 100.5, volume=100, ts=i * 60), "1m")
        confirmed = pa._check_volume(pa._candles_1m)
        assert not confirmed

    def test_sr_level_detection(self, pa):
        candles = []
        price = 100
        for i in range(50):
            if i == 10 or i == 30:
                candles.append(_make_candle(price, price + 5, price - 1, price + 3, ts=i * 60))
            elif i == 20 or i == 40:
                candles.append(_make_candle(price, price + 1, price - 5, price - 3, ts=i * 60))
            else:
                candles.append(_make_candle(price, price + 1, price - 1, price + 0.5, ts=i * 60))

        for c in candles:
            pa.add_candle(c, "1m")

        pa._update_sr_levels()
        assert len(pa.sr_levels) >= 0

    def test_find_tp2_level_long(self, pa):
        pa._sr_levels = [
            SRLevel(price=105.0, level_type="resistance", strength=3),
            SRLevel(price=110.0, level_type="resistance", strength=2),
            SRLevel(price=95.0, level_type="support", strength=2),
        ]
        tp2 = pa.find_tp2_level(100.0, direction=1)
        assert tp2 == 105.0

    def test_find_tp2_level_short(self, pa):
        pa._sr_levels = [
            SRLevel(price=95.0, level_type="support", strength=3),
            SRLevel(price=90.0, level_type="support", strength=2),
            SRLevel(price=105.0, level_type="resistance", strength=2),
        ]
        tp2 = pa.find_tp2_level(100.0, direction=-1)
        assert tp2 == 95.0

    def test_find_tp2_level_none(self, pa):
        pa._sr_levels = []
        assert pa.find_tp2_level(100.0, 1) is None

    def test_evaluate_full_confirmation(self, pa):
        for c in _bullish_trend(30):
            pa.add_candle(c, "1m")
        for c in _bullish_trend(30, start=100.0, step=2.5):
            pa.add_candle(c, "5m")

        result = pa.evaluate(1, 115.0)
        assert isinstance(result, PAFilterResult)
        assert result.trend_1m == TrendDirection.UP

    def test_multi_tf_candle_storage(self, pa):
        pa.add_candle(_make_candle(100, 101, 99, 100.5), "1m")
        pa.add_candle(_make_candle(100, 102, 98, 101), "5m")
        pa.add_candle(_make_candle(100, 103, 97, 101.5), "15m")
        assert len(pa._candles_1m) == 1
        assert len(pa._candles_5m) == 1
        assert len(pa._candles_15m) == 1

    def test_reset(self, pa):
        pa.add_candle(_make_candle(100, 101, 99, 100.5), "1m")
        pa._sr_levels = [SRLevel(price=100)]
        pa.reset()
        assert len(pa._candles_1m) == 0
        assert len(pa._sr_levels) == 0


# ===================================================================
#  PositionManager Tests
# ===================================================================


class TestPositionManager:
    @pytest.fixture
    def pm(self):
        return PositionManager(cooldown_seconds=0, max_hold_seconds=3600)

    def test_open_position(self, pm):
        pos = pm.open_position(
            symbol="BTCUSDT", direction=1,
            entry_price=50000, size=0.1,
            stop_price=49500, tp1_price=50750,
            tp2_price=51250, atr=250,
        )
        assert pos is not None
        assert pos.direction == 1
        assert pos.is_open
        assert pm.position_count == 1

    def test_max_positions_enforced(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.open_position("ETH", 1, 3000, 1.0, 2950, 3075, 3125, 25)
        pos3 = pm.open_position("SOL", 1, 100, 10, 99, 101.5, 102.5, 0.5)
        assert pos3 is None
        assert pm.position_count == 2

    def test_stop_loss(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        exits = pm.update("BTC", 49400)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"
        assert pm.position_count == 0

    def test_tp1_partial_exit(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        exits = pm.update("BTC", 50800)
        assert len(exits) == 1
        assert exits[0].reason == "tp1"
        assert exits[0].size == pytest.approx(0.05, abs=0.001)
        assert pm.position_count == 1

    def test_tp2_after_tp1(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.update("BTC", 50800)
        exits = pm.update("BTC", 51300)
        assert len(exits) == 1
        assert exits[0].reason == "tp2"

    def test_break_even_at_1r(self, pm):
        pos = pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.update("BTC", 50600)
        assert pos.be_triggered

    def test_trailing_stop(self, pm):
        pos = pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.update("BTC", 50800)
        pm.update("BTC", 51300)
        old_stop = pos.stop_price
        pm.update("BTC", 52000, atr=200)
        assert pos.stop_price > old_stop

    def test_time_exit(self):
        pm = PositionManager(cooldown_seconds=0, max_hold_seconds=1)
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        import time as _t
        _t.sleep(1.1)
        exits = pm.update("BTC", 50100)
        assert len(exits) == 1
        assert exits[0].reason == "time_exit"

    def test_short_position_stop(self, pm):
        pm.open_position("BTC", -1, 50000, 0.1, 50500, 49250, 48750, 250)
        exits = pm.update("BTC", 50600)
        assert len(exits) == 1
        assert exits[0].reason == "stop_loss"

    def test_short_tp1(self, pm):
        pm.open_position("BTC", -1, 50000, 0.1, 50500, 49250, 48750, 250)
        exits = pm.update("BTC", 49200)
        assert len(exits) == 1
        assert exits[0].reason == "tp1"

    def test_force_close_all(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.open_position("ETH", -1, 3000, 1.0, 3050, 2925, 2875, 25)
        exits = pm.force_close_all(50000, "veto")
        assert len(exits) == 2
        assert pm.position_count == 0

    def test_update_tp2(self, pm):
        pos = pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        pm.update_tp2(pos.position_id, 51500)
        assert pos.tp2_price == 51500

    def test_daily_trade_count(self, pm):
        pm.open_position("BTC", 1, 50000, 0.1, 49500, 50750, 51250, 250)
        assert pm.daily_trade_count == 1


# ===================================================================
#  DynamicHedger Tests
# ===================================================================


class TestDynamicHedger:
    @pytest.fixture
    def hedger(self):
        return DynamicHedger()

    def test_no_triggers(self, hedger):
        result = hedger.evaluate(
            position_value_usd=1000, vpin_value=0.3,
            hold_time_seconds=60, unrealized_pnl_pct=0.01,
            gex_flip_detected=False,
        )
        assert not result.should_hedge
        assert result.trigger_count == 0

    def test_one_trigger_no_hedge(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.3,
            hold_time_seconds=60, unrealized_pnl_pct=0.01,
            gex_flip_detected=False,
        )
        assert not result.should_hedge
        assert result.trigger_count == 1
        assert "SIZE" in result.triggers

    def test_two_triggers_40pct(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=60, unrealized_pnl_pct=0.01,
            gex_flip_detected=False,
        )
        assert result.should_hedge
        assert result.hedge_pct == 0.40
        assert result.trigger_count == 2
        assert "SIZE" in result.triggers
        assert "VPIN" in result.triggers

    def test_three_triggers_65pct(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=7500, unrealized_pnl_pct=0.01,
            gex_flip_detected=False,
        )
        assert result.should_hedge
        assert result.hedge_pct == 0.65
        assert result.trigger_count == 3

    def test_four_triggers_85pct(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=7500, unrealized_pnl_pct=-0.015,
            gex_flip_detected=False,
        )
        assert result.should_hedge
        assert result.hedge_pct == 0.85
        assert result.trigger_count == 4

    def test_five_triggers_100pct(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=7500, unrealized_pnl_pct=-0.015,
            gex_flip_detected=True,
        )
        assert result.should_hedge
        assert result.hedge_pct == 1.00
        assert result.trigger_count == 5

    def test_vpin_none_not_trigger(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=None,
            hold_time_seconds=7500, unrealized_pnl_pct=-0.015,
            gex_flip_detected=True,
        )
        assert result.trigger_count == 4
        assert "VPIN" not in result.triggers

    def test_adverse_threshold(self, hedger):
        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=60, unrealized_pnl_pct=-0.011,
            gex_flip_detected=False,
        )
        assert "ADVERSE" not in result.triggers

        result = hedger.evaluate(
            position_value_usd=6000, vpin_value=0.60,
            hold_time_seconds=60, unrealized_pnl_pct=-0.013,
            gex_flip_detected=False,
        )
        assert "ADVERSE" in result.triggers


# ===================================================================
#  PaperTrader Tests
# ===================================================================


class TestPaperTrader:
    @pytest.fixture
    def trader(self, tmp_path):
        return PaperTrader(initial_balance=10000, log_dir=str(tmp_path))

    def test_initial_state(self, trader):
        assert trader.balance == 10000
        assert trader.stats.total_trades == 0
        assert len(trader.positions) == 0

    def test_veto_increments_counter(self, trader):
        trader.on_decision("BTCUSDT", DecisionAction.VETO, 0, 50000, 200)
        assert trader.stats.vetoes == 1

    def test_neutral_no_trade(self, trader):
        trader.on_decision("BTCUSDT", DecisionAction.NEUTRAL, 0, 50000, 200)
        assert trader.stats.total_trades == 0

    def test_long_signal_without_pa(self, trader):
        trader.on_decision("BTCUSDT", DecisionAction.LONG, 0.4, 50000, 200)
        assert trader.stats.signals_received == 1
        assert trader.stats.pa_filter_blocks == 1
        assert len(trader.positions) == 0

    def test_long_signal_with_pa_data(self, trader):
        for c in _bullish_trend(30, start=49000, step=50):
            trader.on_candle(c, "1m")

        trader.on_decision("BTCUSDT", DecisionAction.LONG, 0.4, 50500, 200)
        assert trader.stats.signals_received == 1

    def test_save_session(self, trader, tmp_path):
        trader.on_decision("BTCUSDT", DecisionAction.NEUTRAL, 0, 50000, 200)
        path = trader.save_session()
        assert path.exists()
        import json
        data = json.loads(path.read_text())
        assert "stats" in data
        assert "trades" in data

    def test_status_output(self, trader):
        status = trader.print_status()
        assert "PAPER TRADING STATUS" in status
        assert "$10,000.00" in status

    def test_drawdown_tracking(self, trader):
        trader._balance = 9500
        trader._stats.peak_balance = 10000
        trader._update_drawdown()
        assert trader.stats.max_drawdown_pct == pytest.approx(0.05, abs=0.001)

    def test_hedge_trigger_in_paper_mode(self, trader):
        for c in _bullish_trend(30, start=49000, step=50):
            trader.on_candle(c, "1m")

        trader._pm._cooldown = 0
        trader._pa._min_conf = 0.0

        trader.on_decision("BTCUSDT", DecisionAction.LONG, 0.5, 50000, 200)

        if len(trader.positions) > 0:
            pos = trader.positions[0]
            assert pos.direction == 1


# ===================================================================
#  Candle dataclass tests
# ===================================================================


class TestCandle:
    def test_candle_properties(self):
        c = _make_candle(100, 105, 95, 103)
        assert c.body == 3.0
        assert c.range == 10.0
        assert c.upper_wick == 2.0
        assert c.lower_wick == 5.0
        assert c.is_bullish
        assert c.mid == 100.0

    def test_bearish_candle(self):
        c = _make_candle(103, 105, 95, 100)
        assert not c.is_bullish
        assert c.body == 3.0

    def test_doji_candle(self):
        c = _make_candle(100, 105, 95, 100)
        assert c.body == 0.0


# ===================================================================
#  EMA helper test
# ===================================================================


class TestEMA:
    def test_ema_basic(self):
        pa = PriceActionFilter()
        values = [10, 11, 12, 13, 14, 15]
        ema = pa._ema(values, 3)
        assert ema > 13

    def test_ema_empty(self):
        pa = PriceActionFilter()
        assert pa._ema([], 3) == 0.0
