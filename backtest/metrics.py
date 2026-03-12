from __future__ import annotations

import math
from dataclasses import dataclass, field
from backtest.portfolio import TradeRecord


@dataclass
class PerformanceMetrics:
    """Complete backtest performance summary."""

    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    win_rate: float = 0.0

    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0

    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_rr: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    avg_hold_time_seconds: float = 0.0
    avg_daily_trades: float = 0.0
    total_fees: float = 0.0

    final_balance: float = 0.0
    return_pct: float = 0.0

    veto_count: int = 0
    long_count: int = 0
    short_count: int = 0

    max_trades_in_a_day: int = 0
    days_exceeding_limit: int = 0

    meets_targets: dict = field(default_factory=dict)


def calculate_metrics(
    trades: list[TradeRecord],
    equity_curve: list[tuple[float, float]],
    initial_balance: float = 10_000.0,
    risk_free_rate: float = 0.0,
) -> PerformanceMetrics:
    m = PerformanceMetrics()
    m.total_trades = len(trades)

    if not trades:
        m.final_balance = initial_balance
        m.max_drawdown_pct, m.max_drawdown_usd = _calc_max_drawdown(equity_curve, initial_balance)
        return m

    winning = [t for t in trades if t.pnl_usd > 0]
    losing = [t for t in trades if t.pnl_usd <= 0]
    m.winners = len(winning)
    m.losers = len(losing)
    m.win_rate = m.winners / m.total_trades if m.total_trades > 0 else 0.0

    m.gross_profit = sum(t.pnl_usd for t in winning)
    m.gross_loss = abs(sum(t.pnl_usd for t in losing))
    m.net_pnl = m.gross_profit - m.gross_loss
    m.profit_factor = m.gross_profit / m.gross_loss if m.gross_loss > 0 else float("inf")

    m.avg_win = m.gross_profit / m.winners if m.winners > 0 else 0.0
    m.avg_loss = m.gross_loss / m.losers if m.losers > 0 else 0.0
    m.avg_rr = m.avg_win / m.avg_loss if m.avg_loss > 0 else float("inf")

    m.largest_win = max((t.pnl_usd for t in trades), default=0.0)
    m.largest_loss = min((t.pnl_usd for t in trades), default=0.0)

    m.total_fees = sum(t.fees_total for t in trades)
    m.avg_hold_time_seconds = sum(t.hold_time_seconds for t in trades) / m.total_trades

    m.long_count = sum(1 for t in trades if t.direction == 1)
    m.short_count = sum(1 for t in trades if t.direction == -1)

    m.final_balance = initial_balance + m.net_pnl
    m.return_pct = m.net_pnl / initial_balance if initial_balance > 0 else 0.0

    if len(trades) >= 2:
        span_days = (trades[-1].exit_ts - trades[0].entry_ts) / 86400
        m.avg_daily_trades = m.total_trades / max(span_days, 1)
    else:
        m.avg_daily_trades = float(m.total_trades)

    m.max_drawdown_pct, m.max_drawdown_usd = _calc_max_drawdown(equity_curve, initial_balance)

    daily_trade_counts = _daily_trade_counts(trades)
    if daily_trade_counts:
        m.max_trades_in_a_day = max(daily_trade_counts.values())
        m.days_exceeding_limit = sum(1 for c in daily_trade_counts.values() if c > 8)

    daily_returns = _daily_returns(equity_curve, initial_balance)
    m.sharpe_ratio = _sharpe(daily_returns, risk_free_rate)
    m.sortino_ratio = _sortino(daily_returns, risk_free_rate)

    if len(trades) >= 2:
        span_days = (trades[-1].exit_ts - trades[0].entry_ts) / 86400
        if span_days > 0 and m.max_drawdown_pct > 0:
            total_return = m.net_pnl / initial_balance
            annualized = total_return * (365 / span_days) if span_days > 0 else 0
            m.calmar_ratio = annualized / m.max_drawdown_pct
        else:
            m.calmar_ratio = 0.0

    m.meets_targets = _check_targets(m)
    return m


def _daily_trade_counts(trades: list[TradeRecord]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for t in trades:
        day = int(t.entry_ts // 86400)
        counts[day] = counts.get(day, 0) + 1
    return counts


def _calc_max_drawdown(
    curve: list[tuple[float, float]], initial_balance: float,
) -> tuple[float, float]:
    if not curve:
        return 0.0, 0.0

    peak = initial_balance
    max_dd_usd = 0.0

    for _, equity in curve:
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_usd:
            max_dd_usd = dd

    max_dd_pct = max_dd_usd / initial_balance if initial_balance > 0 else 0.0
    return round(max_dd_pct, 6), round(max_dd_usd, 4)


def _daily_returns(
    curve: list[tuple[float, float]], initial_balance: float,
) -> list[float]:
    if len(curve) < 2:
        return []

    daily: dict[int, float] = {}
    for ts, equity in curve:
        day = int(ts // 86400)
        daily[day] = equity

    sorted_days = sorted(daily.keys())
    if not sorted_days:
        return []

    returns = []
    prev_eq = initial_balance
    for day in sorted_days:
        eq = daily[day]
        if prev_eq > 0:
            returns.append((eq - prev_eq) / prev_eq)
        prev_eq = eq

    return returns


def _sharpe(daily_returns: list[float], risk_free: float = 0.0) -> float:
    if len(daily_returns) < 2:
        return 0.0
    daily_rf = risk_free / 252
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    var = sum((r - mean_excess) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean_excess / std) * math.sqrt(252)


def _sortino(daily_returns: list[float], risk_free: float = 0.0) -> float:
    if len(daily_returns) < 2:
        return 0.0
    daily_rf = risk_free / 252
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return float("inf") if mean_excess > 0 else 0.0
    downside_var = sum(r ** 2 for r in downside) / len(downside)
    downside_dev = math.sqrt(downside_var) if downside_var > 0 else 0.0
    if downside_dev == 0:
        return 0.0
    return (mean_excess / downside_dev) * math.sqrt(252)


def _check_targets(m: PerformanceMetrics) -> dict:
    """Check metrics against PRD targets."""
    return {
        "win_rate_target": m.win_rate > 0.54,
        "win_rate_min": m.win_rate > 0.50,
        "rr_target": m.avg_rr > 1.8,
        "rr_min": m.avg_rr > 1.4,
        "sharpe_target": m.sharpe_ratio > 1.5,
        "sharpe_min": m.sharpe_ratio > 1.0,
        "max_dd_target": m.max_drawdown_pct < 0.08,
        "max_dd_min": m.max_drawdown_pct < 0.15,
        "profit_factor_target": m.profit_factor > 1.6,
        "profit_factor_min": m.profit_factor > 1.3,
        "daily_trades_ok": 2 <= m.avg_daily_trades <= 6,
        "daily_limit_respected": m.days_exceeding_limit == 0,
    }
