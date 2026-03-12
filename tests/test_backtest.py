"""
Phase 4 — Backtest Framework Tests.

Covers: data_loader, fee_model, portfolio, metrics, sim_engine,
walk_forward, monte_carlo, optimizer, report.
"""

from __future__ import annotations

import csv
import json
import math
import tempfile
from pathlib import Path

import pytest

from backtest.data_loader import BacktestTick, DataLoader, SyntheticGenerator
from backtest.fee_model import FeeModel, FillResult
from backtest.metrics import PerformanceMetrics, calculate_metrics
from backtest.monte_carlo import MonteCarloResult, MonteCarloSimulator
from backtest.optimizer import GridSearchOptimizer, OptimizationResult, ParameterSpace
from backtest.portfolio import Portfolio, Position, TradeRecord
from backtest.report import BacktestReport
from backtest.sim_engine import BacktestEngine, BacktestResult
from backtest.walk_forward import WalkForwardResult, WalkForwardSplitter

from config.settings import BacktestSettings


# ── Synthetic Data Fixtures ─────────────────────────────────────────


@pytest.fixture
def synthetic_ticks():
    return SyntheticGenerator.generate_random_walk(
        n_ticks=200, start_price=50_000, seed=42, tick_interval=60,
    )


@pytest.fixture
def small_ticks():
    return SyntheticGenerator.generate_random_walk(
        n_ticks=50, start_price=50_000, seed=123, tick_interval=60,
    )


@pytest.fixture
def fee_model():
    return FeeModel(taker_fee_pct=0.001, slippage_pct=0.0005, randomize_slippage=False)


@pytest.fixture
def portfolio(fee_model):
    return Portfolio(initial_balance=10_000, fee_model=fee_model, cooldown_seconds=0)


@pytest.fixture
def backtest_settings():
    return BacktestSettings(
        cooldown_seconds=0,
        taker_fee_pct=0.001,
        slippage_pct=0.0005,
    )


# ── DataLoader Tests ────────────────────────────────────────────────


class TestSyntheticGenerator:
    def test_random_walk_length(self):
        ticks = SyntheticGenerator.generate_random_walk(n_ticks=100, seed=1)
        assert len(ticks) == 100

    def test_random_walk_prices_positive(self):
        ticks = SyntheticGenerator.generate_random_walk(n_ticks=200, seed=1)
        for t in ticks:
            assert t.mid_price > 0
            assert t.best_bid > 0
            assert t.best_ask > 0
            assert t.best_ask >= t.best_bid

    def test_random_walk_has_trades(self):
        ticks = SyntheticGenerator.generate_random_walk(n_ticks=50, seed=1)
        for t in ticks:
            assert len(t.trades) > 0

    def test_random_walk_has_orderbook(self):
        ticks = SyntheticGenerator.generate_random_walk(n_ticks=50, seed=1)
        for t in ticks:
            assert len(t.bids) == 10
            assert len(t.asks) == 10

    def test_deterministic_with_seed(self):
        a = SyntheticGenerator.generate_random_walk(n_ticks=20, seed=42)
        b = SyntheticGenerator.generate_random_walk(n_ticks=20, seed=42)
        for ta, tb in zip(a, b):
            assert ta.mid_price == tb.mid_price

    def test_trend_affects_direction(self):
        up = SyntheticGenerator.generate_random_walk(n_ticks=500, trend=0.005, seed=10)
        down = SyntheticGenerator.generate_random_walk(n_ticks=500, trend=-0.005, seed=10)
        assert up[-1].mid_price > down[-1].mid_price

    def test_generate_book_side(self):
        bids = SyntheticGenerator.generate_book_side(50_000, 1.0, depth=5, ascending=False)
        assert len(bids) == 5
        for level in bids:
            assert level["price"] < 50_000
            assert level["qty"] > 0


