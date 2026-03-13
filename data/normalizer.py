from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BaselineTracker:
    """Rolling baseline average over a configurable window (in samples)."""
    window: int = 1440
    _values: deque[float] = field(default_factory=deque)

    def update(self, value: float) -> float:
        self._values.append(value)
        if len(self._values) > self.window:
            self._values.popleft()
        return self.average

    @property
    def average(self) -> float:
        if not self._values:
            return 0.0
        return sum(self._values) / len(self._values)

    @property
    def count(self) -> int:
        return len(self._values)


class PriceAnomalyFilter:
    """
    Filters out price outliers that deviate >threshold_pct from the previous value.
    Replaces outliers with the rolling median.
    """

    def __init__(self, threshold_pct: float = 0.05, history_size: int = 50):
        self._threshold = threshold_pct
        self._history: deque[float] = deque(maxlen=history_size)
        self._last_valid: float | None = None

    def filter(self, price: float) -> tuple[float, bool]:
        """Returns (filtered_price, was_outlier)."""
        if self._last_valid is None:
            self._last_valid = price
            self._history.append(price)
            return price, False

        deviation = abs(price - self._last_valid) / self._last_valid
        if deviation > self._threshold:
            median = self._get_median()
            logger.warning(
                "Price outlier: %.2f (deviation=%.2f%%, using median=%.2f)",
                price, deviation * 100, median,
            )
            return median, True

        self._last_valid = price
        self._history.append(price)
        return price, False

    def _get_median(self) -> float:
        if not self._history:
            return self._last_valid or 0.0
        sorted_h = sorted(self._history)
        mid = len(sorted_h) // 2
        if len(sorted_h) % 2 == 0:
            return (sorted_h[mid - 1] + sorted_h[mid]) / 2
        return sorted_h[mid]


class DataNormalizer:
    """
    Central normalizer that processes raw feed events:
    - UTC timestamp normalisation
    - Price anomaly filtering
    - Baseline tracking per symbol/metric
    - Last-known-value fallback on gaps
    """

    def __init__(self):
        self._price_filters: dict[str, PriceAnomalyFilter] = {}
        self._baselines: dict[str, BaselineTracker] = {}
        self._last_known: dict[str, dict] = {}

    def _get_price_filter(self, key: str) -> PriceAnomalyFilter:
        if key not in self._price_filters:
            self._price_filters[key] = PriceAnomalyFilter()
        return self._price_filters[key]

    def get_baseline(self, key: str, window: int = 1440) -> BaselineTracker:
        if key not in self._baselines:
            self._baselines[key] = BaselineTracker(window=window)
        return self._baselines[key]

    def normalize_order_book(self, event: dict) -> dict:
        data = event["data"]
        symbol = data["symbol"]
        exchange = data["exchange"]
        key = f"{exchange}:{symbol}"

        event["data"]["ts_utc"] = time.time()

        if data["bids"]:
            filt = self._get_price_filter(f"{key}:bid")
            top_bid = data["bids"][0]["price"]
            filtered_bid, _ = filt.filter(top_bid)
            data["mid_price"] = (
                (filtered_bid + data["asks"][0]["price"]) / 2
                if data["asks"]
                else filtered_bid
            )
        elif data["asks"]:
            data["mid_price"] = data["asks"][0]["price"]
        else:
            last = self._last_known.get(f"{key}:ob")
            data["mid_price"] = last["mid_price"] if last else 0.0

        self._last_known[f"{key}:ob"] = data
        return event

    def normalize_trade(self, event: dict) -> dict:
        data = event["data"]
        symbol = data["symbol"]
        exchange = data["exchange"]
        key = f"{exchange}:{symbol}:trade"

        filt = self._get_price_filter(key)
        data["price"], data["was_outlier"] = filt.filter(data["price"])
        data["ts_utc"] = data.get("timestamp_ms", time.time() * 1000) / 1000

        self._last_known[key] = data
        return event

    def normalize_ticker(self, event: dict) -> dict:
        data = event["data"]
        symbol = data["symbol"]
        exchange = data["exchange"]
        key = f"{exchange}:{symbol}:ticker"

        data["ts_utc"] = time.time()
        data["mid_price"] = (data["best_bid"] + data["best_ask"]) / 2
        data["spread"] = data["best_ask"] - data["best_bid"]
        data["spread_pct"] = (
            data["spread"] / data["mid_price"] * 100
            if data["mid_price"] > 0
            else 0.0
        )

        self._last_known[key] = data
        return event

    def get_last_known(self, key: str) -> dict | None:
        return self._last_known.get(key)
