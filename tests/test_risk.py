from __future__ import annotations

import time
import pytest
from config.settings import RiskSettings
from risk.circuit_breaker import CircuitBreaker, DrawdownScaler
from risk.position_sizer import PositionSizer
from risk.risk_manager import (
    RiskManager, CorrelationTracker, FundingRateTracker, PortfolioVaR,
)


class TestCircuitBreaker:
    def test_resets_on_win(self):
        cb = CircuitBreaker()
        cb.record_trade_result(-100)
        cb.record_trade_result(-100)
        assert cb.state.consecutive_losses == 2
        cb.record_trade_result(50)
        assert cb.state.consecutive_losses == 0

    def test_reduces_size_at_threshold(self):
        cb = CircuitBreaker(RiskSettings(consecutive_loss_reduce_at=3))
        for _ in range(3):
            cb.record_trade_result(-50)
        assert cb.state.size_multiplier == 0.50
        assert cb.state.reduced_trades_remaining == 2

    def test_cooldown_at_five_losses(self):
        cb = CircuitBreaker(RiskSettings(consecutive_loss_cooldown_at=5))
        for _ in range(5):
            cb.record_trade_result(-50)
        can, reason = cb.can_trade()
        assert not can
        assert "cooldown" in reason.lower()

    def test_can_trade_after_cooldown(self):
        cb = CircuitBreaker(RiskSettings(consecutive_loss_cooldown_at=5))
        for _ in range(5):
            cb.record_trade_result(-50)
        cb.state.cooldown_until = time.time() - 1
        can, _ = cb.can_trade()
        assert can

    def test_get_size_multiplier_decrement(self):
        cb = CircuitBreaker(RiskSettings(consecutive_loss_reduce_at=3))
        for _ in range(3):
            cb.record_trade_result(-50)
        m1 = cb.get_size_multiplier()
        assert m1 == 0.50
        m2 = cb.get_size_multiplier()
        assert m2 == 0.50
        m3 = cb.get_size_multiplier()
        assert m3 == 1.0


class TestDrawdownScaler:
    def test_no_loss_full_risk(self):
        ds = DrawdownScaler()
        mult, halt = ds.get_risk_multiplier(0.0, 10000)
        assert mult == 1.0
        assert not halt

    def test_moderate_loss_reduces(self):
        ds = DrawdownScaler()
        mult, halt = ds.get_risk_multiplier(0.022, 10000)
        assert mult < 1.0
        assert not halt

    def test_max_loss_halts(self):
        ds = DrawdownScaler()
        mult, halt = ds.get_risk_multiplier(0.035, 10000)
        assert halt

    def test_progressive_scaling(self):
        ds = DrawdownScaler()
        m1, _ = ds.get_risk_multiplier(0.005, 10000)
        m2, _ = ds.get_risk_multiplier(0.015, 10000)
        m3, _ = ds.get_risk_multiplier(0.025, 10000)
        assert m1 >= m2 >= m3


class TestPositionSizer:
    def test_zero_balance_returns_zero(self):
        ps = PositionSizer()
        assert ps.calculate(0, 0.5, 100) == 0.0

    def test_below_threshold_returns_zero(self):
        ps = PositionSizer()
        assert ps.calculate(10000, 0.20, 100) == 0.0

    def test_weak_signal_small_size(self):
        ps = PositionSizer()
        weak = ps.calculate(10000, 0.40, 100)
        strong = ps.calculate(10000, 0.80, 100)
        assert weak < strong

    def test_vpin_warning_halves_size(self):
        ps = PositionSizer()
        normal = ps.calculate(10000, 0.50, 100, vpin=0.40)
        warned = ps.calculate(10000, 0.50, 100, vpin=0.60)
        assert warned < normal

    def test_mm_cautious_reduces(self):
        ps = PositionSizer()
        normal = ps.calculate(10000, 0.50, 100, spread_status="MM_ACTIVE")
        cautious = ps.calculate(10000, 0.50, 100, spread_status="MM_CAUTIOUS")
        assert cautious < normal

    def test_max_position_cap(self):
        ps = PositionSizer()
        size = ps.calculate(10000, 0.90, 0.01)
        max_allowed = 10000 * 0.10 / 0.01
        assert size <= max_allowed


