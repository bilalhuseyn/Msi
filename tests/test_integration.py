"""
Integration tests — validate the full pipeline from data through signals.
Uses mock data instead of live exchange connections.
"""
from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio

from config.settings import Settings, OBISettings, VPINSettings, SpreadSettings, OptionsSettings
from config.constants import SignalDirection, SpreadStatus
from core.events import EventBus
from data.normalizer import DataNormalizer
from signals.obi import OBIModule
from signals.vpin import VPINModule
from signals.spread_monitor import SpreadMonitor
from signals.options_layer import OptionsLayer
from risk.risk_manager import RiskManager
from tests.conftest import make_order_book, make_trades


class TestEventBusPipeline:
    @pytest.mark.asyncio
    async def test_event_dispatch(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe("test_event", handler)
        await bus.start()

        await bus.queue.put({"type": "test_event", "data": {"value": 42}})
        await asyncio.sleep(0.1)

        await bus.stop()
        assert len(received) == 1
        assert received[0]["data"]["value"] == 42

    @pytest.mark.asyncio
    async def test_unsubscribed_events_dropped(self):
        bus = EventBus()
        await bus.start()
        await bus.queue.put({"type": "unknown", "data": {}})
        await asyncio.sleep(0.1)
        await bus.stop()
        assert bus.stats["dropped"] == 1

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        bus = EventBus()
        results_a = []
        results_b = []

        async def handler_a(event):
            results_a.append(event)

        async def handler_b(event):
            results_b.append(event)

        bus.subscribe("shared", handler_a)
        bus.subscribe("shared", handler_b)
        await bus.start()

        await bus.queue.put({"type": "shared", "data": {}})
        await asyncio.sleep(0.1)
        await bus.stop()

        assert len(results_a) == 1
        assert len(results_b) == 1


class TestNormalizerPipeline:
    def test_order_book_normalization(self):
        norm = DataNormalizer()
        ob = make_order_book(bid_base=50000, ask_base=50010)
        event = {"type": "order_book", "data": {**ob, "symbol": "BTCUSDT", "exchange": "binance"}}
        result = norm.normalize_order_book(event)
        assert "mid_price" in result["data"]
        assert result["data"]["mid_price"] > 0

    def test_trade_normalization(self):
        norm = DataNormalizer()
        event = {
            "type": "trade",
            "data": {
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "price": 50000,
                "qty": 0.1,
                "side": "buy",
                "timestamp_ms": 1700000000000,
            },
        }
        result = norm.normalize_trade(event)
        assert "ts_utc" in result["data"]
        assert not result["data"]["was_outlier"]

    def test_price_outlier_filtered(self):
        norm = DataNormalizer()
        base = {
            "type": "trade",
            "data": {
                "symbol": "BTCUSDT", "exchange": "binance",
                "price": 50000, "qty": 0.1, "side": "buy",
                "timestamp_ms": 1700000000000,
            },
        }
        norm.normalize_trade(base)

        outlier = {
            "type": "trade",
            "data": {
                "symbol": "BTCUSDT", "exchange": "binance",
                "price": 100000, "qty": 0.1, "side": "buy",
                "timestamp_ms": 1700000001000,
            },
        }
        result = norm.normalize_trade(outlier)
        assert result["data"]["was_outlier"]
        assert result["data"]["price"] != 100000

    def test_ticker_normalization(self):
        norm = DataNormalizer()
        event = {
            "type": "ticker",
            "data": {
                "symbol": "BTCUSDT", "exchange": "binance",
                "best_bid": 50000, "best_ask": 50010,
                "best_bid_qty": 1.0, "best_ask_qty": 1.0,
            },
        }
        result = norm.normalize_ticker(event)
        data = result["data"]
        assert data["mid_price"] == 50005
        assert data["spread"] == 10
        assert data["spread_pct"] > 0


class TestFullSignalPipeline:
    """Test all 4 signal modules working together as they would in the engine."""

    def test_bullish_alignment(self):
        obi = OBIModule(OBISettings(ma_window=5, consistency_window=3))
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        spread = SpreadMonitor(SpreadSettings(baseline_window=20))
        options = OptionsLayer()

        ob = make_order_book(bid_qty=30, ask_qty=5)
        for _ in range(10):
            obi_sig = obi.update(ob)

        for i in range(10000):
            side_price = 100.05 if i % 2 == 0 else 99.95
            vpin.process_trade(side_price, 1.0, 100.0)
        vpin_sig = vpin.update({})

        for _ in range(20):
            spread.update({"best_bid": 100.0, "best_ask": 100.05})
        spread_sig = spread.update({"best_bid": 100.0, "best_ask": 100.05})

        chain = [
            {"type": "call", "strike": 50000 + i * 1000, "gamma": 0.001, "open_interest": 100}
            for i in range(10)
        ] + [
            {"type": "put", "strike": 49000 - i * 1000, "gamma": 0.001, "open_interest": 50}
            for i in range(5)
        ]
        options_sig = options.update({"options_chain": chain, "spot_price": 50000})

        assert obi_sig.direction == SignalDirection.BULLISH
        assert vpin_sig.metadata.get("status") != "VETO"
        assert spread_sig.metadata.get("is_veto") is not True

    def test_veto_conditions(self):
        vpin = VPINModule(VPINSettings(
            bucket_size=200, window=20, min_buckets=20,
            veto_threshold=0.65, spike_filter_pct=1.0,
        ))
        mid = 100.0
        for _ in range(10000):
            vpin.process_trade(mid + 1.0, 1.0, mid)
        sig = vpin.update({})
        assert sig.metadata.get("is_veto") is True

    def test_spread_veto(self):
        spread = SpreadMonitor(SpreadSettings(baseline_window=20))
        for _ in range(20):
            spread.update({"best_bid": 100.0, "best_ask": 100.05})
        result = spread.update({"best_bid": 100.0, "best_ask": 101.0})
        assert result.metadata.get("is_veto") is True


class TestRiskIntegration:
    def test_full_trade_lifecycle(self):
        rm = RiskManager()
        rm.set_balance(10000)

        can, _ = rm.can_open_position("BTCUSDT")
        assert can

        size = rm.calculate_position_size(
            signal_score=0.50,
            stop_distance=100,
            vpin=0.40,
            spread_status="MM_ACTIVE",
        )
        assert size > 0

        rm.record_trade_open()
        assert rm.state.open_positions == 1

        rm.record_trade_close(pnl=50)
        assert rm.state.open_positions == 0
        assert rm.state.daily_pnl == 50

    def test_drawdown_reduces_size(self):
        rm = RiskManager()
        rm.set_balance(10000)

        size_normal = rm.calculate_position_size(0.50, 100)

        rm.state.daily_pnl = -250
        size_drawdown = rm.calculate_position_size(0.50, 100)

        assert size_drawdown <= size_normal

    def test_consecutive_losses_circuit_break(self):
        rm = RiskManager()
        rm.set_balance(10000)

        for _ in range(5):
            rm.record_trade_close(-50)

        can, reason = rm.can_open_position("BTCUSDT")
        assert not can


class TestBaselineTracker:
    def test_rolling_average(self):
        from data.normalizer import BaselineTracker
        bt = BaselineTracker(window=5)
        for v in [10, 20, 30, 40, 50]:
            bt.update(v)
        assert bt.average == 30.0

    def test_window_eviction(self):
        from data.normalizer import BaselineTracker
        bt = BaselineTracker(window=3)
        for v in [10, 20, 30, 40, 50]:
            bt.update(v)
        assert bt.count == 3
        assert bt.average == 40.0
