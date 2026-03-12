from __future__ import annotations

from collections import deque

from config.constants import ClearanceStatus, SignalDirection
from signals.base import BaseSignalModule, SignalOutput


class ClearanceDetector(BaseSignalModule):
    """
    M6: Inventory Clearance Detector — MM Dumping / Loading.

    Detects when a Market Maker is aggressively clearing accumulated
    inventory via 4 simultaneous traces:

      Trace 1 — One-sided heavy trade flow (>72% in one direction)
      Trace 2 — Best ask sliding down (MM selling cheap)
      Trace 3 — Cluster of abnormally large sell trades
      Trace 4 — Bid-side thinning (MM not buying)

    score >= 3 → CLEARANCE_ACTIVE → GLOBAL VETO
    score == 2 → CLEARANCE_POSSIBLE (warning only)
    """

    @property
    def name(self) -> str:
        return "CLEARANCE"

    def __init__(
        self,
        one_sided_threshold: float = 0.72,
        ask_slide_pct: float = 0.0008,
        large_trade_multiplier: float = 4.0,
        large_trade_min_cluster: int = 3,
        bid_thin_pct: float = 0.55,
        ob_history_depth: int = 5,
        recent_trade_window: int = 100,
    ):
        self._one_sided = one_sided_threshold
        self._ask_slide = ask_slide_pct
        self._large_mult = large_trade_multiplier
        self._large_min = large_trade_min_cluster
        self._bid_thin = bid_thin_pct
        self._ob_depth = ob_history_depth
        self._trade_window = recent_trade_window

        self._ob_history: deque[dict] = deque(maxlen=ob_history_depth + 1)
        self._last_status = ClearanceStatus.NORMAL

    def reset(self) -> None:
        self._ob_history.clear()
        self._last_status = ClearanceStatus.NORMAL

    def update(self, data: dict) -> SignalOutput:
        """
        Expected data keys:
          bids: list[{price, qty}]
          asks: list[{price, qty}]
          recent_trades: list[{qty, side}]  (last ~100-200 trades)
        """
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        recent_trades = data.get("recent_trades", [])

        ob_snap = {"bids": bids, "asks": asks}
        self._ob_history.append(ob_snap)

        score, traces = self._detect(ob_snap, recent_trades)

        if score >= 4:  # P8: require all 4 traces to reduce false positives
            status = ClearanceStatus.CLEARANCE_ACTIVE
        elif score >= 2:
            status = ClearanceStatus.CLEARANCE_POSSIBLE
        else:
            status = ClearanceStatus.NORMAL

        self._last_status = status

        direction = SignalDirection.BEARISH if score >= 3 else SignalDirection.NEUTRAL

        return SignalOutput(
            module=self.name,
            direction=direction,
            raw_value=float(score),
            confidence=min(score / 4.0, 1.0),
            metadata={
                "status": status.value,
                "score": score,
                "traces": traces,
                "is_veto": status == ClearanceStatus.CLEARANCE_ACTIVE,
            },
        )

    def _detect(self, ob: dict, recent_trades: list[dict]) -> tuple[int, list[str]]:
        score = 0
        traces: list[str] = []

        trades_window = recent_trades[-self._trade_window:]
        if trades_window:
            buys = sum(t["qty"] for t in trades_window if t.get("side") == "buy")
            sells = sum(t["qty"] for t in trades_window if t.get("side") == "sell")
            total = buys + sells
            if total > 0:
                if buys / total > self._one_sided:
                    score += 1
                    traces.append("ONE_SIDED_BUY")
                elif sells / total > self._one_sided:
                    score += 1
                    traces.append("ONE_SIDED_SELL")

        if len(self._ob_history) >= self._ob_depth:
            old_ob = list(self._ob_history)[-self._ob_depth]
            old_asks = old_ob.get("asks", [])
            new_asks = ob.get("asks", [])
            if old_asks and new_asks:
                old_best = old_asks[0]["price"]
                new_best = new_asks[0]["price"]
                if old_best > 0 and new_best < old_best * (1 - self._ask_slide):
                    score += 1
                    traces.append("ASK_SLIDING_DOWN")

        if len(recent_trades) > 50:
            avg_qty = sum(t["qty"] for t in recent_trades[-200:]) / min(len(recent_trades[-200:]), 200)
            big_sells = [
                t for t in recent_trades[-20:]
                if t.get("side") == "sell" and t["qty"] > avg_qty * self._large_mult
            ]
            if len(big_sells) >= self._large_min:
                score += 1
                traces.append("LARGE_SELL_CLUSTER")

        if len(self._ob_history) >= self._ob_depth:
            old_ob = list(self._ob_history)[-self._ob_depth]
            old_bids = old_ob.get("bids", [])
            new_bids = ob.get("bids", [])
            old_bd = sum(l["qty"] for l in old_bids[:10])
            new_bd = sum(l["qty"] for l in new_bids[:10])
            if old_bd > 0 and new_bd < old_bd * self._bid_thin:
                score += 1
                traces.append("BID_THINNING")

        return score, traces

    @property
    def last_status(self) -> ClearanceStatus:
        return self._last_status
