from __future__ import annotations

import pytest
from config.constants import SignalDirection
from signals.spoofing_detector import SpoofingDetector


def _make_ob(bid_large: float = 0, ask_large: float = 0):
    bids = [{"price": 100.0 - i * 0.1, "qty": 5.0} for i in range(10)]
    asks = [{"price": 100.1 + i * 0.1, "qty": 5.0} for i in range(10)]
    if bid_large > 0:
        bids[0] = {"price": 100.0, "qty": bid_large}
    if ask_large > 0:
        asks[0] = {"price": 100.1, "qty": ask_large}
    return {"bids": bids, "asks": asks}


class TestSpoofDetection:
    def test_no_spoof_when_order_persists(self):
        sd = SpoofingDetector(size_threshold=50)
        ob1 = _make_ob(bid_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob(bid_large=60)
        ob2["timestamp_ms"] = 1500
        result = sd.update(ob2)
        assert result.metadata["spoof_count"] == 0

    def test_bid_spoof_detected(self):
        sd = SpoofingDetector(size_threshold=50, cancel_window_ms=1000)
        ob1 = _make_ob(bid_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob()
        ob2["timestamp_ms"] = 1500
        result = sd.update(ob2)
        assert result.metadata["spoof_count"] > 0
        spoof = result.metadata["spoofs"][0]
        assert spoof["side"] == "BID"
        assert spoof["implication"] == "SELL"

    def test_ask_spoof_detected(self):
        sd = SpoofingDetector(size_threshold=50, cancel_window_ms=1000)
        ob1 = _make_ob(ask_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob()
        ob2["timestamp_ms"] = 1500
        result = sd.update(ob2)
        assert result.metadata["spoof_count"] > 0
        spoof = result.metadata["spoofs"][0]
        assert spoof["side"] == "ASK"
        assert spoof["implication"] == "BUY"

    def test_no_spoof_if_cancel_too_slow(self):
        sd = SpoofingDetector(size_threshold=50, cancel_window_ms=500)
        ob1 = _make_ob(bid_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob()
        ob2["timestamp_ms"] = 2000
        result = sd.update(ob2)
        assert result.metadata["spoof_count"] == 0

    def test_direction_from_bid_spoof(self):
        sd = SpoofingDetector(size_threshold=50, cancel_window_ms=1000)
        ob1 = _make_ob(bid_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob()
        ob2["timestamp_ms"] = 1500
        result = sd.update(ob2)
        assert result.direction == SignalDirection.BEARISH

    def test_direction_from_ask_spoof(self):
        sd = SpoofingDetector(size_threshold=50, cancel_window_ms=1000)
        ob1 = _make_ob(ask_large=60)
        ob1["timestamp_ms"] = 1000
        sd.update(ob1)

        ob2 = _make_ob()
        ob2["timestamp_ms"] = 1500
        result = sd.update(ob2)
        assert result.direction == SignalDirection.BULLISH

    def test_reset_clears_state(self):
        sd = SpoofingDetector(size_threshold=50)
        ob = _make_ob(bid_large=60)
        ob["timestamp_ms"] = 1000
        sd.update(ob)
        assert len(sd._log) > 0
        sd.reset()
        assert len(sd._log) == 0
        assert sd._consecutive_count == 0
