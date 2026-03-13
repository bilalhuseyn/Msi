from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from config.settings import RiskSettings

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    size_multiplier: float = 1.0
    reduced_trades_remaining: int = 0
    is_halted: bool = False
    halt_reason: str = ""


class CircuitBreaker:
    """
    Anti-tilt mechanism that reduces position sizes or enforces cooldowns
    after consecutive losses to prevent emotional/revenge trading.

    Tiers:
    - 3 consecutive losses: 50% size for next 2 trades
    - 5 consecutive losses: 1h cooldown + 25% size for next 3 trades
    - Resets on first winner
    """

    def __init__(self, settings: RiskSettings | None = None):
        cfg = settings or RiskSettings()
        self._reduce_at = cfg.consecutive_loss_reduce_at
        self._cooldown_at = cfg.consecutive_loss_cooldown_at
        self.state = CircuitBreakerState()

    def record_trade_result(self, pnl: float) -> None:
        if pnl >= 0:
            self.state.consecutive_losses = 0
            self.state.reduced_trades_remaining = 0
            self.state.size_multiplier = 1.0
            self.state.is_halted = False
            self.state.halt_reason = ""
            return

        self.state.consecutive_losses += 1
        streak = self.state.consecutive_losses

        if streak >= self._cooldown_at:
            self.state.cooldown_until = time.time() + 3600
            self.state.size_multiplier = 0.25
            self.state.reduced_trades_remaining = 3
            self.state.is_halted = True
            self.state.halt_reason = f"{streak} consecutive losses — 1h cooldown"
            logger.warning(
                "Circuit breaker: %d consecutive losses — 1h cooldown activated",
                streak,
            )
        elif streak >= self._reduce_at:
            self.state.size_multiplier = 0.50
            self.state.reduced_trades_remaining = 2
            logger.warning(
                "Circuit breaker: %d consecutive losses — size reduced to 50%%",
                streak,
            )

    def can_trade(self) -> tuple[bool, str]:
        if self.state.is_halted:
            if time.time() < self.state.cooldown_until:
                remaining = int(self.state.cooldown_until - time.time())
                return False, f"Cooldown active — {remaining}s remaining"
            self.state.is_halted = False
            self.state.halt_reason = ""

        return True, ""

    def get_size_multiplier(self) -> float:
        if self.state.reduced_trades_remaining > 0:
            self.state.reduced_trades_remaining -= 1
            mult = self.state.size_multiplier
            if self.state.reduced_trades_remaining == 0:
                self.state.size_multiplier = 1.0
            return mult
        return 1.0


class DrawdownScaler:
    """
    Progressive risk scaling based on daily drawdown.
    Instead of a binary 3% halt, scales risk down as losses accumulate.
    """

    def __init__(self, settings: RiskSettings | None = None):
        cfg = settings or RiskSettings()
        self._tiers = sorted(cfg.drawdown_tiers, key=lambda t: t[0])
        self._max_daily_loss = cfg.max_daily_loss_pct

    def get_risk_multiplier(
        self, daily_loss_pct: float, account_balance: float
    ) -> tuple[float, bool]:
        """
        Returns (risk_multiplier, should_halt).
        daily_loss_pct should be positive (absolute value of loss fraction).
        """
        if daily_loss_pct <= 0:
            return 1.0, False

        if daily_loss_pct >= self._max_daily_loss:
            return 0.0, True

        multiplier = 1.0
        for threshold, mult in self._tiers:
            if daily_loss_pct >= threshold:
                multiplier = mult

        return multiplier, False
