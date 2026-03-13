from __future__ import annotations

from collections import deque

from config.constants import SignalDirection
from config.settings import VPINSettings
from signals.base import BaseSignalModule, SignalOutput


class VPINModule(BaseSignalModule):
    """
    M2: Volume-synchronized Probability of Informed Trading.

    Measures "toxicity" of order flow — the proportion of informed trading.
    High VPIN signals that Market Makers are about to withdraw liquidity.

    P2 fix: Dynamic bucket sizing based on rolling hourly volume.
    The bucket_size setting is used as a baseline for the first hour;
    after that, bucket_size = hourly_volume × 0.0005 (≈50 buckets/hour).
    """

    _DYNAMIC_BUCKET_FACTOR = 0.0005  # 0.05% of hourly volume per bucket

    @property
    def name(self) -> str:
        return "VPIN"

    def __init__(self, settings: VPINSettings | None = None):
        cfg = settings or VPINSettings()
        self._bucket_size = cfg.bucket_size
        self._base_bucket_size = cfg.bucket_size
        self._window = cfg.window
        self._veto_thresh = cfg.veto_threshold
        self._warning_thresh = cfg.warning_threshold
        self._spike_pct = cfg.spike_filter_pct
        self._min_buckets = cfg.min_buckets

        self._current_bucket = {"buy": 0.0, "sell": 0.0}
        self._buckets: deque[float] = deque(maxlen=max(self._window * 2, 200))
        self._total_buckets_filled = 0
        self._last_vpin: float | None = None

        self._hourly_volume: deque[tuple[float, float]] = deque()
        self._volume_window_sec = 3600.0
        self._last_recalc_ts: float = 0.0
        self._recalc_interval_sec = 300.0

    def reset(self) -> None:
        self._current_bucket = {"buy": 0.0, "sell": 0.0}
        self._buckets.clear()
        self._total_buckets_filled = 0
        self._last_vpin = None
        self._hourly_volume.clear()
        self._bucket_size = self._base_bucket_size
        self._last_recalc_ts = 0.0

    def _recalculate_bucket_size(self, timestamp: float) -> None:
        """Adjust bucket_size based on rolling hourly volume."""
        cutoff = timestamp - self._volume_window_sec
        while self._hourly_volume and self._hourly_volume[0][0] < cutoff:
            self._hourly_volume.popleft()

        if len(self._hourly_volume) < 10:
            return

        hourly_vol = sum(qty for _, qty in self._hourly_volume)
        dynamic = hourly_vol * self._DYNAMIC_BUCKET_FACTOR
        self._bucket_size = max(dynamic, self._base_bucket_size * 0.1)

    def process_trade(self, price: float, qty: float, mid_price: float,
                      timestamp: float = 0.0) -> None:
        """Classify a trade and accumulate into the current bucket."""
        if timestamp > 0:
            self._hourly_volume.append((timestamp, qty))
            if timestamp - self._last_recalc_ts >= self._recalc_interval_sec:
                self._recalculate_bucket_size(timestamp)
                self._last_recalc_ts = timestamp

        if price >= mid_price:
            self._current_bucket["buy"] += qty
        else:
            self._current_bucket["sell"] += qty

        total = self._current_bucket["buy"] + self._current_bucket["sell"]
        if total >= self._bucket_size:
            imbalance = abs(
                self._current_bucket["buy"] - self._current_bucket["sell"]
            ) / total
            self._add_bucket(imbalance)
            self._current_bucket = {"buy": 0.0, "sell": 0.0}

    def _add_bucket(self, imbalance: float) -> None:
        if self._last_vpin is not None and len(self._buckets) > 0:
            last_bucket = self._buckets[-1] if self._buckets else 0.0
            if abs(imbalance - last_bucket) > self._spike_pct:
                imbalance = last_bucket

        self._buckets.append(imbalance)
        self._total_buckets_filled += 1

    @property
    def vpin(self) -> float | None:
        if self._total_buckets_filled < self._min_buckets:
            return None
        if len(self._buckets) < self._window:
            return None
        window = list(self._buckets)[-self._window :]
        return sum(window) / len(window)

    @property
    def is_ready(self) -> bool:
        return self._total_buckets_filled >= self._min_buckets

    def update(self, data: dict) -> SignalOutput:
        """
        Called with trade data: {price, qty, mid_price}.
        Can also be called periodically to just check current state.
        """
        if "price" in data and "qty" in data:
            mid = data.get("mid_price", data["price"])
            self.process_trade(data["price"], data["qty"], mid)

        current_vpin = self.vpin

        if current_vpin is None:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                raw_value=0.0,
                metadata={
                    "reason": "insufficient_data",
                    "buckets_filled": self._total_buckets_filled,
                    "min_required": self._min_buckets,
                },
            )

        self._last_vpin = current_vpin

        is_veto = current_vpin > self._veto_thresh
        is_warning = self._warning_thresh < current_vpin <= self._veto_thresh

        direction = SignalDirection.NEUTRAL
        if is_veto:
            direction = SignalDirection.BEARISH

        return SignalOutput(
            module=self.name,
            direction=direction,
            raw_value=current_vpin,
            confidence=min(current_vpin / self._veto_thresh, 1.0),
            metadata={
                "vpin": current_vpin,
                "is_veto": is_veto,
                "is_warning": is_warning,
                "buckets_filled": self._total_buckets_filled,
                "bucket_size": round(self._bucket_size, 2),
                "status": (
                    "VETO" if is_veto
                    else "WARNING" if is_warning
                    else "SAFE"
                ),
            },
        )