class TestDataLoaderCSV:
    def test_load_klines(self, tmp_path):
        csv_path = tmp_path / "klines.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            w.writeheader()
            for i in range(10):
                w.writerow({
                    "timestamp": 1700000000 + i * 60,
                    "open": 50000 + i, "high": 50010 + i,
                    "low": 49990 + i, "close": 50005 + i, "volume": 100,
                })
        ticks = DataLoader.load_klines(csv_path)
        assert len(ticks) == 10
        assert all(t.mid_price > 0 for t in ticks)

    def test_load_klines_with_limit(self, tmp_path):
        csv_path = tmp_path / "klines.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            w.writeheader()
            for i in range(100):
                w.writerow({
                    "timestamp": 1700000000 + i * 60,
                    "open": 50000, "high": 50010, "low": 49990, "close": 50005, "volume": 100,
                })
        ticks = DataLoader.load_klines(csv_path, limit=25)
        assert len(ticks) == 25

    def test_load_trades_csv(self, tmp_path):
        csv_path = tmp_path / "trades.csv"
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["timestamp", "price", "qty", "side"])
            w.writeheader()
            for i in range(20):
                w.writerow({
                    "timestamp": 1700000000 + i,
                    "price": 50000 + i, "qty": 0.1, "side": "buy" if i % 2 == 0 else "sell",
                })
        trades = DataLoader.load_trades(csv_path)
        assert len(trades) == 20
        assert trades[0].side in ("buy", "sell")

    def test_load_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            DataLoader.load_klines("nonexistent.csv")


# ── FeeModel Tests ──────────────────────────────────────────────────


class TestFeeModel:
    def test_entry_long_slippage(self, fee_model):
        fill = fee_model.apply_entry(50_000, 0.1, direction=1)
        assert fill.fill_price > 50_000
        assert fill.fee_usd > 0
        assert fill.slippage_usd >= 0

    def test_entry_short_slippage(self, fee_model):
        fill = fee_model.apply_entry(50_000, 0.1, direction=-1)
        assert fill.fill_price < 50_000

    def test_exit_long_slippage(self, fee_model):
        fill = fee_model.apply_exit(51_000, 0.1, direction=1)
        assert fill.fill_price < 51_000

    def test_fee_calculation(self, fee_model):
        fee = fee_model.calculate_fee(10_000)
        assert fee == pytest.approx(10.0)

    def test_total_cost_positive(self, fee_model):
        fill = fee_model.apply_entry(50_000, 0.1, direction=1)
        assert fill.total_cost > 0
        assert fill.total_cost == pytest.approx(fill.fee_usd + fill.slippage_usd)


# ── Portfolio Tests ─────────────────────────────────────────────────


