from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Any

from backtest.data_loader import BacktestTick
from backtest.sim_engine import BacktestEngine, BacktestResult

from config.settings import (
    BacktestSettings,
    ClearanceSettings,
    ConfirmationSettings,
    DepthErosionSettings,
    OBISettings,
    SpoofingSettings,
    SpreadSettings,
    VPINSettings,
)

logger = logging.getLogger(__name__)


@dataclass
class ParameterSpace:
    """
    Defines the optimization ranges for each parameter.
    Based on PRD Section 12.2.
    """

    obi_bullish_threshold: list[float] = field(default_factory=lambda: [0.60, 0.63, 0.65, 0.68, 0.70])
    obi_consistency_window: list[int] = field(default_factory=lambda: [2, 3, 4, 5])
    vpin_veto_threshold: list[float] = field(default_factory=lambda: [0.60, 0.62, 0.65, 0.68, 0.70])
    depth_erosion_threshold: list[float] = field(default_factory=lambda: [0.25, 0.30, 0.35, 0.40, 0.45])
    spoof_cancel_window_ms: list[int] = field(default_factory=lambda: [400, 600, 800, 1000, 1200])
    confirmation_threshold: list[float] = field(default_factory=lambda: [0.25, 0.30, 0.35, 0.40, 0.45])

    @property
    def total_combinations(self) -> int:
        return (
            len(self.obi_bullish_threshold)
            * len(self.obi_consistency_window)
            * len(self.vpin_veto_threshold)
            * len(self.depth_erosion_threshold)
            * len(self.spoof_cancel_window_ms)
            * len(self.confirmation_threshold)
        )

    def iter_grid(self):
        """Yield all parameter combinations as flat dicts."""
        for combo in itertools.product(
            self.obi_bullish_threshold,
            self.obi_consistency_window,
            self.vpin_veto_threshold,
            self.depth_erosion_threshold,
            self.spoof_cancel_window_ms,
            self.confirmation_threshold,
        ):
            yield {
                "obi_bullish_threshold": combo[0],
                "obi_consistency_window": combo[1],
                "vpin_veto_threshold": combo[2],
                "depth_erosion_threshold": combo[3],
                "spoof_cancel_window_ms": combo[4],
                "confirmation_threshold": combo[5],
            }

    def sample(self, n: int, seed: int | None = None) -> list[dict]:
        """Random sample from the parameter space for faster exploration."""
        import random
        if seed is not None:
            random.seed(seed)

        samples = []
        for _ in range(n):
            samples.append({
                "obi_bullish_threshold": random.choice(self.obi_bullish_threshold),
                "obi_consistency_window": random.choice(self.obi_consistency_window),
                "vpin_veto_threshold": random.choice(self.vpin_veto_threshold),
                "depth_erosion_threshold": random.choice(self.depth_erosion_threshold),
                "spoof_cancel_window_ms": random.choice(self.spoof_cancel_window_ms),
                "confirmation_threshold": random.choice(self.confirmation_threshold),
            })
        return samples


@dataclass
class OptimizationResult:
    """Result of a single parameter combination."""

    params: dict
    backtest_result: BacktestResult
    objective_score: float = 0.0


class GridSearchOptimizer:
    """
    Grid search parameter optimization.

    Evaluates every parameter combination (or a random subset) against
    historical data, ranking by a composite objective function.
    """

    def __init__(
        self,
        ticks: list[BacktestTick],
        parameter_space: ParameterSpace | None = None,
        backtest_settings: BacktestSettings | None = None,
        initial_balance: float = 10_000.0,
        objective: str = "sharpe",
    ):
        self._ticks = ticks
        self._space = parameter_space or ParameterSpace()
        self._bt_settings = backtest_settings or BacktestSettings()
        self._balance = initial_balance
        self._objective = objective
        self._results: list[OptimizationResult] = []

    def _build_engine(self, params: dict) -> BacktestEngine:
        obi_bull = params["obi_bullish_threshold"]
        return BacktestEngine(
            obi_settings=OBISettings(
                bullish_threshold=obi_bull,
                bearish_threshold=round(1.0 - obi_bull, 2),
                consistency_window=params["obi_consistency_window"],
            ),
            vpin_settings=VPINSettings(
                veto_threshold=params["vpin_veto_threshold"],
                warning_threshold=max(params["vpin_veto_threshold"] - 0.10, 0.40),
            ),
            depth_settings=DepthErosionSettings(
                erosion_threshold=params["depth_erosion_threshold"],
            ),
            spoof_settings=SpoofingSettings(
                cancel_window_ms=params["spoof_cancel_window_ms"],
            ),
            confirmation_settings=ConfirmationSettings(
                long_threshold=params["confirmation_threshold"],
                short_threshold=-params["confirmation_threshold"],
            ),
            backtest_settings=self._bt_settings,
        )

    def run_full_grid(self, max_combinations: int | None = None) -> list[OptimizationResult]:
        """Run all parameter combinations (or capped at max_combinations)."""
        count = 0
        for params in self._space.iter_grid():
            if max_combinations is not None and count >= max_combinations:
                break
            result = self._evaluate(params)
            self._results.append(result)
            count += 1
            if count % 50 == 0:
                logger.info("Grid search: %d/%d combinations evaluated", count, self._space.total_combinations)

        self._results.sort(key=lambda r: r.objective_score, reverse=True)
        return self._results

    def run_random_search(self, n_samples: int = 100, seed: int | None = None) -> list[OptimizationResult]:
        """Random search over parameter space for faster exploration."""
        samples = self._space.sample(n_samples, seed)
        for i, params in enumerate(samples):
            result = self._evaluate(params)
            self._results.append(result)
            if (i + 1) % 20 == 0:
                logger.info("Random search: %d/%d samples evaluated", i + 1, n_samples)

        self._results.sort(key=lambda r: r.objective_score, reverse=True)
        return self._results

    def _evaluate(self, params: dict) -> OptimizationResult:
        engine = self._build_engine(params)
        bt_result = engine.run(self._ticks, self._balance)
        bt_result.params = params
        score = self._calc_objective(bt_result)
        return OptimizationResult(params=params, backtest_result=bt_result, objective_score=score)

    def _calc_objective(self, result: BacktestResult) -> float:
        m = result.metrics
        if m.total_trades < 5:
            return -999.0

        if self._objective == "sharpe":
            return m.sharpe_ratio
        elif self._objective == "profit_factor":
            return m.profit_factor if m.profit_factor != float("inf") else 10.0
        elif self._objective == "composite":
            return _composite_score(m)
        return m.sharpe_ratio

    @property
    def best_result(self) -> OptimizationResult | None:
        if not self._results:
            return None
        return self._results[0]

    @property
    def top_n(self) -> list[OptimizationResult]:
        return self._results[:10]

    @property
    def all_results(self) -> list[OptimizationResult]:
        return list(self._results)


def _composite_score(m) -> float:
    """
    Multi-objective score balancing Sharpe, win rate, profit factor,
    and max drawdown into a single ranking number.
    """
    sharpe_component = min(m.sharpe_ratio / 2.0, 1.0) * 0.35
    wr_component = min(m.win_rate / 0.60, 1.0) * 0.20
    pf_raw = m.profit_factor if m.profit_factor != float("inf") else 5.0
    pf_component = min(pf_raw / 2.0, 1.0) * 0.25
    dd_component = max(1.0 - m.max_drawdown_pct / 0.15, 0.0) * 0.20

    return sharpe_component + wr_component + pf_component + dd_component
