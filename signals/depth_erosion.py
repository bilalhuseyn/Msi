from __future__ import annotations

import time
from config.constants import DepthErosionStatus, SignalDirection
from signals.base import BaseSignalModule, SignalOutput


class DepthErosionMonitor(BaseSignalModule):
    """
    M4: Depth Erosion Detector — Hidden Institutional Pressure.

    Detects order-book thinning on one side while price stays stable.
    When the ask side erodes without price moving up, a large buyer
    is likely absorbing liquidity (HIDDEN_BUY). The reverse signals
    HIDDEN_SELL.

    Checks run every `check_interval` seconds (default 60) to avoid
    over-trading on noise.  Erosion is suppressed when the Spoofing
    Detector fires at the same time (spoof creates artificial erosion).
    """

    @property
    def name(self) -> str:
        return "DEPTH"

    def __init__(
        self,
        check_interval: int = 60,
        erosion_threshold: float = 0.35,
        price_stability: float = 0.003,
        depth: int = 10,
    ):
        self._interval = check_interval
        self._threshold = erosion_threshold
        self._price_stable_pct = price_stability
        self._depth = depth

        self._baseline_ask: float | None = None
        self._baseline_bid: float | None = None
        self._price_baseline: float | None = None
        self._last_check: float = 0.0
        self._last_status = DepthErosionStatus.NEUTRAL
        self._last_score = 0

    def reset(self) -> None:
        self._baseline_ask = None
        self._baseline_bid = None
        self._price_baseline = None
        self._last_check = 0.0
        self._last_status = DepthErosionStatus.NEUTRAL
        self._last_score = 0

    def update(self, data: dict) -> SignalOutput:
        """
        Expected data keys:
          bids: list[{price, qty}]
          asks: list[{price, qty}]
          mid_price: float
          timestamp: float  (epoch seconds; falls back to time.time())
          spoof_active: bool (optional — from Spoofing Detector)
        """
        ts = data.get("timestamp", time.time())
        mid = data.get("mid_price", 0.0)
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        spoof_active = data.get("spoof_active", False)

        if ts - self._last_check < self._interval:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                metadata={"status": DepthErosionStatus.SKIP.value},
            )

        self._last_check = ts

        ask_depth = sum(l["qty"] for l in asks[: self._depth])
        bid_depth = sum(l["qty"] for l in bids[: self._depth])

        if self._baseline_ask is None:
            self._baseline_ask = ask_depth
            self._baseline_bid = bid_depth
            self._price_baseline = mid
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                metadata={"status": DepthErosionStatus.BASELINE_SET.value},
            )

        price_move = (
            abs(mid - self._price_baseline) / self._price_baseline
            if self._price_baseline
            else 0.0
        )

        ask_erosion = (
            (self._baseline_ask - ask_depth) / max(self._baseline_ask, 1e-9)
        )
        bid_erosion = (
            (self._baseline_bid - bid_depth) / max(self._baseline_bid, 1e-9)
        )

        status = DepthErosionStatus.NEUTRAL
        score = 0

        if price_move < self._price_stable_pct and not spoof_active:
            if ask_erosion > self._threshold:
                status = DepthErosionStatus.HIDDEN_BUY
                score = 1
            elif bid_erosion > self._threshold:
                status = DepthErosionStatus.HIDDEN_SELL
                score = -1

        # Baseline fix: only advance when no erosion detected, so the
        # pre-erosion depth level is preserved as reference during erosion
        if status == DepthErosionStatus.NEUTRAL:
            self._baseline_ask = ask_depth
            self._baseline_bid = bid_depth
            self._price_baseline = mid

        self._last_status = status
        self._last_score = score

        return SignalOutput(
            module=self.name,
            direction=SignalDirection(score),
            raw_value=max(ask_erosion, bid_erosion),
            confidence=max(abs(ask_erosion), abs(bid_erosion)),
            metadata={
                "status": status.value,
                "ask_erosion": round(ask_erosion, 4),
                "bid_erosion": round(bid_erosion, 4),
                "price_move": round(price_move, 6),
                "spoof_suppressed": spoof_active and (ask_erosion > self._threshold or bid_erosion > self._threshold),
            },
        )

    @property
    def last_status(self) -> DepthErosionStatus:
        return self._last_status