class TestPortfolio:
    def test_open_position(self, portfolio):
        pos = portfolio.open_position(
            price=50_000, size=0.1, direction=1,
            stop_price=49_500, tp1_price=50_750, tp2_price=51_250,
            timestamp=1700000000,
        )
        assert pos is not None
        assert portfolio.open_position_count == 1
        assert portfolio.balance < 10_000  # Fee deducted

    def test_max_positions_enforced(self, portfolio):
        for i in range(2):
            portfolio.open_position(
                50_000, 0.1, 1, 49_500, 50_750, 51_250, 1700000000 + i,
            )
        pos3 = portfolio.open_position(
            50_000, 0.1, 1, 49_500, 50_750, 51_250, 1700000000 + 10,
        )
        assert pos3 is None
        assert portfolio.open_position_count == 2

    def test_stop_loss_closes_position(self, portfolio):
        portfolio.open_position(50_000, 0.1, 1, 49_500, 50_750, 51_250, 1700000000)
        closed = portfolio.update(49_400, 1700000100)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"
        assert closed[0].pnl_usd < 0
        assert portfolio.open_position_count == 0

    def test_tp1_partial_exit(self, portfolio):
        portfolio.open_position(50_000, 1.0, 1, 49_000, 51_500, 52_500, 1700000000)
        closed = portfolio.update(51_600, 1700000100)
        assert len(closed) == 1
        assert closed[0].exit_reason == "tp1"
        assert closed[0].size == pytest.approx(0.5)
        assert portfolio.open_position_count == 1

    def test_time_exit(self, portfolio):
        portfolio._max_hold = 3600
        portfolio.open_position(50_000, 0.1, 1, 49_000, 51_500, 52_500, 1700000000)
        closed = portfolio.update(50_100, 1700004000)
        assert len(closed) == 1
        assert closed[0].exit_reason == "time_exit"

    def test_force_close_all(self, portfolio):
        portfolio.open_position(50_000, 0.1, 1, 49_500, 50_750, 51_250, 1700000000)
        portfolio.open_position(50_000, 0.1, -1, 50_500, 49_250, 48_750, 1700000001)
        closed = portfolio.force_close_all(50_100, 1700000100)
        assert len(closed) == 2
        assert portfolio.open_position_count == 0

    def test_equity_curve_recorded(self, portfolio):
        portfolio.open_position(50_000, 0.1, 1, 49_500, 51_500, 52_500, 1700000000)
        portfolio.update(50_500, 1700000060)
        portfolio.update(51_000, 1700000120)
        assert len(portfolio.equity_curve) == 2

    def test_short_stop_loss(self, portfolio):
        portfolio.open_position(50_000, 0.1, -1, 50_500, 49_250, 48_500, 1700000000)
        closed = portfolio.update(50_600, 1700000100)
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop_loss"
        assert closed[0].pnl_usd < 0

    def test_reset(self, portfolio):
        portfolio.open_position(50_000, 0.1, 1, 49_500, 51_500, 52_500, 1700000000)
        portfolio.reset()
        assert portfolio.balance == 10_000
        assert portfolio.open_position_count == 0
        assert len(portfolio.trades) == 0

    def test_cooldown_enforced(self, fee_model):
        p = Portfolio(initial_balance=10_000, fee_model=fee_model, cooldown_seconds=900)
        p.open_position(50_000, 0.1, 1, 49_500, 51_500, 52_500, 1700000000)
        can, reason = p.can_open(1700000500)
        assert not can
        assert reason == "cooldown_active"


# ── Position Tests ──────────────────────────────────────────────────


class TestPosition:
    def test_unrealized_pnl_long(self):
        pos = Position(
            entry_price=50_000, size=0.1, direction=1,
            stop_price=49_500, tp1_price=51_000, tp2_price=52_000,
            entry_ts=0,
        )
        assert pos.unrealized_pnl(51_000) == pytest.approx(100.0)

    def test_unrealized_pnl_short(self):
        pos = Position(
            entry_price=50_000, size=0.1, direction=-1,
            stop_price=50_500, tp1_price=49_000, tp2_price=48_000,
            entry_ts=0,
        )
        assert pos.unrealized_pnl(49_000) == pytest.approx(100.0)

    def test_initial_risk(self):
        pos = Position(
            entry_price=50_000, size=0.1, direction=1,
            stop_price=49_500, tp1_price=51_000, tp2_price=52_000,
            entry_ts=0,
        )
        assert pos.initial_risk == pytest.approx(50.0)


# ── Metrics Tests ───────────────────────────────────────────────────


