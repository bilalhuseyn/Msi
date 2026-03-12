from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field

from config.settings import RiskSettings
from risk.circuit_breaker import CircuitBreaker, DrawdownScaler
from risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    trade_count_today: int = 0
    last_trade_ts: float = 0.0
    open_positions: int = 0
    portfolio_var: float = 0.0
    is_halted: bool = False
    halt_reason: str = ""


class CorrelationTracker:
    """Rolling Pearson correlation between two assets' returns."""

    def __init__(self, window: int = 1440):
        self._window = window
        self._returns_a: deque[float] = deque(maxlen=window)
        self._returns_b: deque[float] = deque(maxlen=window)

    def add_returns(self, return_a: float, return_b: float) -> None:
        self._returns_a.append(return_a)
        self._returns_b.append(return_b)

    @property
    def correlation(self) -> float | None:
        if len(self._returns_a) < 30:
            return None

        a = list(self._returns_a)
        b = list(self._returns_b)
        n = len(a)

        mean_a = sum(a) / n
        mean_b = sum(b) / n

        cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n)) / n
        std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a) / n)
        std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b) / n)

        if std_a == 0 or std_b == 0:
            return None

        return cov / (std_a * std_b)


class FundingRateTracker:
    """Tracks perpetual futures funding rates for hedge cost estimation."""

    def __init__(self):
        self._rates: dict[str, deque[tuple[float, float]]] = {}

    def record_rate(self, symbol: str, rate: float, ts: float | None = None) -> None:
        if symbol not in self._rates:
            self._rates[symbol] = deque(maxlen=1000)
        self._rates[symbol].append((ts or time.time(), rate))

    def annualized_rate(self, symbol: str) -> float | None:
        rates = self._rates.get(symbol)
        if not rates or len(rates) < 3:
            return None
        recent = [r for _, r in list(rates)[-24:]]
        avg_8h = sum(recent) / len(recent)
        return avg_8h * 3 * 365

    def is_expensive(self, symbol: str, threshold: float = 0.30) -> bool:
        annual = self.annualized_rate(symbol)
        if annual is None:
            return False
        return abs(annual) > threshold


class PortfolioVaR:
    """Simple ATR-based Value-at-Risk estimation."""

    def __init__(self, confidence: float = 0.95):
        z_scores = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
        self._z = z_scores.get(confidence, 1.645)

    def estimate(
        self,
        positions: list[dict],
        atr_values: dict[str, float],
    ) -> float:
        """
        Estimate 1-day portfolio VaR.
        positions: [{symbol, size, direction, entry_price}]
        atr_values: {symbol: atr_1d}
        """
        total_var = 0.0
        for pos in positions:
            sym = pos["symbol"]
            atr = atr_values.get(sym, 0.0)
            notional = abs(pos.get("size", 0) * pos.get("entry_price", 0))
            if notional == 0:
                continue
            daily_vol = atr / pos.get("entry_price", 1)
            position_var = notional * daily_vol * self._z
            total_var += position_var
        return total_var


