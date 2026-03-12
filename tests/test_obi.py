from __future__ import annotations

import pytest
from config.constants import SignalDirection
from config.settings import OBISettings
from signals.obi import OBIModule
from tests.conftest import make_order_book


class TestOBICalculation:
    def test_balanced_book_returns_half(self):
        obi = OBIModule()
        result = obi.calculate_obi(
            bids=[{"price": 100, "qty": 10}] * 10,
            asks=[{"price": 101, "qty": 10}] * 10,
        )
        assert abs(result - 0.5) < 1e-6

    def test_heavy_bids_returns_high(self):
        obi = OBIModule()
        result = obi.calculate_obi(
            bids=[{"price": 100, "qty": 90}] * 10,
            asks=[{"price": 101, "qty": 10}] * 10,
        )
        assert result == pytest.approx(0.9, abs=0.01)

    def test_heavy_asks_returns_low(self):
        obi = OBIModule()
        result = obi.calculate_obi(
            bids=[{"price": 100, "qty": 10}] * 10,
            asks=[{"price": 101, "qty": 90}] * 10,
        )
        assert result == pytest.approx(0.1, abs=0.01)

    def test_empty_book_returns_half(self):
        obi = OBIModule()
        assert obi.calculate_obi([], []) == 0.5

    def test_respects_depth_parameter(self):
        settings = OBISettings(depth=5)
        obi = OBIModule(settings)
        bids = [{"price": 100 - i, "qty": 10} for i in range(20)]
        asks = [{"price": 101 + i, "qty": 10} for i in range(20)]
        result = obi.calculate_obi(bids, asks)
        assert result == pytest.approx(0.5, abs=0.01)


class TestOBISignal:
    def _feed_consistent(self, obi: OBIModule, ob: dict, count: int) -> None:
        for _ in range(count):
            obi.update(ob)

    def test_insufficient_data_returns_neutral(self):
        obi = OBIModule()
        ob = make_order_book(bid_qty=20, ask_qty=5)
        result = obi.update(ob)
        assert result.direction == SignalDirection.NEUTRAL
        assert result.metadata["reason"] == "insufficient_data"

    def test_bullish_after_consistency(self):
        obi = OBIModule(OBISettings(ma_window=5, consistency_window=3))
        ob = make_order_book(bid_qty=30, ask_qty=5)
        for _ in range(10):
            result = obi.update(ob)
        assert result.direction == SignalDirection.BULLISH

    def test_bearish_after_consistency(self):
        obi = OBIModule(OBISettings(ma_window=5, consistency_window=3))
        ob = make_order_book(bid_qty=5, ask_qty=30)
        for _ in range(10):
            result = obi.update(ob)
        assert result.direction == SignalDirection.BEARISH

    def test_mixed_signals_return_neutral(self):
        obi = OBIModule(OBISettings(ma_window=5, consistency_window=3))
        bullish_ob = make_order_book(bid_qty=30, ask_qty=5)
        bearish_ob = make_order_book(bid_qty=5, ask_qty=30)
        for _ in range(5):
            obi.update(bullish_ob)
        for _ in range(5):
            obi.update(bearish_ob)
        result = obi.update(bullish_ob)
        assert result.direction == SignalDirection.NEUTRAL

    def test_reset_clears_history(self):
        obi = OBIModule()
        ob = make_order_book(bid_qty=20, ask_qty=5)
        for _ in range(15):
            obi.update(ob)
        assert len(obi.history) > 0
        obi.reset()
        assert len(obi.history) == 0

    def test_history_property(self):
        obi = OBIModule()
        ob = make_order_book()
        for _ in range(5):
            obi.update(ob)
        assert len(obi.history) == 5
