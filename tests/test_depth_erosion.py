from __future__ import annotations

import time
import pytest
from config.constants import DepthErosionStatus, SignalDirection
from signals.depth_erosion import DepthErosionMonitor
from tests.conftest import make_order_book


class TestDepthErosionBaseline:
    def test_first_call_sets_baseline(self):
        de = DepthErosionMonitor(check_interval=0)
        ob = make_order_book(bid_qty=10, ask_qty=10)
        ob["mid_price"] = 100.0
        ob["timestamp"] = time.time()
        result = de.update(ob)
        assert result.metadata["status"] == DepthErosionStatus.BASELINE_SET.value

    def test_skip_within_interval(self):
        de = DepthErosionMonitor(check_interval=60)
        ob = make_order_book(bid_qty=10, ask_qty=10)
        ob["mid_price"] = 100.0
        ob["timestamp"] = time.time()
        de.update(ob)
        result = de.update(ob)
        assert result.metadata["status"] == DepthErosionStatus.SKIP.value


class TestDepthErosionDetection:
    def test_hidden_buy_detected(self):
        de = DepthErosionMonitor(check_interval=0, erosion_threshold=0.30)
        t = time.time()
        ob1 = make_order_book(bid_qty=10, ask_qty=10)
        ob1["mid_price"] = 100.0
        ob1["timestamp"] = t
        de.update(ob1)

        ob2 = make_order_book(bid_qty=10, ask_qty=3)
        ob2["mid_price"] = 100.0
        ob2["timestamp"] = t + 1
        result = de.update(ob2)
        assert result.metadata["status"] == DepthErosionStatus.HIDDEN_BUY.value
        assert result.direction == SignalDirection.BULLISH

    def test_hidden_sell_detected(self):
        de = DepthErosionMonitor(check_interval=0, erosion_threshold=0.30)
        t = time.time()
        ob1 = make_order_book(bid_qty=10, ask_qty=10)
        ob1["mid_price"] = 100.0
        ob1["timestamp"] = t
        de.update(ob1)

        ob2 = make_order_book(bid_qty=3, ask_qty=10)
        ob2["mid_price"] = 100.0
        ob2["timestamp"] = t + 1
        result = de.update(ob2)
        assert result.metadata["status"] == DepthErosionStatus.HIDDEN_SELL.value
        assert result.direction == SignalDirection.BEARISH

    def test_no_erosion_when_price_moved(self):
        de = DepthErosionMonitor(check_interval=0, erosion_threshold=0.30, price_stability=0.003)
        t = time.time()
        ob1 = make_order_book(bid_qty=10, ask_qty=10)
        ob1["mid_price"] = 100.0
        ob1["timestamp"] = t
        de.update(ob1)

        ob2 = make_order_book(bid_qty=10, ask_qty=3)
        ob2["mid_price"] = 101.0
        ob2["timestamp"] = t + 1
        result = de.update(ob2)
        assert result.metadata["status"] == DepthErosionStatus.NEUTRAL.value

    def test_spoof_suppresses_erosion(self):
        de = DepthErosionMonitor(check_interval=0, erosion_threshold=0.30)
        t = time.time()
        ob1 = make_order_book(bid_qty=10, ask_qty=10)
        ob1["mid_price"] = 100.0
        ob1["timestamp"] = t
        de.update(ob1)

        ob2 = make_order_book(bid_qty=10, ask_qty=3)
        ob2["mid_price"] = 100.0
        ob2["timestamp"] = t + 1
        ob2["spoof_active"] = True
        result = de.update(ob2)
        assert result.metadata["status"] == DepthErosionStatus.NEUTRAL.value
        assert result.metadata["spoof_suppressed"] is True

    def test_reset_clears_state(self):
        de = DepthErosionMonitor(check_interval=0)
        ob = make_order_book()
        ob["mid_price"] = 100.0
        ob["timestamp"] = time.time()
        de.update(ob)
        de.reset()
        assert de._baseline_ask is None
        assert de._baseline_bid is None
