from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class FillResult:
    """Result of applying fee and slippage to an order."""

    fill_price: float
    fee_usd: float
    slippage_usd: float
    total_cost: float  # fee + slippage


class FeeModel:
    """
    Simulates realistic trading costs.

    PRD specification:
      - Taker fee: 0.1% per trade
      - Slippage: ±0.05% on entry
    """

    def __init__(
        self,
        taker_fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
        randomize_slippage: bool = True,
    ):
        self._taker_fee = taker_fee_pct
        self._slippage = slippage_pct
        self._randomize = randomize_slippage

    def apply_entry(self, price: float, size: float, direction: int) -> FillResult:
        """
        Apply slippage + fee to an entry order.
        direction: +1 for LONG (buy), -1 for SHORT (sell)
        Buys fill higher, sells fill lower (adverse slippage).
        """
        slip_pct = self._get_slippage()
        slip_price = price * slip_pct * direction
        fill_price = price + slip_price

        notional = abs(size * fill_price)
        fee = notional * self._taker_fee
        slippage_cost = abs(slip_price * size)

        return FillResult(
            fill_price=round(fill_price, 2),
            fee_usd=round(fee, 4),
            slippage_usd=round(slippage_cost, 4),
            total_cost=round(fee + slippage_cost, 4),
        )

    def apply_exit(self, price: float, size: float, direction: int) -> FillResult:
        """
        Apply slippage + fee to an exit order.
        Exits face adverse slippage opposite to entry direction.
        """
        slip_pct = self._get_slippage()
        slip_price = price * slip_pct * (-direction)
        fill_price = price + slip_price

        notional = abs(size * fill_price)
        fee = notional * self._taker_fee
        slippage_cost = abs(slip_price * size)

        return FillResult(
            fill_price=round(fill_price, 2),
            fee_usd=round(fee, 4),
            slippage_usd=round(slippage_cost, 4),
            total_cost=round(fee + slippage_cost, 4),
        )

    def calculate_fee(self, notional: float) -> float:
        return notional * self._taker_fee

    def _get_slippage(self) -> float:
        if self._randomize:
            return random.uniform(0, self._slippage)
        return self._slippage