class TestMetrics:
    def _make_trades(self, wins: int, losses: int, win_avg: float = 100, loss_avg: float = -50) -> list[TradeRecord]:
        trades = []
        ts = 1700000000
        for i in range(wins):
            trades.append(TradeRecord(
                entry_price=50_000, exit_price=50_100, direction=1, size=0.01,
                entry_ts=ts, exit_ts=ts + 3600, pnl_usd=win_avg,
                pnl_r=2.0, fees_total=1.0, exit_reason="tp1",
            ))
            ts += 86400
        for i in range(losses):
            trades.append(TradeRecord(
                entry_price=50_000, exit_price=49_900, direction=1, size=0.01,
                entry_ts=ts, exit_ts=ts + 3600, pnl_usd=loss_avg,
                pnl_r=-1.0, fees_total=1.0, exit_reason="stop_loss",
            ))
            ts += 86400
        return trades

    def test_win_rate(self):
        trades = self._make_trades(6, 4)
        m = calculate_metrics(trades, [], 10_000)
        assert m.win_rate == pytest.approx(0.6)
        assert m.winners == 6
        assert m.losers == 4

    def test_profit_factor(self):
        trades = self._make_trades(6, 4, win_avg=100, loss_avg=-50)
        m = calculate_metrics(trades, [], 10_000)
        assert m.gross_profit == pytest.approx(600)
        assert m.gross_loss == pytest.approx(200)
        assert m.profit_factor == pytest.approx(3.0)

    def test_avg_rr(self):
        trades = self._make_trades(5, 5, win_avg=150, loss_avg=-75)
        m = calculate_metrics(trades, [], 10_000)
        assert m.avg_rr == pytest.approx(2.0)

    def test_empty_trades(self):
        m = calculate_metrics([], [], 10_000)
        assert m.total_trades == 0
        assert m.final_balance == 10_000

    def test_max_drawdown(self):
        curve = [
            (1, 10000), (2, 10500), (3, 10200), (4, 9800),
            (5, 10100), (6, 9500), (7, 9900),
        ]
        m = calculate_metrics([], curve, 10_000)
        assert m.max_drawdown_usd == pytest.approx(1000.0)

    def test_meets_targets(self):
        trades = self._make_trades(7, 3, win_avg=200, loss_avg=-80)
        m = calculate_metrics(trades, [], 10_000)
        assert m.meets_targets["win_rate_target"] is True
        assert m.meets_targets["win_rate_min"] is True

    def test_net_pnl(self):
        trades = self._make_trades(5, 5, win_avg=120, loss_avg=-100)
        m = calculate_metrics(trades, [], 10_000)
        assert m.net_pnl == pytest.approx(100.0)
        assert m.return_pct == pytest.approx(0.01)

    def test_sharpe_with_positive_returns(self):
        curve = [(i * 86400, 10_000 + i * 50) for i in range(30)]
        trades = self._make_trades(10, 5)
        m = calculate_metrics(trades, curve, 10_000)
        assert m.sharpe_ratio > 0


# ── Walk-Forward Tests ──────────────────────────────────────────────


class TestWalkForward:
    def test_split_ratio(self, synthetic_ticks):
        splitter = WalkForwardSplitter(train_ratio=0.70)
        train, test = splitter.split(synthetic_ticks)
        assert len(train) == 140
        assert len(test) == 60

    def test_split_empty(self):
        splitter = WalkForwardSplitter(train_ratio=0.70)
        train, test = splitter.split([])
        assert train == []
        assert test == []

    def test_invalid_ratio_raises(self):
        with pytest.raises(ValueError):
            WalkForwardSplitter(train_ratio=0.05)

    def test_rolling_windows(self, synthetic_ticks):
        splitter = WalkForwardSplitter(train_ratio=0.70)
        windows = splitter.split_rolling(synthetic_ticks, window_size=100)
        assert len(windows) >= 2
        for train, test in windows:
            assert len(train) > 0
            assert len(test) > 0
            assert len(train) + len(test) == 100

    def test_train_before_test(self, synthetic_ticks):
        splitter = WalkForwardSplitter(train_ratio=0.70)
        train, test = splitter.split(synthetic_ticks)
        assert train[-1].timestamp < test[0].timestamp


# ── Monte Carlo Tests ───────────────────────────────────────────────


