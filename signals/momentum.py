"""
Momentum Module — RSI + EMA crossover score.

Produces a continuous score in [-1.0, +1.0] from 15-min candle closes:
  - RSI(14): overbought (>70) favors short, oversold (<30) favors long
  - EMA cross: 8-period vs 21-period — direction and separation = momentum strength

Combined: RSI component (40%) + EMA component (60%).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class MomentumSettings:
    rsi_period: int = 14
    ema_fast: int = 8
    ema_slow: int = 21
    rsi_ob: float = 70.0
    rsi_os: float = 30.0


class MomentumModule:

    def __init__(self, settings: MomentumSettings | None = None):
        cfg = settings or MomentumSettings()
        self._rsi_period = cfg.rsi_period
        self._ema_fast_n = cfg.ema_fast
        self._ema_slow_n = cfg.ema_slow
        self._rsi_ob = cfg.rsi_ob
        self._rsi_os = cfg.rsi_os

        self._closes: deque[float] = deque(maxlen=max(cfg.ema_slow, cfg.rsi_period) + 50)
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._rsi_ready = False
        self._rsi_count = 0

        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._ema_fast_k = 2.0 / (self._ema_fast_n + 1)
        self._ema_slow_k = 2.0 / (self._ema_slow_n + 1)

        self._score = 0.0

    def reset(self) -> None:
        self._closes.clear()
        self._avg_gain = 0.0
        self._avg_loss = 0.0
        self._rsi_ready = False
        self._rsi_count = 0
        self._ema_fast = None
        self._ema_slow = None
        self._score = 0.0

    @property
    def score(self) -> float:
        return self._score

    @property
    def is_ready(self) -> bool:
        return self._rsi_ready and self._ema_fast is not None and self._ema_slow is not None

    def add_candle(self, close: float) -> float:
        self._closes.append(close)
        self._update_ema(close)
        self._update_rsi(close)

        rsi_score = self._rsi_score()
        ema_score = self._ema_score()

        self._score = 0.4 * rsi_score + 0.6 * ema_score
        return self._score

    def _update_rsi(self, close: float) -> None:
        if len(self._closes) < 2:
            return

        delta = close - self._closes[-2]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        self._rsi_count += 1

        if not self._rsi_ready:
            if self._rsi_count >= self._rsi_period:
                closes = list(self._closes)
                gains, losses = [], []
                for i in range(1, self._rsi_period + 1):
                    d = closes[-self._rsi_period - 1 + i] - closes[-self._rsi_period - 1 + i - 1]
                    gains.append(max(d, 0.0))
                    losses.append(max(-d, 0.0))
                self._avg_gain = sum(gains) / self._rsi_period
                self._avg_loss = sum(losses) / self._rsi_period
                self._rsi_ready = True
        else:
            n = self._rsi_period
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n

    def _rsi_value(self) -> float | None:
        if not self._rsi_ready:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _rsi_score(self) -> float:
        rsi = self._rsi_value()
        if rsi is None:
            return 0.0

        mid = (self._rsi_ob + self._rsi_os) / 2.0
        half_range = (self._rsi_ob - self._rsi_os) / 2.0

        if rsi >= self._rsi_ob:
            return -min((rsi - self._rsi_ob) / 30.0 + 0.5, 1.0)
        if rsi <= self._rsi_os:
            return min((self._rsi_os - rsi) / 30.0 + 0.5, 1.0)

        normalized = (rsi - mid) / half_range
        return -normalized * 0.3

    def _update_ema(self, close: float) -> None:
        if self._ema_fast is None:
            if len(self._closes) >= self._ema_fast_n:
                self._ema_fast = sum(list(self._closes)[-self._ema_fast_n:]) / self._ema_fast_n
        else:
            self._ema_fast = close * self._ema_fast_k + self._ema_fast * (1 - self._ema_fast_k)

        if self._ema_slow is None:
            if len(self._closes) >= self._ema_slow_n:
                self._ema_slow = sum(list(self._closes)[-self._ema_slow_n:]) / self._ema_slow_n
        else:
            self._ema_slow = close * self._ema_slow_k + self._ema_slow * (1 - self._ema_slow_k)

    def _ema_score(self) -> float:
        if self._ema_fast is None or self._ema_slow is None:
            return 0.0

        mid = (self._ema_fast + self._ema_slow) / 2.0
        if mid == 0:
            return 0.0

        separation_pct = (self._ema_fast - self._ema_slow) / mid * 100.0
        return max(-1.0, min(1.0, separation_pct / 0.5))