class TestCorrelationTracker:
    def test_insufficient_data_returns_none(self):
        ct = CorrelationTracker()
        assert ct.correlation is None

    def test_perfect_positive_correlation(self):
        ct = CorrelationTracker()
        for i in range(50):
            ct.add_returns(float(i), float(i))
        corr = ct.correlation
        assert corr is not None
        assert corr > 0.99

    def test_perfect_negative_correlation(self):
        ct = CorrelationTracker()
        for i in range(50):
            ct.add_returns(float(i), float(-i))
        corr = ct.correlation
        assert corr is not None
        assert corr < -0.99


class TestFundingRateTracker:
    def test_no_data_returns_none(self):
        ft = FundingRateTracker()
        assert ft.annualized_rate("BTCUSDT") is None

    def test_annualized_calculation(self):
        ft = FundingRateTracker()
        for i in range(24):
            ft.record_rate("BTCUSDT", 0.0001, ts=float(i))
        rate = ft.annualized_rate("BTCUSDT")
        assert rate is not None
        assert rate > 0

    def test_expensive_detection(self):
        ft = FundingRateTracker()
        for i in range(24):
            ft.record_rate("BTCUSDT", 0.01, ts=float(i))
        assert ft.is_expensive("BTCUSDT")


class TestPortfolioVaR:
    def test_empty_positions(self):
        var = PortfolioVaR()
        assert var.estimate([], {}) == 0.0

    def test_single_position(self):
        var = PortfolioVaR()
        positions = [{"symbol": "BTCUSDT", "size": 0.1, "entry_price": 50000, "direction": 1}]
        atr = {"BTCUSDT": 2000.0}
        result = var.estimate(positions, atr)
        assert result > 0


class TestRiskManager:
    def test_fresh_state_allows_trading(self):
        rm = RiskManager()
        rm.set_balance(10000)
        can, _ = rm.can_open_position("BTCUSDT")
        assert can

    def test_daily_limit_blocks(self):
        rm = RiskManager(RiskSettings(max_daily_trades=2))
        rm.set_balance(10000)
        rm.state.trade_count_today = 2
        can, reason = rm.can_open_position("BTCUSDT")
        assert not can
        assert "limit" in reason.lower()

    def test_max_positions_blocks(self):
        rm = RiskManager(RiskSettings(max_open_positions=2))
        rm.set_balance(10000)
        rm.state.open_positions = 2
        can, _ = rm.can_open_position("BTCUSDT")
        assert not can

    def test_cooldown_blocks(self):
        rm = RiskManager(RiskSettings(cooldown_minutes=15))
        rm.set_balance(10000)
        rm.state.last_trade_ts = time.time()
        can, _ = rm.can_open_position("BTCUSDT")
        assert not can

    def test_trade_lifecycle(self):
        rm = RiskManager()
        rm.set_balance(10000)
        rm.record_trade_open()
        assert rm.state.open_positions == 1
        assert rm.state.trade_count_today == 1
        rm.record_trade_close(50)
        assert rm.state.open_positions == 0
        assert rm.state.daily_pnl == 50

    def test_position_size_respects_drawdown(self):
        rm = RiskManager()
        rm.set_balance(10000)
        size_normal = rm.calculate_position_size(0.50, 100)
        rm.state.daily_pnl = -200
        size_drawdown = rm.calculate_position_size(0.50, 100)
        assert size_drawdown <= size_normal

    def test_reset_daily(self):
        rm = RiskManager()
        rm.state.daily_pnl = -500
        rm.state.trade_count_today = 5
        rm.state.is_halted = True
        rm.reset_daily()
        assert rm.state.daily_pnl == 0
        assert rm.state.trade_count_today == 0
        assert not rm.state.is_halted

    def test_correlation_cap(self):
        rm = RiskManager(RiskSettings(correlation_cap_threshold=0.85))
        rm.set_balance(10000)
        for i in range(50):
            rm.correlation.add_returns(float(i), float(i))
        existing = [{"symbol": "BTCUSDT", "direction": 1, "size": 0.05, "entry_price": 50000}]
        cap = rm.check_correlation_cap("ETHUSDT", 1, existing)
        assert cap <= 1.0
