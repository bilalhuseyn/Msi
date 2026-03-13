from __future__ import annotations

import pytest
from config.constants import SignalDirection
from config.settings import VPINSettings
from signals.vpin import VPINModule


def _fill_buckets(vpin_mod: VPINModule, count: int, buy_ratio: float = 0.5) -> None:
    """Feed trades until `count` buckets are filled."""
    bucket_size = vpin_mod._bucket_size
    mid = 100.0
    filled = 0
    while filled < count:
        n_buy = int(bucket_size * buy_ratio)
        n_sell = int(bucket_size * (1 - buy_ratio))
        for _ in range(n_buy):
            vpin_mod.process_trade(price=mid + 0.01, qty=1.0, mid_price=mid)
        for _ in range(n_sell):
            vpin_mod.process_trade(price=mid - 0.01, qty=1.0, mid_price=mid)
        remaining = bucket_size - n_buy - n_sell
        if remaining > 0:
            vpin_mod.process_trade(price=mid + 0.01, qty=remaining, mid_price=mid)
        filled += 1


class TestVPINBucketing:
    def test_empty_returns_none(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        assert vpin.vpin is None
        assert not vpin.is_ready

    def test_bucket_fills_on_volume(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        for _ in range(200):
            vpin.process_trade(price=101, qty=1.0, mid_price=100)
        assert vpin._total_buckets_filled == 1

    def test_balanced_trades_low_vpin(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        _fill_buckets(vpin, 30, buy_ratio=0.5)
        v = vpin.vpin
        assert v is not None
        assert v < 0.3

    def test_onesided_trades_high_vpin(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        _fill_buckets(vpin, 30, buy_ratio=0.95)
        v = vpin.vpin
        assert v is not None
        assert v > 0.5


class TestVPINSignal:
    def test_insufficient_data_neutral(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        result = vpin.update({})
        assert result.direction == SignalDirection.NEUTRAL
        assert result.metadata["reason"] == "insufficient_data"

    def test_safe_vpin_neutral(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        _fill_buckets(vpin, 30, buy_ratio=0.5)
        result = vpin.update({})
        assert result.direction == SignalDirection.NEUTRAL
        assert result.metadata["status"] == "SAFE"

    def test_high_vpin_triggers_veto(self):
        settings = VPINSettings(
            bucket_size=200, window=20, min_buckets=20,
            veto_threshold=0.65, spike_filter_pct=1.0,
        )
        vpin = VPINModule(settings)
        _fill_buckets(vpin, 30, buy_ratio=0.99)
        result = vpin.update({})
        assert result.metadata["is_veto"] is True

    def test_reset_clears_state(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        _fill_buckets(vpin, 30)
        assert vpin.is_ready
        vpin.reset()
        assert not vpin.is_ready
        assert vpin.vpin is None

    def test_update_with_trade_data(self):
        vpin = VPINModule(VPINSettings(bucket_size=200, window=20, min_buckets=20))
        for i in range(5000):
            vpin.update({"price": 100.01, "qty": 1.0, "mid_price": 100.0})
        assert vpin._total_buckets_filled >= 20
