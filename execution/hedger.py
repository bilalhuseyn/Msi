"""
Dynamic Hedger — PRD-defined 5-trigger hedge system.

Hedge triggers:
  1. SIZE    — position notional > $5,000
  2. VPIN    — VPIN > 0.55 (informed trading detected)
  3. TIME    — holding > 120 minutes
  4. ADVERSE — unrealized PnL < -1.2%
  5. GEX_FLIP — Options Layer detects gamma regime flip

Hedge levels by trigger count:
  2 triggers → 40% hedge
  3 triggers → 65% hedge
  4 triggers → 85% hedge
  5 triggers → 100% hedge (full close)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class HedgeDecision:
    """Output of the hedge evaluation."""
    should_hedge: bool = False
    hedge_pct: float = 0.0
    trigger_count: int = 0
    triggers: list[str] = field(default_factory=list)
    reason: str = ""


class DynamicHedger:
    """
    Evaluates 5 hedge triggers against an open position and determines
    how much of the position should be hedged (partially closed).
    """

    def __init__(
        self,
        size_threshold_usd: float = 5_000.0,
        vpin_threshold: float = 0.55,
        time_threshold_minutes: float = 120.0,
        adverse_pct: float = 0.012,
        hedge_levels: dict[int, float] | None = None,
    ):
        self._size_thresh = size_threshold_usd
        self._vpin_thresh = vpin_threshold
        self._time_thresh = time_threshold_minutes * 60
        self._adverse_pct = adverse_pct
        self._hedge_levels = hedge_levels or {
            2: 0.40,
            3: 0.65,
            4: 0.85,
            5: 1.00,
        }

    def evaluate(
        self,
        position_value_usd: float,
        vpin_value: float | None,
        hold_time_seconds: float,
        unrealized_pnl_pct: float,
        gex_flip_detected: bool,
    ) -> HedgeDecision:
        """
        Check all 5 hedge triggers and return a hedge decision.

        Args:
            position_value_usd: Current notional value of position
            vpin_value: Current VPIN reading (None if not ready)
            hold_time_seconds: How long position has been held
            unrealized_pnl_pct: Unrealized PnL as % of entry (negative = losing)
            gex_flip_detected: Whether Options Layer detected a GEX flip
        """
        triggers: list[str] = []

        if position_value_usd >= self._size_thresh:
            triggers.append("SIZE")

        if vpin_value is not None and vpin_value > self._vpin_thresh:
            triggers.append("VPIN")

        if hold_time_seconds >= self._time_thresh:
            triggers.append("TIME")

        if unrealized_pnl_pct <= -self._adverse_pct:
            triggers.append("ADVERSE")

        if gex_flip_detected:
            triggers.append("GEX_FLIP")

        count = len(triggers)

        if count < 2:
            return HedgeDecision(
                should_hedge=False,
                trigger_count=count,
                triggers=triggers,
                reason="insufficient_triggers" if count == 1 else "no_triggers",
            )

        hedge_pct = self._hedge_levels.get(count, 1.0)
        if count > max(self._hedge_levels.keys()):
            hedge_pct = 1.0

        return HedgeDecision(
            should_hedge=True,
            hedge_pct=hedge_pct,
            trigger_count=count,
            triggers=triggers,
            reason=f"hedge_{int(hedge_pct * 100)}pct: {'+'.join(triggers)}",
        )
