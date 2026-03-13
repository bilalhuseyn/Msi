"""
Higher-Timeframe Trend Strength Module — 4H ADX scoring.

Aggregates 15-min candles into 4-hour candles, then computes ADX(14) to
measure trend strength and direction.

Score in [-1.0, +1.0]:
  - ADX > 25 + bullish (+DI > -DI) => positive score scaled by ADX
  - ADX > 25 + bearish (-DI > +DI) => negative score scaled by ADX
  - ADX < 20 => near 0 (ranging market, dampens signals)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class HTFTrendSettings:
    aggregation_factor: int = 16   # 16 x 15min = 4 hours
    adx_period: int = 14
    strong_trend: float = 25.0
    weak_trend: float = 20.0


@dataclass
class _4HCandle:
    high: float
    low: float
    close: float


class HTFTrendModule:

    def __init__(self, settings: HTFTrendSettings | None = None):
        cfg = settings or HTFTrendSettings()
        self._agg = cfg.aggregation_factor
        self._period = cfg.adx_period
        self._strong = cfg.strong_trend
        self._weak = cfg.weak_trend

        self._15m_buffer: list[tuple[float, float, float, float]] = []
        self._4h_candles: deque[_4HCandle] = deque(maxlen=self._period + 50)

        self._smoothed_plus_dm = 0.0
        self._smoothed_minus_dm = 0.0
        self._smoothed_tr = 0.0
        self._adx = 0.0
        self._adx_ready = False
        self._adx_init_count = 0
        self._dx_sum = 0.0

        self._score = 0.0

    def reset(self) -> None:
        self._15m_buffer.clear()
        self._4h_candles.clear()
        self._smoothed_plus_dm = 0.0
        self._smoothed_minus_dm = 0.0
        self._smoothed_tr = 0.0
        self._adx = 0.0
        self._adx_ready = False
        self._adx_init_count = 0
        self._dx_sum = 0.0
        self._score = 0.0

    @property
    def score(self) -> float:
        return self._score

    @property
    def is_ready(self) -> bool:
        return self._adx_ready

    def add_candle_15m(self, open_: float, high: float, low: float, close: float) -> float:
        self._15m_buffer.append((open_, high, low, close))

        if len(self._15m_buffer) >= self._agg:
            self._flush_4h()
            self._15m_buffer.clear()

        return self._score

    def _flush_4h(self) -> None:
        highs = [c[1] for c in self._15m_buffer]
        lows = [c[2] for c in self._15m_buffer]
        candle = _4HCandle(
            high=max(highs),
            low=min(lows),
            close=self._15m_buffer[-1][3],
        )
        self._4h_candles.append(candle)

        if len(self._4h_candles) < 2:
            return

        self._update_adx()

    def _update_adx(self) -> None:
        curr = self._4h_candles[-1]
        prev = self._4h_candles[-2]

        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )

        up_move = curr.high - prev.high
        down_move = prev.low - curr.low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        n = self._period

        if not self._adx_ready:
            self._smoothed_plus_dm += plus_dm
            self._smoothed_minus_dm += minus_dm
            self._smoothed_tr += tr
            self._adx_init_count += 1

            if self._adx_init_count == n:
                if self._smoothed_tr > 0:
                    plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
                    minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr
                    di_sum = plus_di + minus_di
                    dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
                    self._dx_sum = dx
                    self._adx = dx
                    self._adx_ready = True
                    self._score = self._compute_score(plus_di, minus_di)
        else:
            self._smoothed_plus_dm = self._smoothed_plus_dm - self._smoothed_plus_dm / n + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - self._smoothed_minus_dm / n + minus_dm
            self._smoothed_tr = self._smoothed_tr - self._smoothed_tr / n + tr

            if self._smoothed_tr > 0:
                plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
                minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr
            else:
                plus_di = minus_di = 0.0

            di_sum = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
            self._adx = (self._adx * (n - 1) + dx) / n
            self._score = self._compute_score(plus_di, minus_di)

    def _compute_score(self, plus_di: float, minus_di: float) -> float:
        adx = self._adx

        if adx < self._weak:
            return 0.0

        if adx < self._strong:
            strength = (adx - self._weak) / (self._strong - self._weak)
        else:
            strength = min(adx / 50.0, 1.0)

        if plus_di > minus_di:
            return strength
        elif minus_di > plus_di:
            return -strength
        return 0.0
