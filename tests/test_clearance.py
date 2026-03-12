from __future__ import annotations

import pytest
from config.constants import ClearanceStatus, SignalDirection
from signals.clearance_detector import ClearanceDetector
from tests.conftest import make_order_book


def _make_trades(count: int, sell_ratio: float = 0.5, qty: float = 1.0):
    trades = []
    n_sells = int(count * sell_ratio)
    for i in range(count):
        trades.append({
            "price": 100.0,
            "qty": qty,
            "side": "sell" if i < n_sells else "buy",
        })
    return trades


class TestClearanceTraces:
    def test_normal_conditions(self):
        cd = ClearanceDetector()
        ob = make_order_book(bid_qty=10, ask_qty=10)
        trades = _make_trades(100, sell_ratio=0.5)
        result = cd.update({"bids": ob["bids"], "asks": ob["asks"], "recent_trades": trades})
        assert result.metadata["status"] == ClearanceStatus.NORMAL.value

    def test_one_sided_sell_trace(self):
        cd = ClearanceDetector(one_sided_threshold=0.72)
        ob = make_order_book()
        trades = _make_trades(100, sell_ratio=0.80)
        result = cd.update({"bids": ob["bids"], "asks": ob["asks"], "recent_trades": trades})
        assert "ONE_SIDED_SELL" in result.metadata["traces"]

    def test_one_sided_buy_trace(self):
        cd = ClearanceDetector(one_sided_threshold=0.72)
        ob = make_order_book()
        trades = _make_trades(100, sell_ratio=0.20)
        result = cd.update({"bids": ob["bids"], "asks": ob["asks"], "recent_trades": trades})
        assert "ONE_SIDED_BUY" in result.metadata["traces"]


class TestClearanceActive:
    def test_three_traces_triggers_clearance(self):
        cd = ClearanceDetector(
            one_sided_threshold=0.72,
            bid_thin_pct=0.55,
            ob_history_depth=2,
        )
        ob_normal = make_order_book(bid_qty=10, ask_qty=10, bid_base=100, ask_base=100.1)
        for _ in range(3):
            cd.update({
                "bids": ob_normal["bids"],
                "asks": ob_normal["asks"],
                "recent_trades": _make_trades(100, sell_ratio=0.5),
            })

        big_sells = [{"price": 100, "qty": 50, "side": "sell"}] * 5
        normal_trades = _make_trades(100, sell_ratio=0.80)
        ob_eroded = make_order_book(bid_qty=2, ask_qty=10, bid_base=100, ask_base=99.9)
        result = cd.update({
            "bids": ob_eroded["bids"],
            "asks": ob_eroded["asks"],
            "recent_trades": normal_trades + big_sells,
        })
        assert result.metadata["score"] >= 3
        assert result.metadata["is_veto"] is True
        assert result.direction == SignalDirection.BEARISH

    def test_two_traces_gives_possible(self):
        cd = ClearanceDetector(one_sided_threshold=0.72)
        ob = make_order_book(bid_qty=10, ask_qty=10)
        trades = _make_trades(100, sell_ratio=0.80)
        result = cd.update({"bids": ob["bids"], "asks": ob["asks"], "recent_trades": trades})
        if result.metadata["score"] == 2:
            assert result.metadata["status"] == ClearanceStatus.CLEARANCE_POSSIBLE.value

    def test_reset_clears(self):
        cd = ClearanceDetector()
        ob = make_order_book()
        cd.update({"bids": ob["bids"], "asks": ob["asks"], "recent_trades": []})
        cd.reset()
        assert cd.last_status == ClearanceStatus.NORMAL
        assert len(cd._ob_history) == 0
