from __future__ import annotations

import logging
from config.constants import SignalDirection
from signals.base import BaseSignalModule, SignalOutput

logger = logging.getLogger(__name__)


class SpoofingDetector(BaseSignalModule):
    """
    M5: Spoofing Detector — Manipulation Shield.

    Tracks large orders in the order book. If a large order disappears
    within `cancel_window_ms` it is flagged as a spoof.

    Spoof → OBI interaction rules (applied in ConfirmationEngine):
      BID-side spoof + OBI BULLISH → reverse to BEARISH
      ASK-side spoof + OBI BEARISH → reverse to BULLISH
      3+ consecutive spoofs → OBI suspended (set to 0)
    """

    @property
    def name(self) -> str:
        return "SPOOF"

    def __init__(
        self,
        size_threshold: float = 50.0,
        cancel_window_ms: int = 800,
    ):
        self._size_threshold = size_threshold
        self._cancel_window = cancel_window_ms
        self._log: dict[float, dict] = {}
        self._recent_spoofs: list[dict] = []
        self._consecutive_count = 0

    def reset(self) -> None:
        self._log.clear()
        self._recent_spoofs.clear()
        self._consecutive_count = 0

    def update(self, data: dict) -> SignalOutput:
        """
        Expected data keys:
          bids: list[{price, qty}]
          asks: list[{price, qty}]
          timestamp_ms: int
        """
        ob = data
        ts_ms = data.get("timestamp_ms", 0)
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        signals = self._detect(bids, asks, ts_ms)

        if signals:
            self._consecutive_count += len(signals)
            self._recent_spoofs.extend(signals)
            if len(self._recent_spoofs) > 20:
                self._recent_spoofs = self._recent_spoofs[-20:]
        else:
            if self._consecutive_count > 0:
                self._consecutive_count = max(0, self._consecutive_count - 1)

        spoof_direction = SignalDirection.NEUTRAL
        if signals:
            latest = signals[-1]
            if latest["implication"] == "BUY":
                spoof_direction = SignalDirection.BULLISH
            elif latest["implication"] == "SELL":
                spoof_direction = SignalDirection.BEARISH

        return SignalOutput(
            module=self.name,
            direction=spoof_direction,
            raw_value=float(len(signals)),
            confidence=min(len(signals) / 3.0, 1.0),
            metadata={
                "spoof_count": len(signals),
                "spoofs": signals,
                "consecutive": self._consecutive_count,
                "is_active": len(signals) > 0,
            },
        )

    def _detect(
        self,
        bids: list[dict],
        asks: list[dict],
        ts_ms: int,
    ) -> list[dict]:
        current_large = self._find_large(bids, asks)
        signals: list[dict] = []

        for price, order_info in list(self._log.items()):
            still_exists = any(
                abs(l["price"] - price) < 1e-8 and l["qty"] >= self._size_threshold
                for side_list in (bids, asks)
                for l in side_list
            )
            if not still_exists:
                elapsed = ts_ms - order_info["ts"]
                if 0 < elapsed < self._cancel_window:
                    implication = "SELL" if order_info["side"] == "BID" else "BUY"
                    signals.append({
                        "side": order_info["side"],
                        "price": price,
                        "elapsed_ms": elapsed,
                        "implication": implication,
                    })
                    logger.info(
                        "Spoof detected: %s @ %.2f cancelled in %dms -> %s",
                        order_info["side"], price, elapsed, implication,
                    )
                del self._log[price]

        for o in current_large:
            self._log[o["price"]] = {**o, "ts": ts_ms}

        return signals

    def _find_large(
        self, bids: list[dict], asks: list[dict]
    ) -> list[dict]:
        result = []
        for l in bids:
            if l["qty"] >= self._size_threshold:
                result.append({**l, "side": "BID"})
        for l in asks:
            if l["qty"] >= self._size_threshold:
                result.append({**l, "side": "ASK"})
        return result

    @property
    def recent_spoofs(self) -> list[dict]:
        return list(self._recent_spoofs)

    @property
    def is_active(self) -> bool:
        return len(self._recent_spoofs) > 0 and self._consecutive_count > 0
