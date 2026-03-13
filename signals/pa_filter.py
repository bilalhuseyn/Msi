"""
Price Action Filter — Multi-timeframe confirmation and S/R detection.

Provides:
  1. Candle pattern recognition (engulfing, pin bar, inside bar)
  2. Multi-TF trend alignment (1m, 5m, 15m)
  3. Support / Resistance level detection (replaces TP2 2.5R placeholder)
  4. Volume confirmation (breakout volume > 1.5x average)

Used as a final confirmation gate AFTER the CE produces LONG/SHORT.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CandlePattern(str, Enum):
    NONE = "NONE"
    BULLISH_ENGULFING = "BULLISH_ENGULFING"
    BEARISH_ENGULFING = "BEARISH_ENGULFING"
    BULLISH_PIN_BAR = "BULLISH_PIN_BAR"
    BEARISH_PIN_BAR = "BEARISH_PIN_BAR"
    INSIDE_BAR = "INSIDE_BAR"
    DOJI = "DOJI"


class TrendDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    SIDEWAYS = "SIDEWAYS"


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


@dataclass
class SRLevel:
    """Support or Resistance level."""
    price: float
    strength: int = 1
    level_type: str = "support"
    last_touch_ts: float = 0.0

    @property
    def is_support(self) -> bool:
        return self.level_type == "support"


@dataclass
class PAFilterResult:
    confirmed: bool
    pattern: CandlePattern = CandlePattern.NONE
    trend_1m: TrendDirection = TrendDirection.SIDEWAYS
    trend_5m: TrendDirection = TrendDirection.SIDEWAYS
    trend_15m: TrendDirection = TrendDirection.SIDEWAYS
    trend_aligned: bool = False
    volume_confirmed: bool = False
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0
    confidence: float = 0.0
    reason: str = ""


class PriceActionFilter:
    """
    Final confirmation gate using multi-timeframe price action analysis.

    Call flow:
      1. CE outputs LONG/SHORT
      2. PA Filter checks:
         a) Is there a confirming candle pattern?
         b) Are higher timeframes aligned?
         c) Is volume above average?
         d) Where are nearest S/R levels?
      3. Returns confirmed=True/False with confidence score
    """

    def __init__(
        self,
        ema_fast: int = 8,
        ema_slow: int = 21,
        volume_multiplier: float = 1.5,
        sr_lookback: int = 100,
        sr_proximity_pct: float = 0.003,
        min_confidence: float = 0.4,
    ):
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow
        self._vol_mult = volume_multiplier
        self._sr_lookback = sr_lookback
        self._sr_prox = sr_proximity_pct
        self._min_conf = min_confidence

        self._candles_1m: deque[Candle] = deque(maxlen=200)
        self._candles_5m: deque[Candle] = deque(maxlen=200)
        self._candles_15m: deque[Candle] = deque(maxlen=200)

        self._sr_levels: list[SRLevel] = []

    def add_candle(self, candle: Candle, timeframe: str = "1m") -> None:
        buf = self._get_buffer(timeframe)
        buf.append(candle)
        if timeframe == "1m" and len(self._candles_1m) >= self._sr_lookback:
            self._update_sr_levels()

    def evaluate(self, direction: int, current_price: float) -> PAFilterResult:
        """
        Evaluate whether a CE signal should be confirmed.
        direction: +1 for LONG, -1 for SHORT
        """
        if len(self._candles_1m) < 3:
            return PAFilterResult(confirmed=False, reason="insufficient_data")

        pattern = self._detect_pattern(self._candles_1m, direction)
        trend_1m = self._calc_trend(self._candles_1m)
        trend_5m = self._calc_trend(self._candles_5m) if len(self._candles_5m) >= 3 else TrendDirection.SIDEWAYS
        trend_15m = self._calc_trend(self._candles_15m) if len(self._candles_15m) >= 3 else TrendDirection.SIDEWAYS

        expected_trend = TrendDirection.UP if direction == 1 else TrendDirection.DOWN
        aligned = trend_1m == expected_trend
        if len(self._candles_5m) >= 3:
            aligned = aligned and trend_5m in (expected_trend, TrendDirection.SIDEWAYS)
        if len(self._candles_15m) >= 3:
            aligned = aligned and trend_15m in (expected_trend, TrendDirection.SIDEWAYS)

        vol_confirmed = self._check_volume(self._candles_1m)

        support, resistance = self._find_nearest_sr(current_price)

        sr_ok = True
        if direction == 1 and resistance > 0:
            dist_to_res = (resistance - current_price) / current_price
            if dist_to_res < self._sr_prox:
                sr_ok = False
        elif direction == -1 and support > 0:
            dist_to_sup = (current_price - support) / current_price
            if dist_to_sup < self._sr_prox:
                sr_ok = False

        score = 0.0
        reasons = []
        if pattern != CandlePattern.NONE:
            score += 0.30
            reasons.append(f"pattern:{pattern.value}")
        if aligned:
            score += 0.35
            reasons.append("trend_aligned")
        if vol_confirmed:
            score += 0.20
            reasons.append("volume_confirmed")
        if sr_ok:
            score += 0.15
            reasons.append("sr_clear")
        else:
            reasons.append("sr_blocked")

        confirmed = score >= self._min_conf and sr_ok

        return PAFilterResult(
            confirmed=confirmed,
            pattern=pattern,
            trend_1m=trend_1m,
            trend_5m=trend_5m,
            trend_15m=trend_15m,
            trend_aligned=aligned,
            volume_confirmed=vol_confirmed,
            nearest_support=support,
            nearest_resistance=resistance,
            confidence=round(score, 4),
            reason=", ".join(reasons),
        )

    def find_tp2_level(self, entry_price: float, direction: int) -> float | None:
        """
        Find the next S/R level for TP2 placement.
        Replaces the hardcoded 2.5R placeholder from Phase 4.
        """
        if not self._sr_levels:
            return None

        if direction == 1:
            resistances = sorted(
                [s for s in self._sr_levels if s.price > entry_price * 1.001],
                key=lambda s: s.price,
            )
            return resistances[0].price if resistances else None
        else:
            supports = sorted(
                [s for s in self._sr_levels if s.price < entry_price * 0.999],
                key=lambda s: s.price,
                reverse=True,
            )
            return supports[0].price if supports else None

    @property
    def sr_levels(self) -> list[SRLevel]:
        return list(self._sr_levels)

    def reset(self) -> None:
        self._candles_1m.clear()
        self._candles_5m.clear()
        self._candles_15m.clear()
        self._sr_levels.clear()

    def _get_buffer(self, tf: str) -> deque[Candle]:
        if tf == "5m":
            return self._candles_5m
        if tf == "15m":
            return self._candles_15m
        return self._candles_1m

    def _detect_pattern(self, candles: deque[Candle], direction: int) -> CandlePattern:
        if len(candles) < 2:
            return CandlePattern.NONE

        curr = candles[-1]
        prev = candles[-2]

        if curr.range == 0:
            return CandlePattern.NONE

        body_ratio = curr.body / curr.range

        if direction == 1:
            if (not prev.is_bullish and curr.is_bullish
                    and curr.close > prev.open and curr.open < prev.close
                    and curr.body > prev.body * 0.8):
                return CandlePattern.BULLISH_ENGULFING

            if (curr.lower_wick > curr.range * 0.6
                    and curr.upper_wick < curr.range * 0.2):
                return CandlePattern.BULLISH_PIN_BAR

        elif direction == -1:
            if (prev.is_bullish and not curr.is_bullish
                    and curr.close < prev.open and curr.open > prev.close
                    and curr.body > prev.body * 0.8):
                return CandlePattern.BEARISH_ENGULFING

            if (curr.upper_wick > curr.range * 0.6
                    and curr.lower_wick < curr.range * 0.2):
                return CandlePattern.BEARISH_PIN_BAR

        if len(candles) >= 2:
            if curr.high < prev.high and curr.low > prev.low:
                return CandlePattern.INSIDE_BAR

        if body_ratio < 0.1:
            return CandlePattern.DOJI

        return CandlePattern.NONE

    def _calc_trend(self, candles: deque[Candle]) -> TrendDirection:
        if len(candles) < max(self._ema_slow, 3):
            closes = [c.close for c in candles]
            if len(closes) >= 3:
                if closes[-1] > closes[-3] and closes[-2] > closes[-3]:
                    return TrendDirection.UP
                if closes[-1] < closes[-3] and closes[-2] < closes[-3]:
                    return TrendDirection.DOWN
            return TrendDirection.SIDEWAYS

        closes = [c.close for c in candles]
        ema_f = self._ema(closes, self._ema_fast)
        ema_s = self._ema(closes, self._ema_slow)

        if ema_f > ema_s * 1.0001:
            return TrendDirection.UP
        if ema_f < ema_s * 0.9999:
            return TrendDirection.DOWN
        return TrendDirection.SIDEWAYS

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        k = 2.0 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _check_volume(self, candles: deque[Candle]) -> bool:
        if len(candles) < 20:
            return False
        vols = [c.volume for c in list(candles)[-20:]]
        avg = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else 0
        if avg <= 0:
            return False
        return candles[-1].volume >= avg * self._vol_mult

    def _update_sr_levels(self) -> None:
        candles = list(self._candles_1m)
        if len(candles) < 20:
            return

        levels: list[SRLevel] = []
        lookback = min(len(candles), self._sr_lookback)
        recent = candles[-lookback:]

        for i in range(2, len(recent) - 2):
            c = recent[i]
            if c.high > recent[i - 1].high and c.high > recent[i - 2].high \
               and c.high > recent[i + 1].high and c.high > recent[i + 2].high:
                levels.append(SRLevel(
                    price=c.high, level_type="resistance",
                    last_touch_ts=c.timestamp,
                ))
            if c.low < recent[i - 1].low and c.low < recent[i - 2].low \
               and c.low < recent[i + 1].low and c.low < recent[i + 2].low:
                levels.append(SRLevel(
                    price=c.low, level_type="support",
                    last_touch_ts=c.timestamp,
                ))

        merged = self._merge_close_levels(levels, recent[-1].close)
        self._sr_levels = merged

    def _merge_close_levels(
        self, levels: list[SRLevel], current_price: float,
    ) -> list[SRLevel]:
        if not levels:
            return []

        levels.sort(key=lambda l: l.price)
        merged: list[SRLevel] = []
        threshold = current_price * 0.002

        for lvl in levels:
            found = False
            for m in merged:
                if abs(m.price - lvl.price) < threshold:
                    m.strength += 1
                    m.price = (m.price + lvl.price) / 2
                    m.last_touch_ts = max(m.last_touch_ts, lvl.last_touch_ts)
                    found = True
                    break
            if not found:
                merged.append(lvl)

        merged.sort(key=lambda l: l.strength, reverse=True)
        return merged[:20]

    def _find_nearest_sr(self, price: float) -> tuple[float, float]:
        support = 0.0
        resistance = 0.0

        for lvl in self._sr_levels:
            if lvl.price < price:
                if support == 0 or lvl.price > support:
                    support = lvl.price
            elif lvl.price > price:
                if resistance == 0 or lvl.price < resistance:
                    resistance = lvl.price

        return support, resistance
