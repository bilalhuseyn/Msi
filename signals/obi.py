from __future__ import annotations

from collections import deque

from config.constants import SignalDirection
from config.settings import OBISettings
from signals.base import BaseSignalModule, SignalOutput


class OBIModule(BaseSignalModule):
    """
    M1: Order Book Imbalance.

    Measures bid/ask volume balance in the top N order-book levels.
    Requires `consistency_window` consecutive periods with the same
    directional reading before emitting a non-neutral signal.
    """

    @property
    def name(self) -> str:
        return "OBI"

    def __init__(self, settings: OBISettings | None = None):
        cfg = settings or OBISettings()
        self._depth = cfg.depth
        self._bull_thresh = cfg.bullish_threshold
        self._bear_thresh = cfg.bearish_threshold
        self._consist_win = cfg.consistency_window
        self._ma_win = cfg.ma_window

        self._history: deque[float] = deque(maxlen=max(self._ma_win, 200))

    def reset(self) -> None:
        self._history.clear()

    def calculate_obi(self, bids: list[dict], asks: list[dict]) -> float:
        bid_vol = sum(l["qty"] for l in bids[: self._depth])
        ask_vol = sum(l["qty"] for l in asks[: self._depth])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.5
        return bid_vol / total

    def update(self, data: dict) -> SignalOutput:
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        obi_raw = self.calculate_obi(bids, asks)
        self._history.append(obi_raw)

        if len(self._history) < self._ma_win:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                raw_value=obi_raw,
                metadata={"reason": "insufficient_data", "count": len(self._history)},
            )

        window = list(self._history)[-self._ma_win :]
        obi_ma = sum(window) / len(window)

        direction = self._classify(obi_ma)
        if not self._is_consistent():
            direction = SignalDirection.NEUTRAL

        return SignalOutput(
            module=self.name,
            direction=direction,
            raw_value=obi_raw,
            confidence=abs(obi_ma - 0.5) * 2,
            metadata={"obi_ma": obi_ma, "obi_raw": obi_raw},
        )

    def _classify(self, obi_ma: float) -> SignalDirection:
        if obi_ma > self._bull_thresh:
            return SignalDirection.BULLISH
        if obi_ma < self._bear_thresh:
            return SignalDirection.BEARISH
        return SignalDirection.NEUTRAL

    def _is_consistent(self) -> bool:
        """
        P7 fix: MA-based consistency — require the rolling MA to point clearly
        in one direction for consistency_window periods, allowing one neutral
        reading in between without resetting the check.
        """
        if len(self._history) < self._consist_win:
            return False
        recent = list(self._history)[-self._consist_win :]
        directions = []
        for v in recent:
            if v > self._bull_thresh:
                directions.append(1)
            elif v < self._bear_thresh:
                directions.append(-1)
            else:
                directions.append(0)

        non_neutral = [d for d in directions if d != 0]
        if not non_neutral:
            return False
        dominant = non_neutral[0]
        # Allow at most one neutral reading; all non-neutral must agree
        neutral_count = directions.count(0)
        return neutral_count <= 1 and all(d == dominant for d in non_neutral)

    @property
    def history(self) -> list[float]:
        return list(self._history)
