"""
Market Structure Reader — replaces PA Filter.

Detects structural trend shifts using swing points, Break of Structure (BOS),
and Change of Character (CHoCH). Operates on 15-min and 4-hour candles.

This module is the FINAL GATE before a position opens. If the CE says LONG
but structure is bearish, the trade is blocked.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGE = "RANGE"


class StructureSignal(str, Enum):
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"
    NONE = "NONE"


@dataclass
class SwingPoint:
    index: int
    price: float
    timestamp: float
    is_high: bool


@dataclass
class Candle:
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class StructureResult:
    trend: Trend
    last_signal: StructureSignal
    swing_highs: list[SwingPoint]
    swing_lows: list[SwingPoint]
    support: float = 0.0
    resistance: float = 0.0
    confidence: float = 0.0


class MarketStructureReader:
    """
    Detects market structure from OHLCV candles using fractal pivots.

    Swing detection: a swing high is a candle whose high is higher than
    the N candles on each side. Same logic inverted for swing lows.

    Trend: sequence of higher highs + higher lows = bullish,
    lower highs + lower lows = bearish, else range.

    BOS: price closes beyond the most recent swing high (bullish) or
    swing low (bearish) in the direction of the prevailing trend.

    CHoCH: price closes beyond a prior swing in the OPPOSITE direction
    of the prevailing trend — early reversal warning.
    """

    def __init__(self, swing_lookback: int = 3, min_swings: int = 4):
        self._lookback = swing_lookback
        self._min_swings = min_swings
        self._candles: deque[Candle] = deque(maxlen=500)
        self._swing_highs: list[SwingPoint] = []
        self._swing_lows: list[SwingPoint] = []
        self._trend = Trend.RANGE
        self._last_signal = StructureSignal.NONE
        self._last_bos_idx = -1

    def reset(self) -> None:
        self._candles.clear()
        self._swing_highs.clear()
        self._swing_lows.clear()
        self._trend = Trend.RANGE
        self._last_signal = StructureSignal.NONE
        self._last_bos_idx = -1

    def add_candle(self, candle: Candle) -> StructureResult:
        self._candles.append(candle)
        idx = len(self._candles) - 1

        self._detect_swing_points(idx)
        self._classify_trend()
        self._detect_bos_choch(candle)

        support = self._swing_lows[-1].price if self._swing_lows else 0.0
        resistance = self._swing_highs[-1].price if self._swing_highs else 0.0

        n_aligned = self._count_aligned_swings()
        confidence = min(n_aligned / max(self._min_swings, 1), 1.0)

        return StructureResult(
            trend=self._trend,
            last_signal=self._last_signal,
            swing_highs=list(self._swing_highs[-10:]),
            swing_lows=list(self._swing_lows[-10:]),
            support=support,
            resistance=resistance,
            confidence=confidence,
        )

    @property
    def trend(self) -> Trend:
        return self._trend

    @property
    def is_ready(self) -> bool:
        return (len(self._swing_highs) >= self._min_swings
                and len(self._swing_lows) >= self._min_swings)

    def allows_direction(self, direction: int) -> bool:
        """Final gate: does current structure allow this trade direction?"""
        if not self.is_ready:
            return True

        if direction == 1:  # LONG
            return self._trend in (Trend.BULLISH, Trend.RANGE)
        if direction == -1:  # SHORT
            return self._trend in (Trend.BEARISH, Trend.RANGE)
        return True

    def sr_proximity_score(self, price: float) -> float:
        """Score price position relative to nearest S/R levels. [-1.0, +1.0].

        Positive = price near support (favors long).
        Negative = price near resistance (favors short).
        Near zero = price in no-man's land or no S/R data.
        """
        if not self._swing_highs or not self._swing_lows:
            return 0.0

        resistances = [sp.price for sp in self._swing_highs[-10:]]
        supports = [sp.price for sp in self._swing_lows[-10:]]

        nearest_sup = None
        sup_dist = float("inf")
        for s in supports:
            d = price - s
            if d >= 0 and d < sup_dist:
                sup_dist = d
                nearest_sup = s

        nearest_res = None
        res_dist = float("inf")
        for r in resistances:
            d = r - price
            if d >= 0 and d < res_dist:
                res_dist = d
                nearest_res = r

        if nearest_sup is None and nearest_res is None:
            return 0.0

        atr = self._estimate_atr()
        if atr <= 0:
            return 0.0

        score = 0.0

        if nearest_sup is not None:
            norm_dist = sup_dist / atr
            if norm_dist < 1.5:
                score += max(0.0, 1.0 - norm_dist / 1.5)

        if nearest_res is not None:
            norm_dist = res_dist / atr
            if norm_dist < 1.5:
                score -= max(0.0, 1.0 - norm_dist / 1.5)

        return max(-1.0, min(1.0, score))

    def _estimate_atr(self) -> float:
        """Quick ATR estimate from recent candles."""
        if len(self._candles) < 5:
            return 0.0
        recent = list(self._candles)[-14:]
        ranges = [c.high - c.low for c in recent]
        return sum(ranges) / len(ranges) if ranges else 0.0

    def find_next_sr_level(self, price: float, direction: int) -> float | None:
        """Find the nearest S/R level as TP2 target."""
        if direction == 1:
            levels = [sp.price for sp in self._swing_highs if sp.price > price]
            return min(levels) if levels else None
        else:
            levels = [sp.price for sp in self._swing_lows if sp.price < price]
            return max(levels) if levels else None

    def _detect_swing_points(self, current_idx: int) -> None:
        """Detect fractal pivot swing highs and lows."""
        candles = list(self._candles)
        check_idx = current_idx - self._lookback
        if check_idx < self._lookback:
            return

        candidate = candles[check_idx]

        is_swing_high = all(
            candidate.high >= candles[check_idx + offset].high
            for offset in range(-self._lookback, self._lookback + 1)
            if offset != 0 and 0 <= check_idx + offset < len(candles)
        )

        is_swing_low = all(
            candidate.low <= candles[check_idx + offset].low
            for offset in range(-self._lookback, self._lookback + 1)
            if offset != 0 and 0 <= check_idx + offset < len(candles)
        )

        if is_swing_high:
            sp = SwingPoint(
                index=check_idx, price=candidate.high,
                timestamp=candidate.timestamp, is_high=True,
            )
            if not self._swing_highs or sp.price != self._swing_highs[-1].price:
                self._swing_highs.append(sp)

        if is_swing_low:
            sp = SwingPoint(
                index=check_idx, price=candidate.low,
                timestamp=candidate.timestamp, is_high=False,
            )
            if not self._swing_lows or sp.price != self._swing_lows[-1].price:
                self._swing_lows.append(sp)

    def _classify_trend(self) -> None:
        """Classify trend from swing point sequence."""
        if len(self._swing_highs) < 2 or len(self._swing_lows) < 2:
            self._trend = Trend.RANGE
            return

        sh = self._swing_highs
        sl = self._swing_lows

        higher_highs = sh[-1].price > sh[-2].price
        higher_lows = sl[-1].price > sl[-2].price
        lower_highs = sh[-1].price < sh[-2].price
        lower_lows = sl[-1].price < sl[-2].price

        if higher_highs and higher_lows:
            self._trend = Trend.BULLISH
        elif lower_highs and lower_lows:
            self._trend = Trend.BEARISH
        else:
            self._trend = Trend.RANGE

    def _detect_bos_choch(self, candle: Candle) -> None:
        """Detect BOS and CHoCH based on candle close vs swing points."""
        if len(self._swing_highs) < 2 or len(self._swing_lows) < 2:
            return

        last_sh = self._swing_highs[-1]
        last_sl = self._swing_lows[-1]
        prev_sh = self._swing_highs[-2]
        prev_sl = self._swing_lows[-2]

        if self._trend == Trend.BULLISH:
            if candle.close > last_sh.price:
                self._last_signal = StructureSignal.BOS_BULLISH
            elif candle.close < last_sl.price:
                self._last_signal = StructureSignal.CHOCH_BEARISH

        elif self._trend == Trend.BEARISH:
            if candle.close < last_sl.price:
                self._last_signal = StructureSignal.BOS_BEARISH
            elif candle.close > last_sh.price:
                self._last_signal = StructureSignal.CHOCH_BULLISH

        else:
            if candle.close > last_sh.price:
                self._last_signal = StructureSignal.BOS_BULLISH
            elif candle.close < last_sl.price:
                self._last_signal = StructureSignal.BOS_BEARISH
            else:
                self._last_signal = StructureSignal.NONE

    def _count_aligned_swings(self) -> int:
        """Count consecutive swing points aligned with current trend."""
        if self._trend == Trend.BULLISH:
            count = 0
            for i in range(len(self._swing_highs) - 1, 0, -1):
                if self._swing_highs[i].price > self._swing_highs[i - 1].price:
                    count += 1
                else:
                    break
            return count
        elif self._trend == Trend.BEARISH:
            count = 0
            for i in range(len(self._swing_lows) - 1, 0, -1):
                if self._swing_lows[i].price < self._swing_lows[i - 1].price:
                    count += 1
                else:
                    break
            return count
        return 0


def build_candles_from_trades(
    trades_by_sec: dict[int, list[dict]],
    interval_sec: int = 900,
) -> list[Candle]:
    """
    Build OHLCV candles from Tardis trade data indexed by second.

    Args:
        trades_by_sec: dict mapping second -> list of trade dicts
        interval_sec: candle interval in seconds (900 = 15 minutes)
    """
    if not trades_by_sec:
        return []

    all_seconds = sorted(trades_by_sec.keys())
    candle_start = (all_seconds[0] // interval_sec) * interval_sec

    candles: list[Candle] = []
    bucket_trades: list[dict] = []
    current_start = candle_start

    for sec in all_seconds:
        while sec >= current_start + interval_sec:
            if bucket_trades:
                candles.append(_trades_to_candle(current_start, bucket_trades))
                bucket_trades = []
            current_start += interval_sec

        bucket_trades.extend(trades_by_sec[sec])

    if bucket_trades:
        candles.append(_trades_to_candle(current_start, bucket_trades))

    return candles


def _trades_to_candle(timestamp: float, trades: list[dict]) -> Candle:
    """Convert a list of trades within an interval to a single OHLCV candle."""
    prices = [t["price"] for t in trades]
    volume = sum(t["qty"] for t in trades)
    return Candle(
        timestamp=timestamp,
        open=prices[0],
        high=max(prices),
        low=min(prices),
        close=prices[-1],
        volume=volume,
    )
