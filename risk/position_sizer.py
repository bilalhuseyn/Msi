from __future__ import annotations

import logging
from collections import deque

from config.settings import RiskSettings

logger = logging.getLogger(__name__)


class PositionSizer:
    """
    Dynamic position sizing with:
    - PRD base logic (score-based multipliers)
    - Half-Kelly overlay cap
    - VPIN and spread adjustments
    """

    def __init__(self, settings: RiskSettings | None = None):
        cfg = settings or RiskSettings()
        self._base_risk_pct = cfg.base_risk_pct
        self._trade_results: deque[tuple[float, float]] = deque(maxlen=100)

    def record_result(self, pnl_r: float, risk_r: float) -> None:
        """Record trade result for Kelly calculation. pnl_r = PnL in R multiples."""
        self._trade_results.append((pnl_r, risk_r))

    def calculate(
        self,
        account_balance: float,
        signal_score: float,
        stop_distance: float,
        vpin: float | None = None,
        spread_status: str = "MM_ACTIVE",
        drawdown_multiplier: float = 1.0,
        circuit_breaker_multiplier: float = 1.0,
    ) -> float:
        if stop_distance <= 0 or account_balance <= 0:
            return 0.0

        score = abs(signal_score)
        if score < 0.35:
            return 0.0

        base_risk = account_balance * self._base_risk_pct

        if score < 0.50:
            score_mult = 0.60
        elif score < 0.70:
            score_mult = 0.80
        else:
            score_mult = 1.00

        vpin_mult = 1.0
        if vpin is not None and 0.55 < vpin <= 0.65:
            vpin_mult = 0.50

        spread_mult = 1.0
        if spread_status == "MM_CAUTIOUS":
            spread_mult = 0.70

        kelly_cap = self._half_kelly_cap()

        final_mult = min(
            score_mult * vpin_mult * spread_mult * drawdown_multiplier * circuit_breaker_multiplier,
            kelly_cap,
        )

        risk_usd = base_risk * final_mult
        size = risk_usd / stop_distance

        max_size = account_balance * 0.10 / stop_distance
        size = min(size, max_size)

        return max(size, 0.0)

    def _half_kelly_cap(self) -> float:
        """Half-Kelly fraction as position size cap."""
        if len(self._trade_results) < 20:
            return 1.0

        wins = [(pnl, risk) for pnl, risk in self._trade_results if pnl > 0]
        losses = [(pnl, risk) for pnl, risk in self._trade_results if pnl <= 0]

        if not wins or not losses:
            return 1.0

        win_rate = len(wins) / len(self._trade_results)
        avg_win = sum(p for p, _ in wins) / len(wins)
        avg_loss = abs(sum(p for p, _ in losses) / len(losses))

        if avg_loss == 0:
            return 1.0

        win_loss_ratio = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / win_loss_ratio

        half_kelly = max(kelly * 0.5, 0.1)
        return min(half_kelly, 1.0)
