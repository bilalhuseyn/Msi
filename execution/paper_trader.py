"""
Paper Trading Mode — Simulated live execution using real market data feeds.

Connects to the live OFI Engine signal pipeline and executes virtual trades
without placing real orders. Tracks performance metrics in real-time.

PRD requirement: minimum 2 weeks of paper trading before going live.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from config.constants import DecisionAction
from execution.position_manager import PositionState
from config.settings import Settings
from execution.hedger import DynamicHedger
from execution.position_manager import PositionManager, ExitOrder, LivePosition
from signals.pa_filter import PriceActionFilter, Candle

logger = logging.getLogger(__name__)


@dataclass
class PaperTradeRecord:
    symbol: str
    direction: int
    entry_price: float
    exit_price: float
    size: float
    entry_ts: float
    exit_ts: float
    pnl_usd: float
    fees: float
    exit_reason: str


@dataclass
class PaperTradingStats:
    """Running statistics for paper trading session."""

    start_ts: float = 0.0
    total_trades: int = 0
    winners: int = 0
    losers: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_fees: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_balance: float = 0.0
    hedges_triggered: int = 0
    pa_filter_blocks: int = 0
    signals_received: int = 0
    vetoes: int = 0

    @property
    def net_pnl(self) -> float:
        return self.gross_profit - self.gross_loss - self.total_fees

    @property
    def win_rate(self) -> float:
        return self.winners / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def uptime_hours(self) -> float:
        return (time.time() - self.start_ts) / 3600 if self.start_ts > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float("inf")


class PaperTrader:
    """
    Paper trading engine that processes CE decisions and simulates execution.

    Integrates:
      - PositionManager for position lifecycle
      - PriceActionFilter for signal confirmation
      - DynamicHedger for hedge evaluation
    """

    def __init__(
        self,
        initial_balance: float = 10_000.0,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
        log_dir: str = "paper_trades",
    ):
        self._balance = initial_balance
        self._initial_balance = initial_balance
        self._fee_pct = fee_pct
        self._slippage_pct = slippage_pct
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        self._pm = PositionManager()
        self._pa = PriceActionFilter()
        self._hedger = DynamicHedger()
        self._stats = PaperTradingStats(start_ts=time.time())
        self._trades: list[PaperTradeRecord] = []
        self._running = False

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def stats(self) -> PaperTradingStats:
        return self._stats

    @property
    def positions(self) -> list[LivePosition]:
        return self._pm.open_positions

    def on_decision(
        self,
        symbol: str,
        action: DecisionAction,
        score: float,
        price: float,
        atr: float,
        vpin_value: float | None = None,
        gex_flip: bool = False,
    ) -> list[ExitOrder]:
        """
        Process a CE decision. Called every time the CE produces a result.

        Returns list of exit orders generated (for logging/display).
        """
        exits: list[ExitOrder] = []

        if action == DecisionAction.VETO:
            # P1 fix: VETO blocks new entries only — open positions managed by SL/TP
            self._stats.vetoes += 1
            return exits

        exits.extend(self._pm.update(symbol, price, atr))
        for order in exits:
            self._execute_exit(order, price)

        self._check_hedges(symbol, price, vpin_value, gex_flip)

        if action in (DecisionAction.LONG, DecisionAction.SHORT):
            self._stats.signals_received += 1
            direction = 1 if action == DecisionAction.LONG else -1

            pa_result = self._pa.evaluate(direction, price)
            if not pa_result.confirmed:
                self._stats.pa_filter_blocks += 1
                logger.info(
                    "PA Filter blocked %s %s — confidence=%.2f reason=%s",
                    action.value, symbol, pa_result.confidence, pa_result.reason,
                )
                return exits

            self._open_trade(symbol, direction, price, atr, score)

        self._update_drawdown()
        return exits

    def on_candle(self, candle: Candle, timeframe: str = "1m") -> None:
        """Feed kline data to the PA filter."""
        self._pa.add_candle(candle, timeframe)

    def _open_trade(
        self, symbol: str, direction: int, price: float, atr: float, score: float,
    ) -> None:
        slip = price * self._slippage_pct * direction
        fill_price = price + slip

        stop_dist = max(atr * 1.5, price * 0.005)
        stop_price = fill_price - direction * stop_dist
        tp1_price = fill_price + direction * stop_dist * 1.5

        tp2_level = self._pa.find_tp2_level(fill_price, direction)
        if tp2_level is not None:
            tp2_price = tp2_level
        else:
            tp2_price = fill_price + direction * stop_dist * 2.5

        risk_usd = self._balance * 0.01
        size = risk_usd / stop_dist if stop_dist > 0 else 0
        max_size = self._balance * 0.10 / fill_price if fill_price > 0 else 0
        size = min(size, max_size)

        if size <= 0:
            return

        fee = fill_price * size * self._fee_pct
        self._balance -= fee

        pos = self._pm.open_position(
            symbol=symbol, direction=direction,
            entry_price=fill_price, size=size,
            stop_price=stop_price, tp1_price=tp1_price,
            tp2_price=tp2_price, atr=atr, fee=fee,
        )
        if pos:
            logger.info(
                "PAPER TRADE: %s %s @ %.2f | size=%.6f | stop=%.2f | tp1=%.2f | tp2=%.2f | score=%.3f",
                "LONG" if direction == 1 else "SHORT", symbol,
                fill_price, size, stop_price, tp1_price, tp2_price, score,
            )

    def _execute_exit(self, order: ExitOrder, market_price: float) -> None:
        d = order.direction
        slip = market_price * self._slippage_pct * (-d)
        fill_price = order.target_price + slip

        notional = fill_price * order.size
        fee = notional * self._fee_pct
        pnl = (fill_price - self._get_entry_price(order.position_id)) * d * order.size
        net_pnl = pnl - fee

        self._balance += net_pnl
        self._stats.total_fees += fee
        self._stats.total_trades += 1

        if net_pnl > 0:
            self._stats.winners += 1
            self._stats.gross_profit += net_pnl
        else:
            self._stats.losers += 1
            self._stats.gross_loss += abs(net_pnl)

        record = PaperTradeRecord(
            symbol=order.symbol, direction=d,
            entry_price=self._get_entry_price(order.position_id),
            exit_price=fill_price, size=order.size,
            entry_ts=0, exit_ts=time.time(),
            pnl_usd=round(net_pnl, 4), fees=round(fee, 4),
            exit_reason=order.reason,
        )
        self._trades.append(record)

        logger.info(
            "PAPER EXIT: %s %s @ %.2f | reason=%s | pnl=$%.2f",
            "LONG" if d == 1 else "SHORT", order.symbol,
            fill_price, order.reason, net_pnl,
        )

    def _get_entry_price(self, position_id: str) -> float:
        pos = self._pm._positions.get(position_id)
        if pos:
            return pos.entry_price
        for p in self._pm._closed_today:
            if p.position_id == position_id:
                return p.entry_price
        return 0.0

    def _check_hedges(
        self, symbol: str, price: float,
        vpin: float | None, gex_flip: bool,
    ) -> None:
        for pos in self._pm.open_positions:
            if pos.symbol != symbol:
                continue

            notional = price * pos.remaining_size
            hold_secs = pos.hold_time_seconds
            pnl_pct = (price - pos.entry_price) * pos.direction / pos.entry_price

            decision = self._hedger.evaluate(
                position_value_usd=notional,
                vpin_value=vpin,
                hold_time_seconds=hold_secs,
                unrealized_pnl_pct=pnl_pct,
                gex_flip_detected=gex_flip,
            )

            if decision.should_hedge:
                self._stats.hedges_triggered += 1
                hedge_size = pos.remaining_size * decision.hedge_pct
                logger.info(
                    "HEDGE: %s %s — %d%% | triggers: %s",
                    pos.position_id, symbol,
                    int(decision.hedge_pct * 100),
                    ", ".join(decision.triggers),
                )
                if decision.hedge_pct >= 1.0:
                    order = self._pm.force_close(pos.position_id, price, decision.reason)
                    if order:
                        self._execute_exit(order, price)
                else:
                    exit_order = ExitOrder(
                        position_id=pos.position_id,
                        symbol=symbol, direction=pos.direction,
                        size=hedge_size, target_price=price,
                        reason=decision.reason,
                    )
                    self._execute_exit(exit_order, price)
                    pos.remaining_size -= hedge_size
                    if pos.remaining_size < 0.001:
                        pos.state = PositionState.CLOSED

    def _update_drawdown(self) -> None:
        if self._balance > self._stats.peak_balance:
            self._stats.peak_balance = self._balance
        if self._stats.peak_balance > 0:
            dd = (self._stats.peak_balance - self._balance) / self._stats.peak_balance
            if dd > self._stats.max_drawdown_pct:
                self._stats.max_drawdown_pct = dd

    def save_session(self) -> Path:
        """Save paper trading session to JSON."""
        session = {
            "start_ts": self._stats.start_ts,
            "end_ts": time.time(),
            "initial_balance": self._initial_balance,
            "final_balance": self._balance,
            "stats": {
                "total_trades": self._stats.total_trades,
                "winners": self._stats.winners,
                "losers": self._stats.losers,
                "win_rate": round(self._stats.win_rate, 4),
                "net_pnl": round(self._stats.net_pnl, 2),
                "gross_profit": round(self._stats.gross_profit, 2),
                "gross_loss": round(self._stats.gross_loss, 2),
                "total_fees": round(self._stats.total_fees, 2),
                "profit_factor": round(self._stats.profit_factor, 4) if self._stats.profit_factor != float("inf") else 999.0,
                "max_drawdown_pct": round(self._stats.max_drawdown_pct, 6),
                "uptime_hours": round(self._stats.uptime_hours, 2),
                "signals_received": self._stats.signals_received,
                "pa_filter_blocks": self._stats.pa_filter_blocks,
                "vetoes": self._stats.vetoes,
                "hedges_triggered": self._stats.hedges_triggered,
            },
            "trades": [
                {
                    "symbol": t.symbol,
                    "direction": "LONG" if t.direction == 1 else "SHORT",
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "size": t.size,
                    "pnl_usd": t.pnl_usd,
                    "fees": t.fees,
                    "exit_reason": t.exit_reason,
                    "exit_ts": t.exit_ts,
                }
                for t in self._trades
            ],
        }

        filepath = self._log_dir / f"paper_session_{int(time.time())}.json"
        filepath.write_text(json.dumps(session, indent=2), encoding="utf-8")
        logger.info("Paper trading session saved: %s", filepath)
        return filepath

    def print_status(self) -> str:
        s = self._stats
        lines = [
            "=== PAPER TRADING STATUS ===",
            f"Balance:    ${self._balance:,.2f} (start: ${self._initial_balance:,.2f})",
            f"Net PnL:    ${s.net_pnl:+,.2f}",
            f"Trades:     {s.total_trades} (W:{s.winners} L:{s.losers} WR:{s.win_rate:.1%})",
            f"Max DD:     {s.max_drawdown_pct:.2%}",
            f"Signals:    {s.signals_received} received, {s.pa_filter_blocks} PA-blocked, {s.vetoes} vetoed",
            f"Hedges:     {s.hedges_triggered}",
            f"Positions:  {self._pm.position_count} open",
            f"Uptime:     {s.uptime_hours:.1f}h",
        ]
        return "\n".join(lines)
