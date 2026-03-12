from __future__ import annotations

import pytest
from config.constants import SignalDirection, SpreadStatus
from config.settings import SpreadSettings
from signals.spread_monitor import SpreadMonitor


def _feed_spread(monitor: SpreadMonitor, bid: float, ask: float, count: int = 1):
    result = None
    for _ in range(count):
        result = monitor.update({"best_bid": bid, "best_ask": ask})
    return result


class TestSpreadClassification:
    def test_mm_active(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.05, count=20)
        result = _feed_spread(sm, 100.0, 100.05)
        assert result.metadata["status"] == SpreadStatus.MM_ACTIVE.value

    def test_mm_cautious(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.01, count=20)
        result = _feed_spread(sm, 100.0, 100.015)
        assert result.metadata["status"] == SpreadStatus.MM_CAUTIOUS.value

    def test_mm_thinning(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.01, count=20)
        result = _feed_spread(sm, 100.0, 100.025)
        assert result.metadata["status"] == SpreadStatus.MM_THINNING.value

    def test_mm_withdrawn_triggers_veto(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.01, count=20)
        result = _feed_spread(sm, 100.0, 100.05)
        assert result.metadata["status"] == SpreadStatus.MM_WITHDRAWN.value
        assert result.metadata["is_veto"] is True


class TestSpreadSignal:
    def test_invalid_prices_neutral(self):
        sm = SpreadMonitor()
        result = sm.update({"best_bid": 0, "best_ask": 0})
        assert result.direction == SignalDirection.NEUTRAL

    def test_warming_up_neutral(self):
        sm = SpreadMonitor()
        result = sm.update({"best_bid": 100, "best_ask": 100.05})
        assert result.direction == SignalDirection.NEUTRAL
        assert result.metadata.get("reason") == "warming_up"

    def test_reset_clears_history(self):
        sm = SpreadMonitor()
        _feed_spread(sm, 100, 100.05, count=50)
        assert len(sm.history) > 0
        sm.reset()
        assert len(sm.history) == 0

    def test_normal_spread_zero_score(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.05, count=20)
        result = _feed_spread(sm, 100.0, 100.05)
        assert result.metadata["score"] == 0

    def test_wide_spread_negative_score(self):
        sm = SpreadMonitor(SpreadSettings(baseline_window=20))
        _feed_spread(sm, 100.0, 100.05, count=20)
        result = _feed_spread(sm, 100.0, 100.20)
        assert result.metadata["score"] < 0
