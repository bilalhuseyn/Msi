"""
Volume Profile Module — VPOC / Value Area scoring.

Builds a rolling volume profile from the last N 15-min candles and scores
the current price relative to VPOC, VAH, and VAL.

Score in [-1.0, +1.0]:
  - Price below VAL => positive (mean-reversion long opportunity)
  - Price above VAH => negative (mean-reversion short opportunity)
  - Price near VPOC => 0 (fair value, no edge)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class VolumeProfileSettings:
    profile_window: int = 96     # 96 x 15min = 24 hours
    value_area_pct: float = 0.70
    num_bins: int = 50


class VolumeProfileModule:

    def __init__(self, settings: VolumeProfileSettings | None = None):
        cfg = settings or VolumeProfileSettings()
        self._window = cfg.profile_window
        self._va_pct = cfg.value_area_pct
        self._num_bins = cfg.num_bins

        self._candles: deque[tuple[float, float, float]] = deque(maxlen=self._window)
        self._score = 0.0
        self._vpoc = 0.0
        self._vah = 0.0
        self._val = 0.0

    def reset(self) -> None:
        self._candles.clear()
        self._score = 0.0
        self._vpoc = 0.0
        self._vah = 0.0
        self._val = 0.0

    @property
    def score(self) -> float:
        return self._score

    @property
    def is_ready(self) -> bool:
        return len(self._candles) >= 10

    @property
    def vpoc(self) -> float:
        return self._vpoc

    @property
    def vah(self) -> float:
        return self._vah

    @property
    def val(self) -> float:
        return self._val

    def add_candle(self, high: float, low: float, close: float, volume: float) -> float:
        self._candles.append((high, low, volume))

        if len(self._candles) < 10:
            self._score = 0.0
            return self._score

        self._build_profile()
        self._score = self._compute_score(close)
        return self._score

    def _build_profile(self) -> None:
        all_highs = [c[0] for c in self._candles]
        all_lows = [c[1] for c in self._candles]
        price_high = max(all_highs)
        price_low = min(all_lows)

        if price_high <= price_low:
            return

        bin_size = (price_high - price_low) / self._num_bins
        bins = [0.0] * self._num_bins

        for h, l, vol in self._candles:
            if vol <= 0:
                continue
            lo_bin = max(0, int((l - price_low) / bin_size))
            hi_bin = min(self._num_bins - 1, int((h - price_low) / bin_size))
            n_bins = hi_bin - lo_bin + 1
            vol_per_bin = vol / n_bins if n_bins > 0 else vol
            for b in range(lo_bin, hi_bin + 1):
                bins[b] += vol_per_bin

        poc_idx = max(range(self._num_bins), key=lambda i: bins[i])
        self._vpoc = price_low + (poc_idx + 0.5) * bin_size

        total_vol = sum(bins)
        if total_vol <= 0:
            self._vah = price_high
            self._val = price_low
            return

        target_vol = total_vol * self._va_pct
        accumulated = bins[poc_idx]
        lo = poc_idx
        hi = poc_idx

        while accumulated < target_vol and (lo > 0 or hi < self._num_bins - 1):
            expand_lo = bins[lo - 1] if lo > 0 else -1.0
            expand_hi = bins[hi + 1] if hi < self._num_bins - 1 else -1.0

            if expand_lo >= expand_hi and lo > 0:
                lo -= 1
                accumulated += bins[lo]
            elif hi < self._num_bins - 1:
                hi += 1
                accumulated += bins[hi]
            else:
                break

        self._val = price_low + lo * bin_size
        self._vah = price_low + (hi + 1) * bin_size

    def _compute_score(self, price: float) -> float:
        if self._vah <= self._val:
            return 0.0

        va_range = self._vah - self._val
        if va_range == 0:
            return 0.0

        if price < self._val:
            dist = (self._val - price) / va_range
            return min(dist * 2.0, 1.0)

        if price > self._vah:
            dist = (price - self._vah) / va_range
            return -min(dist * 2.0, 1.0)

        relative = (price - self._val) / va_range
        return -(relative - 0.5) * 0.4
