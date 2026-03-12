from __future__ import annotations

from collections import deque

from config.constants import SignalDirection, SpreadStatus
from config.settings import SpreadSettings
from signals.base import BaseSignalModule, SignalOutput


class SpreadMonitor(BaseSignalModule):
    """
    M3: Spread Monitor — tracks MM activity via bid-ask spread.

    Compares current spread against a 24-hour rolling baseline.
    Widening spreads indicate MM withdrawal; narrow spreads indicate MM active.
    Score is confidence-reducing (not directional).
    MM_WITHDRAWN triggers GLOBAL VETO.
    """

    @property
    def name(self) -> str:
        return "SPREAD"

    def __init__(self, settings: SpreadSettings | None = None):
        cfg = settings or SpreadSettings()
        self._baseline_win = cfg.baseline_window
        self._active_ratio = cfg.mm_active_ratio
        self._cautious_ratio = cfg.mm_cautious_ratio
        self._thinning_ratio = cfg.mm_thinning_ratio

        self._history: deque[float] = deque(maxlen=self._baseline_win)

    def reset(self) -> None:
        self._history.clear()

    def update(self, data: dict) -> SignalOutput:
        best_bid = data.get("best_bid", 0.0)
        best_ask = data.get("best_ask", 0.0)

        if best_bid <= 0 or best_ask <= 0:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                metadata={"reason": "invalid_prices"},
            )

        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100

        self._history.append(spread_pct)

        if len(self._history) < 10:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                raw_value=spread_pct,
                metadata={
                    "reason": "warming_up",
                    "samples": len(self._history),
                },
            )

        baseline = sum(self._history) / len(self._history)
        ratio = spread_pct / baseline if baseline > 0 else 1.0

        status = self._classify(ratio)
        score = self._score(ratio)

        return SignalOutput(
            module=self.name,
            direction=SignalDirection(score) if -1 <= score <= 1 else SignalDirection.NEUTRAL,
            raw_value=spread_pct,
            confidence=min(ratio / self._thinning_ratio, 1.0) if ratio > 1 else 0.0,
            metadata={
                "spread_pct": spread_pct,
                "baseline": baseline,
                "ratio": ratio,
                "status": status.value,
                "is_veto": status == SpreadStatus.MM_WITHDRAWN,
                "score": score,
            },
        )

    def _classify(self, ratio: float) -> SpreadStatus:
        if ratio < self._active_ratio:
            return SpreadStatus.MM_ACTIVE
        if ratio < self._cautious_ratio:
            return SpreadStatus.MM_CAUTIOUS
        if ratio < self._thinning_ratio:
            return SpreadStatus.MM_THINNING
        return SpreadStatus.MM_WITHDRAWN

    def _score(self, ratio: float) -> int:
        if ratio < self._active_ratio:
            return 0
        if ratio < self._cautious_ratio:
            return 0
        if ratio < self._thinning_ratio:
            return -1
        return -1

    @property
    def current_status(self) -> SpreadStatus | None:
        if len(self._history) < 10:
            return None
        baseline = sum(self._history) / len(self._history)
        if not self._history:
            return None
        ratio = self._history[-1] / baseline if baseline > 0 else 1.0
        return self._classify(ratio)

    @property
    def history(self) -> list[float]:
        return list(self._history)
