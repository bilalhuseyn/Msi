from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backtest.fee_model import FeeModel

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Tracks a single open position with partial-exit TP management."""

    entry_price: float
    size: float
    direction: int  # +1 LONG, -1 SHORT
    stop_price: float
    tp1_price: float
    tp2_price: float
    entry_ts: float
    entry_fee: float = 0.0
    remaining_size: float = 0.0
    be_triggered: bool = False
    tp1_hit: bool = False
    tp2_hit: bool = False
    exit_fees: float = 0.0
    symbol: str = ""

    def __post_init__(self):
        if self.remaining_size == 0:
            self.remaining_size = self.size

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.stop_price) * self.size

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.direction * self.remaining_size

    def pnl_r(self, current_price: float) -> float:
        risk = self.initial_risk
        if risk <= 0:
            return 0.0
        return self.unrealized_pnl(current_price) / risk


@dataclass
class TradeRecord:
    """Completed trade record for analytics."""

    entry_price: float
    exit_price: float
    direction: int
    size: float
    entry_ts: float
    exit_ts: float
    pnl_usd: float
    pnl_r: float
    fees_total: float
    exit_reason: str
    symbol: str = ""

    @property
    def is_winner(self) -> bool:
        return self.pnl_usd > 0

    @property
    def hold_time_seconds(self) -> float:
        return self.exit_ts - self.entry_ts


class Portfolio:
    """
    Virtual portfolio for backtesting.

    Tracks balance, open positions, closed trades, and equity curve.
    Enforces PRD limits: max 2 concurrent positions, cooldown, daily caps.
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        fee_model: FeeModel | None = None,
        max_positions: int = 2,
        cooldown_seconds: float = 900.0,
        max_daily_trades: int = 8,
        max_hold_seconds: float = 14_400.0,
        tp1_exit_pct: float = 0.50,
        tp2_exit_pct: float = 0.30,
    ):
        self._initial_balance = initial_balance
        self._balance = initial_balance
        self._fee_model = fee_model or FeeModel()
        self._max_positions = max_positions
        self._cooldown = cooldown_seconds
        self._max_daily_trades = max_daily_trades
        self._max_hold = max_hold_seconds
        self._tp1_pct = tp1_exit_pct
        self._tp2_pct = tp2_exit_pct

        self._positions: list[Position] = []
        self._trades: list[TradeRecord] = []
        self._equity_curve: list[tuple[float, float]] = []
        self._last_trade_ts: float = 0.0
        self._daily_trades: dict[int, int] = {}

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def positions(self) -> list[Position]:
        return list(self._positions)

    @property
    def trades(self) -> list[TradeRecord]:
        return list(self._trades)

    @property
    def equity_curve(self) -> list[tuple[float, float]]:
        return list(self._equity_curve)

    @property
    def open_position_count(self) -> int:
        return len(self._positions)

    def can_open(self, timestamp: float) -> tuple[bool, str]:
        if len(self._positions) >= self._max_positions:
            return False, "max_positions_reached"
        if timestamp - self._last_trade_ts < self._cooldown:
            return False, "cooldown_active"
        day_key = int(timestamp // 86400)
        if self._daily_trades.get(day_key, 0) >= self._max_daily_trades:
            return False, "daily_trade_limit"
        return True, ""

    def open_position(
        self,
        price: float,
        size: float,
        direction: int,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        timestamp: float,
        symbol: str = "BTCUSDT",
    ) -> Position | None:
        can, reason = self.can_open(timestamp)
        if not can:
            return None

        fill = self._fee_model.apply_entry(price, size, direction)
        self._balance -= fill.fee_usd

        pos = Position(
            entry_price=fill.fill_price,
            size=size,
            direction=direction,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            entry_ts=timestamp,
            entry_fee=fill.fee_usd,
            symbol=symbol,
        )
        self._positions.append(pos)
        self._last_trade_ts = timestamp
        day_key = int(timestamp // 86400)
        self._daily_trades[day_key] = self._daily_trades.get(day_key, 0) + 1

        return pos

    def update(self, current_price: float, timestamp: float, atr: float = 0.0) -> list[TradeRecord]:
        """
        Check all open positions against current price.
        Handles stop loss, TP1/TP2/TP3, time exits, and break-even moves.
        Returns list of trades closed this tick.
        """
        closed: list[TradeRecord] = []
        remaining: list[Position] = []

        for pos in self._positions:
            partial_records = self._check_position(pos, current_price, timestamp, atr)
            closed.extend(partial_records)
            if pos.remaining_size > 0.001:
                remaining.append(pos)

        self._positions = remaining
        self._record_equity(timestamp, current_price)
        return closed

    def force_close_all(self, current_price: float, timestamp: float, reason: str = "veto") -> list[TradeRecord]:
        closed: list[TradeRecord] = []
        for pos in self._positions:
            record = self._close_position(pos, current_price, timestamp, pos.remaining_size, reason)
            closed.append(record)
        self._positions.clear()
        return closed

    def _check_position(
        self, pos: Position, price: float, ts: float, atr: float,
    ) -> list[TradeRecord]:
        records: list[TradeRecord] = []
        d = pos.direction

        # Stop loss
        if (d == 1 and price <= pos.stop_price) or (d == -1 and price >= pos.stop_price):
            rec = self._close_position(pos, pos.stop_price, ts, pos.remaining_size, "stop_loss")
            records.append(rec)
            return records

        # Time-based exit
        if ts - pos.entry_ts >= self._max_hold:
            rec = self._close_position(pos, price, ts, pos.remaining_size, "time_exit")
            records.append(rec)
            return records

        pnl_r = pos.pnl_r(price)

        # TP1: 50% exit
        if not pos.tp1_hit:
            tp1_hit = (d == 1 and price >= pos.tp1_price) or (d == -1 and price <= pos.tp1_price)
            if tp1_hit:
                exit_size = pos.size * self._tp1_pct
                exit_size = min(exit_size, pos.remaining_size)
                rec = self._close_position(pos, pos.tp1_price, ts, exit_size, "tp1")
                records.append(rec)
                pos.tp1_hit = True
                pos.stop_price = pos.entry_price
                pos.be_triggered = True

        # TP2: 30% exit
        if pos.tp1_hit and not pos.tp2_hit:
            tp2_hit = (d == 1 and price >= pos.tp2_price) or (d == -1 and price <= pos.tp2_price)
            if tp2_hit:
                exit_size = pos.size * self._tp2_pct
                exit_size = min(exit_size, pos.remaining_size)
                rec = self._close_position(pos, pos.tp2_price, ts, exit_size, "tp2")
                records.append(rec)
                pos.tp2_hit = True
                pos.stop_price = pos.entry_price + d * abs(pos.entry_price - pos.stop_price) * 0.5

        # Break-even move at 1R
        if not pos.be_triggered and pnl_r >= 1.0:
            pos.stop_price = pos.entry_price
            pos.be_triggered = True

        # Trailing stop for remaining (TP3 territory)
        if pos.tp2_hit and atr > 0:
            trailing = price - d * atr * 1.5
            if d == 1:
                pos.stop_price = max(pos.stop_price, trailing)
            else:
                pos.stop_price = min(pos.stop_price, trailing)

        return records

    def _close_position(
        self, pos: Position, price: float, ts: float, size: float, reason: str,
    ) -> TradeRecord:
        fill = self._fee_model.apply_exit(price, size, pos.direction)
        pnl = (fill.fill_price - pos.entry_price) * pos.direction * size
        entry_fee_portion = pos.entry_fee * (size / pos.size)
        total_fees = entry_fee_portion + fill.fee_usd
        net_pnl = pnl - total_fees

        self._balance += net_pnl
        pos.remaining_size -= size
        pos.exit_fees += fill.fee_usd

        risk_per_unit = abs(pos.entry_price - pos.stop_price) if pos.stop_price != pos.entry_price else pos.entry_price * 0.005
        pnl_r_val = net_pnl / max(risk_per_unit * size, 1e-9)

        record = TradeRecord(
            entry_price=pos.entry_price,
            exit_price=fill.fill_price,
            direction=pos.direction,
            size=size,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            pnl_usd=round(net_pnl, 4),
            pnl_r=round(pnl_r_val, 4),
            fees_total=round(total_fees, 4),
            exit_reason=reason,
            symbol=pos.symbol,
        )
        self._trades.append(record)
        return record

    def _record_equity(self, ts: float, price: float) -> None:
        unrealized = sum(p.unrealized_pnl(price) for p in self._positions)
        equity = self._balance + unrealized
        self._equity_curve.append((ts, equity))

    def daily_pnl(self, timestamp: float) -> float:
        day_start = (int(timestamp) // 86400) * 86400
        day_pnl = sum(
            t.pnl_usd for t in self._trades
            if t.exit_ts >= day_start
        )
        return day_pnl

    def reset(self) -> None:
        self._balance = self._initial_balance
        self._positions.clear()
        self._trades.clear()
        self._equity_curve.clear()
        self._last_trade_ts = 0.0
        self._daily_trades.clear()