class TestMonteCarlo:
    def _sample_trades(self) -> list[TradeRecord]:
        trades = []
        ts = 1700000000
        for i in range(50):
            pnl = 100 if i % 3 != 0 else -80
            trades.append(TradeRecord(
                entry_price=50_000, exit_price=50_100 if pnl > 0 else 49_900,
                direction=1, size=0.01, entry_ts=ts, exit_ts=ts + 3600,
                pnl_usd=pnl, pnl_r=2.0 if pnl > 0 else -1.0,
                fees_total=1.0, exit_reason="tp1" if pnl > 0 else "stop_loss",
            ))
            ts += 86400
        return trades

    def test_mc_iterations(self):
        mc = MonteCarloSimulator(iterations=100, seed=42)
        result = mc.simulate(self._sample_trades(), 10_000)
        assert result.iterations == 100
        assert len(result.all_max_drawdowns) == 100

    def test_mc_drawdown_bounds(self):
        mc = MonteCarloSimulator(iterations=500, seed=42)
        result = mc.simulate(self._sample_trades(), 10_000)
        assert result.best_dd >= 0
        assert result.worst_dd >= result.best_dd
        assert result.percentile_95_dd <= result.worst_dd

    def test_mc_median_reasonable(self):
        mc = MonteCarloSimulator(iterations=500, seed=42)
        result = mc.simulate(self._sample_trades(), 10_000)
        assert 0 < result.median_max_dd < 1.0

    def test_mc_empty_trades(self):
        mc = MonteCarloSimulator(iterations=10)
        result = mc.simulate([], 10_000)
        assert result.iterations == 10
        assert len(result.all_max_drawdowns) == 0

    def test_confidence_interval(self):
        mc = MonteCarloSimulator(iterations=500, seed=42)
        low, high = mc.confidence_interval(self._sample_trades(), 10_000, 0.95)
        assert low <= high

    def test_ruin_probability_bounded(self):
        mc = MonteCarloSimulator(iterations=200, seed=42, ruin_threshold=0.50)
        result = mc.simulate(self._sample_trades(), 10_000)
        assert 0.0 <= result.ruin_probability <= 1.0


# ── Optimizer Tests ─────────────────────────────────────────────────


class TestParameterSpace:
    def test_total_combinations(self):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.60, 0.65],
            obi_consistency_window=[3],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        assert ps.total_combinations == 2

    def test_iter_grid_yields_all(self):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.60, 0.65],
            obi_consistency_window=[3, 4],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        combos = list(ps.iter_grid())
        assert len(combos) == 4

    def test_sample_count(self):
        ps = ParameterSpace()
        samples = ps.sample(10, seed=42)
        assert len(samples) == 10
        assert all("obi_bullish_threshold" in s for s in samples)

    def test_grid_params_complete(self):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.65],
            obi_consistency_window=[3],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        combos = list(ps.iter_grid())
        assert len(combos) == 1
        c = combos[0]
        assert c["obi_bullish_threshold"] == 0.65
        assert c["confirmation_threshold"] == 0.35


class TestGridSearchOptimizer:
    def test_run_small_grid(self, small_ticks, backtest_settings):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.65],
            obi_consistency_window=[3],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        opt = GridSearchOptimizer(
            ticks=small_ticks, parameter_space=ps,
            backtest_settings=backtest_settings,
        )
        results = opt.run_full_grid()
        assert len(results) == 1
        assert results[0].backtest_result is not None

    def test_random_search(self, small_ticks, backtest_settings):
        opt = GridSearchOptimizer(
            ticks=small_ticks,
            backtest_settings=backtest_settings,
        )
        results = opt.run_random_search(n_samples=3, seed=42)
        assert len(results) == 3
        assert opt.best_result is not None

    def test_max_combinations_cap(self, small_ticks, backtest_settings):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.60, 0.65, 0.70],
            obi_consistency_window=[3, 4],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        opt = GridSearchOptimizer(
            ticks=small_ticks, parameter_space=ps,
            backtest_settings=backtest_settings,
        )
        results = opt.run_full_grid(max_combinations=2)
        assert len(results) == 2


# ── Simulation Engine Tests ─────────────────────────────────────────


