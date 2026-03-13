from __future__ import annotations

import pytest
from config.settings import OBISettings, VPINSettings, SpreadSettings, OptionsSettings


def make_order_book(
    bid_base: float = 100.0,
    ask_base: float = 100.1,
    bid_qty: float = 10.0,
    ask_qty: float = 10.0,
    levels: int = 20,
    step: float = 0.1,
) -> dict:
    """Generate a synthetic order book for testing."""
    bids = [
        {"price": bid_base - i * step, "qty": bid_qty}
        for i in range(levels)
    ]
    asks = [
        {"price": ask_base + i * step, "qty": ask_qty}
        for i in range(levels)
    ]
    return {"bids": bids, "asks": asks, "symbol": "BTCUSDT", "exchange": "test"}


def make_trades(
    count: int = 100,
    base_price: float = 100.0,
    qty: float = 1.0,
    buy_ratio: float = 0.5,
) -> list[dict]:
    """Generate synthetic trade list."""
    trades = []
    for i in range(count):
        side = "buy" if i < int(count * buy_ratio) else "sell"
        trades.append({
            "price": base_price + (0.01 * (i % 10)),
            "qty": qty,
            "side": side,
            "timestamp_ms": 1000000 + i * 100,
        })
    return trades


@pytest.fixture
def obi_settings():
    return OBISettings()


@pytest.fixture
def vpin_settings():
    return VPINSettings(bucket_size=200.0, window=20, min_buckets=20)


@pytest.fixture
def spread_settings():
    return SpreadSettings()


@pytest.fixture
def options_settings():
    return OptionsSettings()


@pytest.fixture
def bullish_order_book():
    return make_order_book(bid_qty=20.0, ask_qty=5.0)


@pytest.fixture
def bearish_order_book():
    return make_order_book(bid_qty=5.0, ask_qty=20.0)


@pytest.fixture
def neutral_order_book():
    return make_order_book(bid_qty=10.0, ask_qty=10.0)