class RiskManager:
    """
    Portfolio-level risk manager combining:
    - Per-trade limits (PRD base)
    - Drawdown-responsive scaling
    - Circuit breaker (consecutive loss protection)
    - Cross-asset correlation tracking
    - Funding rate awareness
    - Portfolio VaR estimation
    """

    def __init__(self, settings: RiskSettings | None = None):
        self._cfg = settings or RiskSettings()
        self.state = RiskState()
        self.sizer = PositionSizer(self._cfg)
        self.circuit_breaker = CircuitBreaker(self._cfg)
        self.drawdown_scaler = DrawdownScaler(self._cfg)
        self.correlation = CorrelationTracker()
        self.funding = FundingRateTracker()
        self.var_calculator = PortfolioVaR()
        self._account_balance: float = 0.0

    def set_balance(self, balance: float) -> None:
        self._account_balance = balance

    def can_open_position(self, symbol: str) -> tuple[bool, str]:
        if self.state.is_halted:
            return False, f"System halted: {self.state.halt_reason}"

        can, reason = self.circuit_breaker.can_trade()
        if not can:
            return False, reason

        if self.state.trade_count_today >= self._cfg.max_daily_trades:
            return False, f"Daily trade limit reached ({self._cfg.max_daily_trades})"

        if self.state.open_positions >= self._cfg.max_open_positions:
            return False, f"Max open positions reached ({self._cfg.max_open_positions})"

        cooldown_sec = self._cfg.cooldown_minutes * 60
        if time.time() - self.state.last_trade_ts < cooldown_sec:
            remaining = int(cooldown_sec - (time.time() - self.state.last_trade_ts))
            return False, f"Cooldown active — {remaining}s remaining"

        if self._account_balance > 0:
            daily_loss_pct = abs(min(self.state.daily_pnl, 0)) / self._account_balance
            _, should_halt = self.drawdown_scaler.get_risk_multiplier(
                daily_loss_pct, self._account_balance
            )
            if should_halt:
                self.state.is_halted = True
                self.state.halt_reason = f"Daily loss limit reached ({daily_loss_pct:.1%})"
                return False, self.state.halt_reason

        if self.state.portfolio_var > self._cfg.var_limit_pct * self._account_balance:
            return False, "Portfolio VaR exceeds limit"

        return True, ""

    def calculate_position_size(
        self,
        signal_score: float,
        stop_distance: float,
        vpin: float | None = None,
        spread_status: str = "MM_ACTIVE",
    ) -> float:
        if self._account_balance <= 0:
            return 0.0

        daily_loss_pct = abs(min(self.state.daily_pnl, 0)) / self._account_balance
        dd_mult, _ = self.drawdown_scaler.get_risk_multiplier(
            daily_loss_pct, self._account_balance
        )
        cb_mult = self.circuit_breaker.get_size_multiplier()

        return self.sizer.calculate(
            account_balance=self._account_balance,
            signal_score=signal_score,
            stop_distance=stop_distance,
            vpin=vpin,
            spread_status=spread_status,
            drawdown_multiplier=dd_mult,
            circuit_breaker_multiplier=cb_mult,
        )

    def check_correlation_cap(
        self, symbol: str, direction: int, existing_positions: list[dict]
    ) -> float:
        """
        If high correlation with existing positions in same direction,
        cap the combined exposure.
        Returns a size multiplier (0.0 to 1.0).
        """
        corr = self.correlation.correlation
        if corr is None:
            return 1.0

        same_direction = [
            p for p in existing_positions
            if p.get("direction") == direction and p.get("symbol") != symbol
        ]
        if not same_direction:
            return 1.0

        if abs(corr) > self._cfg.correlation_cap_threshold:
            cap = self._cfg.correlation_combined_cap
            existing_exposure = sum(
                abs(p.get("size", 0) * p.get("entry_price", 0))
                for p in same_direction
            )
            max_combined = self._account_balance * self._cfg.max_position_pct * cap
            remaining = max(max_combined - existing_exposure, 0)
            if remaining <= 0:
                return 0.0
            return min(remaining / (self._account_balance * self._cfg.max_position_pct), 1.0)

        return 1.0

    def record_trade_open(self) -> None:
        self.state.trade_count_today += 1
        self.state.open_positions += 1
        self.state.last_trade_ts = time.time()

    def record_trade_close(self, pnl: float) -> None:
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.daily_pnl += pnl
        self.state.weekly_pnl += pnl
        self.circuit_breaker.record_trade_result(pnl)
        self.sizer.record_result(pnl, 1.0)

    def update_var(
        self, positions: list[dict], atr_values: dict[str, float]
    ) -> float:
        var = self.var_calculator.estimate(positions, atr_values)
        self.state.portfolio_var = var
        return var

    def reset_daily(self) -> None:
        self.state.daily_pnl = 0.0
        self.state.trade_count_today = 0
        self.state.is_halted = False
        self.state.halt_reason = ""

    def reset_weekly(self) -> None:
        self.state.weekly_pnl = 0.0
