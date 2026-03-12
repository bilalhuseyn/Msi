from __future__ import annotations

from dataclasses import dataclass, field

from backtest.data_loader import BacktestTick
from backtest.metrics import PerformanceMetrics
from backtest.sim_engine import BacktestEngine, BacktestResult


@dataclass
class WalkForwardResult:
    """Results from walk-forward validation."""

    train_result: BacktestResult
    test_result: BacktestResult
    train_ticks: int = 0
    test_ticks: int = 0
    train_ratio: float = 0.70
    is_robust: bool = False
    degradation: dict = field(default_factory=dict)


class WalkForwardSplitter:
    """
    Walk-forward validation: split data chronologically into train/test
    periods. Train on the first portion, validate on the unseen remainder.

    PRD: 70% train / 30% out-of-sample test.
    """

    def __init__(self, train_ratio: float = 0.70):
        if not 0.1 <= train_ratio <= 0.95:
            raise ValueError(f"train_ratio must be between 0.1 and 0.95, got {train_ratio}")
        self._ratio = train_ratio

    def split(self, ticks: list[BacktestTick]) -> tuple[list[BacktestTick], list[BacktestTick]]:
        if not ticks:
            return [], []
        split_idx = int(len(ticks) * self._ratio)
        split_idx = max(1, min(split_idx, len(ticks) - 1))
        return ticks[:split_idx], ticks[split_idx:]

    def split_rolling(
        self,
        ticks: list[BacktestTick],
        window_size: int,
        step_size: int | None = None,
    ) -> list[tuple[list[BacktestTick], list[BacktestTick]]]:
        """
        Generate rolling train/test windows.
        Each window is `window_size` ticks, split by train_ratio.
        Step forward by `step_size` ticks (defaults to test portion size).
        """
        if not ticks or window_size > len(ticks):
            return []

        test_size = int(window_size * (1 - self._ratio))
        if step_size is None:
            step_size = max(1, test_size)

        windows = []
        start = 0
        while start + window_size <= len(ticks):
            window = ticks[start: start + window_size]
            train, test = self.split(window)
            windows.append((train, test))
            start += step_size

        return windows

    def validate(
        self,
        ticks: list[BacktestTick],
        engine: BacktestEngine,
        initial_balance: float = 10_000.0,
    ) -> WalkForwardResult:
        """
        Run full walk-forward validation: train on first portion,
        test on remaining. Compare metrics to assess robustness.
        """
        train_ticks, test_ticks = self.split(ticks)

        train_result = engine.run(train_ticks, initial_balance)
        test_result = engine.run(test_ticks, initial_balance)

        degradation = _calc_degradation(train_result.metrics, test_result.metrics)
        is_robust = _check_robustness(degradation)

        return WalkForwardResult(
            train_result=train_result,
            test_result=test_result,
            train_ticks=len(train_ticks),
            test_ticks=len(test_ticks),
            train_ratio=self._ratio,
            is_robust=is_robust,
            degradation=degradation,
        )


def _calc_degradation(train: PerformanceMetrics, test: PerformanceMetrics) -> dict:
    def _pct_change(a: float, b: float) -> float:
        if a == 0:
            return 0.0
        return (b - a) / abs(a)

    return {
        "win_rate_change": round(_pct_change(train.win_rate, test.win_rate), 4),
        "sharpe_change": round(_pct_change(train.sharpe_ratio, test.sharpe_ratio), 4),
        "profit_factor_change": round(_pct_change(train.profit_factor, test.profit_factor), 4),
        "max_dd_change": round(_pct_change(train.max_drawdown_pct, test.max_drawdown_pct), 4),
        "return_change": round(_pct_change(train.return_pct, test.return_pct), 4),
    }


def _check_robustness(degradation: dict) -> bool:
    """
    OOS is considered robust if key metrics don't degrade more than 30%.
    """
    wr = degradation.get("win_rate_change", -1)
    sr = degradation.get("sharpe_change", -1)
    pf = degradation.get("profit_factor_change", -1)

    if wr < -0.30 or sr < -0.50 or pf < -0.40:
        return False
    return True