class TestBacktestEngine:
    def test_run_produces_result(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)
        assert isinstance(result, BacktestResult)
        assert result.total_ticks == len(synthetic_ticks)

    def test_balance_changes(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)
        assert result.final_balance != result.initial_balance or len(result.trades) == 0

    def test_equity_curve_populated(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)
        assert len(result.equity_curve) > 0

    def test_metrics_calculated(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)
        assert result.metrics is not None
        assert result.metrics.final_balance > 0

    def test_decisions_logged(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)
        assert isinstance(result.decisions, list)

    def test_custom_initial_balance(self, small_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(small_ticks, initial_balance=50_000)
        assert result.initial_balance == 50_000

    def test_empty_ticks(self, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run([])
        assert result.total_ticks == 0
        assert result.final_balance == result.initial_balance


# ── Report Tests ────────────────────────────────────────────────────


class TestReport:
    def _make_result(self) -> BacktestResult:
        trades = [
            TradeRecord(50000, 50500, 1, 0.1, 1700000000, 1700003600,
                        50.0, 2.0, 1.0, "tp1"),
            TradeRecord(50500, 50200, -1, 0.1, 1700003700, 1700007200,
                        30.0, 1.5, 1.0, "tp1"),
            TradeRecord(50200, 49800, 1, 0.1, 1700007300, 1700010800,
                        -40.0, -1.0, 1.0, "stop_loss"),
        ]
        curve = [(1700000000 + i * 3600, 10000 + i * 10) for i in range(10)]
        metrics = calculate_metrics(trades, curve, 10000)
        return BacktestResult(
            trades=trades, equity_curve=curve, metrics=metrics,
            initial_balance=10000, final_balance=10040, total_ticks=100,
            veto_count=5, signal_count=10,
        )

    def test_text_report_generated(self):
        report = BacktestReport(self._make_result())
        text = report.generate_text()
        assert "OFI Pro" in text
        assert "Trade Statistics" in text
        assert "PRD Target Check" in text

    def test_json_report_generated(self):
        report = BacktestReport(self._make_result())
        data = report.generate_json()
        assert "metrics" in data
        assert "backtest" in data
        assert data["backtest"]["total_ticks"] == 100

    def test_report_with_mc(self):
        report = BacktestReport(self._make_result())
        mc = MonteCarloResult(iterations=100, median_max_dd=0.05, percentile_95_dd=0.10)
        report.set_monte_carlo(mc)
        text = report.generate_text()
        assert "Monte Carlo" in text
        data = report.generate_json()
        assert "monte_carlo" in data

    def test_report_save(self, tmp_path):
        report = BacktestReport(self._make_result())
        txt_path, json_path = report.save(tmp_path, prefix="test")
        assert txt_path.exists()
        assert json_path.exists()
        content = json.loads(json_path.read_text())
        assert content["backtest"]["total_ticks"] == 100


# ── Options Layer in Backtest (#3 Resolution) ──────────────────────


class TestOptionsRegimeInBacktest:
    def test_default_regime_is_neutral(self, small_ticks, backtest_settings):
        # P5 fix: default options_regime is NEUTRAL → no regime multiplier applied
        engine = BacktestEngine(backtest_settings=backtest_settings)
        assert engine._regime is None

    def test_short_gamma_regime(self, small_ticks):
        bt = BacktestSettings(cooldown_seconds=0, options_regime="SHORT_GAMMA")
        engine = BacktestEngine(backtest_settings=bt)
        assert engine._regime is not None
        assert engine._regime.regime.value == "SHORT_GAMMA"
        assert engine._regime.multiplier == pytest.approx(1.20)

    def test_neutral_regime_is_none(self, small_ticks):
        bt = BacktestSettings(cooldown_seconds=0, options_regime="NEUTRAL")
        engine = BacktestEngine(backtest_settings=bt)
        assert engine._regime is None

    def test_regime_affects_results(self, synthetic_ticks):
        bt_long = BacktestSettings(cooldown_seconds=0, options_regime="LONG_GAMMA")
        bt_short = BacktestSettings(cooldown_seconds=0, options_regime="SHORT_GAMMA")
        bt_neutral = BacktestSettings(cooldown_seconds=0, options_regime="NEUTRAL")

        r_long = BacktestEngine(backtest_settings=bt_long).run(synthetic_ticks)
        r_short = BacktestEngine(backtest_settings=bt_short).run(synthetic_ticks)
        r_neutral = BacktestEngine(backtest_settings=bt_neutral).run(synthetic_ticks)

        assert isinstance(r_long, BacktestResult)
        assert isinstance(r_short, BacktestResult)
        assert isinstance(r_neutral, BacktestResult)

    def test_options_module_reset_on_run(self, small_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        engine.run(small_ticks)
        assert engine._options.last_regime is None


# ── Daily Trade Limit Compliance (#4) ───────────────────────────────


class TestDailyLimitCompliance:
    def _make_trades_same_day(self, count: int) -> list[TradeRecord]:
        trades = []
        ts = 1700000000
        for i in range(count):
            trades.append(TradeRecord(
                entry_price=50_000, exit_price=50_100, direction=1, size=0.01,
                entry_ts=ts + i * 100, exit_ts=ts + i * 100 + 3600,
                pnl_usd=50.0, pnl_r=1.0, fees_total=1.0, exit_reason="tp1",
            ))
        return trades

    def test_under_limit_passes(self):
        trades = self._make_trades_same_day(6)
        m = calculate_metrics(trades, [], 10_000)
        assert m.max_trades_in_a_day == 6
        assert m.days_exceeding_limit == 0
        assert m.meets_targets["daily_limit_respected"] is True

    def test_over_limit_detected(self):
        trades = self._make_trades_same_day(10)
        m = calculate_metrics(trades, [], 10_000)
        assert m.max_trades_in_a_day == 10
        assert m.days_exceeding_limit == 1
        assert m.meets_targets["daily_limit_respected"] is False

    def test_exactly_at_limit_passes(self):
        trades = self._make_trades_same_day(8)
        m = calculate_metrics(trades, [], 10_000)
        assert m.max_trades_in_a_day == 8
        assert m.days_exceeding_limit == 0
        assert m.meets_targets["daily_limit_respected"] is True


# ── Integration: Full Pipeline ──────────────────────────────────────


class TestFullPipeline:
    def test_backtest_then_monte_carlo(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)

        mc = MonteCarloSimulator(iterations=50, seed=42)
        mc_result = mc.simulate(result.trades, result.initial_balance)
        assert mc_result.iterations == 50

    def test_backtest_then_report(self, synthetic_ticks, backtest_settings):
        engine = BacktestEngine(backtest_settings=backtest_settings)
        result = engine.run(synthetic_ticks)

        report = BacktestReport(result)
        text = report.generate_text()
        assert len(text) > 100

    def test_walk_forward_full(self, synthetic_ticks, backtest_settings):
        splitter = WalkForwardSplitter(train_ratio=0.70)
        engine = BacktestEngine(backtest_settings=backtest_settings)
        wf_result = splitter.validate(synthetic_ticks, engine)
        assert wf_result.train_ticks > 0
        assert wf_result.test_ticks > 0
        assert isinstance(wf_result.is_robust, bool)

    def test_optimizer_then_report(self, small_ticks, backtest_settings):
        ps = ParameterSpace(
            obi_bullish_threshold=[0.65],
            obi_consistency_window=[3],
            vpin_veto_threshold=[0.65],
            depth_erosion_threshold=[0.35],
            spoof_cancel_window_ms=[800],
            confirmation_threshold=[0.35],
        )
        opt = GridSearchOptimizer(
            ticks=small_ticks, parameter_space=ps,
            backtest_settings=backtest_settings,
        )
        results = opt.run_full_grid()
        best = opt.best_result
        assert best is not None

        report = BacktestReport(best.backtest_result)
        data = report.generate_json()
        assert "metrics" in data
