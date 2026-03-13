"""
Position Manager — Live position lifecycle management.

Handles:
  - Dynamic stop/TP adjustment in real-time
  - Break-even moves at 1R
  - Partial exits (TP1 50% at 1.5R, TP2 30% at next S/R, TP3 trailing ATR x1.5)
  - Time-based exits (max 4h hold)
  - Integration with PA Filter for S/R-based TP2
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class PositionState(str, Enum):
    OPEN = "OPEN"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"


@dataclass
class LivePosition:
    """Tracks a single live position with full lifecycle management."""

    position_id: str
    symbol: str
    direction: int
    entry_price: float
    size: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    entry_ts: float
    atr_at_entry: float

    remaining_size: float = 0.0
    state: PositionState = PositionState.OPEN
    be_triggered: bool = False
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    realized_pnl: float = 0.0
    total_fees: float = 0.0

    def __post_init__(self):
        if self.remaining_size == 0:
            self.remaining_size = self.size
        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.stop_price) * self.size

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.direction * self.remaining_size

    def pnl_r(self, price: float) -> float:
        risk = abs(self.entry_price - self.stop_price)
        if risk <= 0:
            return 0.0
        return (price - self.entry_price) * self.direction / risk

    @property
    def hold_time_seconds(self) -> float:
        return time.time() - self.entry_ts

    @property
    def is_open(self) -> bool:
        return self.state != PositionState.CLOSED


@dataclass
class ExitOrder:
    """Represents a partial or full exit to be executed."""

    position_id: str
    symbol: str
    direction: int
    size: float
    target_price: float
    reason: str
    order_type: str = "MARKET"


class PositionManager:
    """
    Manages live position lifecycle with dynamic stop/TP management.

    Per-tick update cycle:
      1. Update price extremes
      2. Check stop loss
      3. Check time exit
      4. Check TP1 → partial exit + break-even
      5. Check TP2 → partial exit + tighten stop
      6. Update trailing stop (TP3 territory)
      7. Check break-even at 1R
    """

    def __init__(
        self,
        max_positions: int = 2,
        max_daily_trades: int = 8,
        cooldown_seconds: float = 900.0,
        max_hold_seconds: float = 14_400.0,
        tp1_exit_pct: float = 0.50,
        tp2_exit_pct: float = 0.30,
        trailing_atr_mult: float = 1.5,
    ):
        self._max_positions = max_positions
        self._max_daily = max_daily_trades
        self._cooldown = cooldown_seconds
        self._max_hold = max_hold_seconds
        self._tp1_pct = tp1_exit_pct
        self._tp2_pct = tp2_exit_pct
        self._trail_mult = trailing_atr_mult

        self._positions: dict[str, LivePosition] = {}
        self._closed_today: list[LivePosition] = []
        self._last_trade_ts: float = 0.0
        self._trade_count_today: int = 0
        self._current_day: int = 0
        self._next_id: int = 1

    def can_open(self) -> tuple[bool, str]:
        now = time.time()
        today = int(now // 86400)
        if today != self._current_day:
            self._current_day = today
            self._trade_count_today = 0
            self._closed_today.clear()

        if len(self._positions) >= self._max_positions:
            return False, "max_positions"
        if now - self._last_trade_ts < self._cooldown:
            return False, "cooldown"
        if self._trade_count_today >= self._max_daily:
            return False, "daily_limit"
        return True, ""

    def open_position(
        self,
        symbol: str,
        direction: int,
        entry_price: float,
        size: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        atr: float,
        fee: float = 0.0,
    ) -> LivePosition | None:
        can, reason = self.can_open()
        if not can:
            logger.warning("Cannot open: %s", reason)
            return None

        pid = f"pos_{self._next_id}"
        self._next_id += 1

        pos = LivePosition(
            position_id=pid,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            size=size,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            entry_ts=time.time(),
            atr_at_entry=atr,
            total_fees=fee,
        )
        self._positions[pid] = pos
        self._last_trade_ts = time.time()
        self._trade_count_today += 1

        logger.info(
            "Opened %s %s @ %.2f | stop=%.2f tp1=%.2f tp2=%.2f | size=%.4f",
            "LONG" if direction == 1 else "SHORT", symbol,
            entry_price, stop_price, tp1_price, tp2_price, size,
        )
        return pos

    def update(self, symbol: str, price: float, atr: float = 0.0) -> list[ExitOrder]:
        """
        Per-tick update for all positions of a given symbol.
        Returns list of exit orders to be executed.
        """
        exits: list[ExitOrder] = []
        now = time.time()

        for pid, pos in list(self._positions.items()):
            if pos.symbol != symbol or not pos.is_open:
                continue

            if pos.direction == 1:
                pos.highest_price = max(pos.highest_price, price)
            else:
                pos.lowest_price = min(pos.lowest_price, price)

            exit_order = self._check_exit(pos, price, now, atr)
            if exit_order:
                exits.append(exit_order)

        for order in exits:
            pos = self._positions.get(order.position_id)
            if pos:
                pos.remaining_size -= order.size
                if pos.remaining_size < 0.001:
                    pos.state = PositionState.CLOSED
                    self._closed_today.append(pos)
                    del self._positions[order.position_id]

        return exits

    def update_tp2(self, position_id: str, new_tp2: float) -> None:
        """Update TP2 with S/R level from PA Filter."""
        pos = self._positions.get(position_id)
        if pos and pos.state in (PositionState.OPEN, PositionState.TP1_HIT):
            old = pos.tp2_price
            pos.tp2_price = new_tp2
            logger.info("TP2 updated for %s: %.2f -> %.2f (S/R level)", position_id, old, new_tp2)

    def force_close(self, position_id: str, price: float, reason: str) -> ExitOrder | None:
        pos = self._positions.get(position_id)
        if not pos or not pos.is_open:
            return None
        order = ExitOrder(
            position_id=pos.position_id,
            symbol=pos.symbol,
            direction=pos.direction,
            size=pos.remaining_size,
            target_price=price,
            reason=reason,
        )
        pos.remaining_size = 0
        pos.state = PositionState.CLOSED
        self._closed_today.append(pos)
        del self._positions[position_id]
        return order

    def force_close_all(self, price: float, reason: str) -> list[ExitOrder]:
        exits = []
        for pid in list(self._positions.keys()):
            order = self.force_close(pid, price, reason)
            if order:
                exits.append(order)
        return exits

    @property
    def open_positions(self) -> list[LivePosition]:
        return [p for p in self._positions.values() if p.is_open]

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def daily_trade_count(self) -> int:
        return self._trade_count_today

    def _check_exit(
        self, pos: LivePosition, price: float, now: float, atr: float,
    ) -> ExitOrder | None:
        d = pos.direction

        if (d == 1 and price <= pos.stop_price) or (d == -1 and price >= pos.stop_price):
            return ExitOrder(
                position_id=pos.position_id, symbol=pos.symbol,
                direction=d, size=pos.remaining_size,
                target_price=pos.stop_price, reason="stop_loss",
            )

        if now - pos.entry_ts >= self._max_hold:
            return ExitOrder(
                position_id=pos.position_id, symbol=pos.symbol,
                direction=d, size=pos.remaining_size,
                target_price=price, reason="time_exit",
            )

        pnl_r = pos.pnl_r(price)

        if pos.state == PositionState.OPEN:
            tp1_hit = (d == 1 and price >= pos.tp1_price) or (d == -1 and price <= pos.tp1_price)
            if tp1_hit:
                exit_size = pos.size * self._tp1_pct
                exit_size = min(exit_size, pos.remaining_size)
                pos.state = PositionState.TP1_HIT
                pos.stop_price = pos.entry_price
                pos.be_triggered = True
                logger.info("TP1 hit for %s — partial exit %.4f, stop -> BE", pos.position_id, exit_size)
                return ExitOrder(
                    position_id=pos.position_id, symbol=pos.symbol,
                    direction=d, size=exit_size,
                    target_price=pos.tp1_price, reason="tp1",
                )

        if pos.state == PositionState.TP1_HIT:
            tp2_hit = (d == 1 and price >= pos.tp2_price) or (d == -1 and price <= pos.tp2_price)
            if tp2_hit:
                exit_size = pos.size * self._tp2_pct
                exit_size = min(exit_size, pos.remaining_size)
                pos.state = PositionState.TP2_HIT
                pos.stop_price = pos.entry_price + d * abs(pos.entry_price - pos.stop_price) * 0.5
                logger.info("TP2 hit for %s — partial exit %.4f, stop tightened", pos.position_id, exit_size)
                return ExitOrder(
                    position_id=pos.position_id, symbol=pos.symbol,
                    direction=d, size=exit_size,
                    target_price=pos.tp2_price, reason="tp2",
                )

        if not pos.be_triggered and pnl_r >= 1.0:
            pos.stop_price = pos.entry_price
            pos.be_triggered = True
            logger.info("Break-even triggered for %s at 1R", pos.position_id)

        if pos.state in (PositionState.TP2_HIT, PositionState.TRAILING) and atr > 0:
            pos.state = PositionState.TRAILING
            trailing = price - d * atr * self._trail_mult
            if d == 1:
                pos.stop_price = max(pos.stop_price, trailing)
            else:
                pos.stop_price = min(pos.stop_price, trailing)

        return None
