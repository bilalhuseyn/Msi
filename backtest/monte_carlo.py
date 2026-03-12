from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from backtest.portfolio import TradeRecord


@dataclass
class MonteCarloResult:
    """Results from Monte Carlo drawdown simulation."""

    iterations: int = 0
    median_max_dd: float = 0.0
    mean_max_dd: float = 0.0
    percentile_95_dd: float = 0.0
    percentile_99_dd: float = 0.0
    worst_dd: float = 0.0
    best_dd: float = 0.0
    median_final_balance: float = 0.0
    percentile_5_balance: float = 0.0
    ruin_probability: float = 0.0
    all_max_drawdowns: list[float] = field(default_factory=list)


class MonteCarloSimulator:
    """
    Monte Carlo simulation for drawdown analysis.

    PRD: 1,000 iterations with different trade orderings
    to estimate max drawdown at 95th percentile confidence.
    """

    def __init__(
        self,
        iterations: int = 1000,
        ruin_threshold: float = 0.50,
        seed: int | None = None,
    ):
        self._iterations = iterations
        self._ruin_threshold = ruin_threshold
        self._seed = seed

    def simulate(
        self,
        trades: list[TradeRecord],
        initial_balance: float = 10_000.0,
    ) -> MonteCarloResult:
        if not trades:
            return MonteCarloResult(iterations=self._iterations)

        if self._seed is not None:
            random.seed(self._seed)

        pnl_sequence = [t.pnl_usd for t in trades]
        max_drawdowns: list[float] = []
        final_balances: list[float] = []
        ruin_count = 0

        for _ in range(self._iterations):
            shuffled = pnl_sequence.copy()
            random.shuffle(shuffled)

            peak = initial_balance
            balance = initial_balance
            max_dd = 0.0

            for pnl in shuffled:
                balance += pnl
                if balance > peak:
                    peak = balance
                dd = (peak - balance) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

                if balance <= initial_balance * (1 - self._ruin_threshold):
                    ruin_count += 1
                    break

            max_drawdowns.append(max_dd)
            final_balances.append(balance)

        max_drawdowns.sort()
        final_balances.sort()

        n = len(max_drawdowns)

        return MonteCarloResult(
            iterations=self._iterations,
            median_max_dd=max_drawdowns[n // 2],
            mean_max_dd=sum(max_drawdowns) / n,
            percentile_95_dd=max_drawdowns[int(n * 0.95)],
            percentile_99_dd=max_drawdowns[int(n * 0.99)] if n > 100 else max_drawdowns[-1],
            worst_dd=max_drawdowns[-1],
            best_dd=max_drawdowns[0],
            median_final_balance=final_balances[n // 2],
            percentile_5_balance=final_balances[int(n * 0.05)],
            ruin_probability=ruin_count / self._iterations,
            all_max_drawdowns=max_drawdowns,
        )

    def confidence_interval(
        self,
        trades: list[TradeRecord],
        initial_balance: float = 10_000.0,
        confidence: float = 0.95,
    ) -> tuple[float, float]:
        """
        Return (lower, upper) drawdown bounds at given confidence level.
        """
        result = self.simulate(trades, initial_balance)
        if not result.all_max_drawdowns:
            return 0.0, 0.0

        n = len(result.all_max_drawdowns)
        lower_idx = int(n * (1 - confidence) / 2)
        upper_idx = int(n * (1 + confidence) / 2) - 1
        upper_idx = min(upper_idx, n - 1)

        return result.all_max_drawdowns[lower_idx], result.all_max_drawdowns[upper_idx]
