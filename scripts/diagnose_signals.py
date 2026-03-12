"""
Diagnostic tool: runs the backtest engine and reports signal distribution.
Helps identify why the CE isn't generating LONG/SHORT signals.
"""

from __future__ import annotations

import logging
from collections import Counter, deque

from backtest.data_loader import BacktestTick
from config.settings import (
    BacktestSettings, OBISettings, VPINSettings, SpreadSettings,
    DepthErosionSettings, SpoofingSettings, ClearanceSettings,
    ConfirmationSettings, OptionsSettings,
)
from signals.obi import OBIModule
from signals.vpin import VPINModule
from signals.spread_monitor import SpreadMonitor
from signals.depth_erosion import DepthErosionMonitor
from signals.spoofing_detector import SpoofingDetector
from signals.clearance_detector import ClearanceDetector
from signals.options_layer import OptionsLayer
from signals.confirmation import ConfirmationEngine
from backtest.sim_engine import BacktestEngine
from data.converter import trades_to_ticks

logger = logging.getLogger(__name__)


def diagnose(ticks: list[BacktestTick]) -> None:
    bt = BacktestSettings()
    obi = OBIModule()
    vpin = VPINModule()
    spread = SpreadMonitor()
    options = OptionsLayer()

    de_cfg = DepthErosionSettings()
    depth = DepthErosionMonitor(
        check_interval=de_cfg.check_interval,
        erosion_threshold=de_cfg.erosion_threshold,
        price_stability=de_cfg.price_stability,
        depth=de_cfg.depth,
    )
    sp_cfg = SpoofingSettings()
    spoof = SpoofingDetector(
        size_threshold=sp_cfg.size_threshold,
        cancel_window_ms=sp_cfg.cancel_window_ms,
    )
    cl_cfg = ClearanceSettings()
    clearance = ClearanceDetector(
        one_sided_threshold=cl_cfg.one_sided_threshold,
        ask_slide_pct=cl_cfg.ask_slide_pct,
    )
    ce = ConfirmationEngine()
    regime = BacktestEngine._build_regime(bt.options_regime)

    obi_scores = []
    vpin_values = []
    spread_statuses = Counter()
    depth_scores = []
    spoof_scores = []
    clearance_scores = []
    ce_scores = []
    ce_actions = Counter()
    recent_trades: deque = deque(maxlen=300)

    for i, tick in enumerate(ticks):
        mid = tick.mid_price
        if mid <= 0:
            continue

        for t in tick.trades:
            recent_trades.append(t)

        obi_out = obi.update({"bids": tick.bids, "asks": tick.asks})
        obi_scores.append(obi_out.score)

        for t in tick.trades:
            vpin.process_trade(t["price"], t["qty"], mid)
        vpin_out = vpin.update({})
        vpin_val = vpin_out.metadata.get("vpin")
        if vpin_val is not None:
            vpin_values.append(vpin_val)

        spread_out = spread.update({"best_bid": tick.best_bid, "best_ask": tick.best_ask})
        spread_status = spread_out.metadata.get("status", "MM_ACTIVE")
        spread_statuses[spread_status] += 1

        spoof_out = spoof.update({
            "bids": tick.bids, "asks": tick.asks,
            "timestamp_ms": int(tick.timestamp * 1000),
        })
        spoof_scores.append(spoof_out.score)

        depth_out = depth.update({
            "bids": tick.bids, "asks": tick.asks,
            "mid_price": mid, "timestamp": tick.timestamp,
            "spoof_active": spoof_out.metadata.get("is_active", False),
        })
        depth_scores.append(depth_out.score)

        clearance_out = clearance.update({
            "bids": tick.bids, "asks": tick.asks,
            "recent_trades": list(recent_trades)[-200:],
        })
        clearance_scores.append(clearance_out.score)

        options_out = options.update({
            "options_chain": getattr(tick, "options_chain", []),
            "spot_price": mid,
        })

        signals = {
            "OBI": obi_out.score,
            "OBI_HISTORY": obi.history[-10:],
            "SPREAD_SCORE": spread_out.metadata.get("score", 0),
            "DEPTH_SCORE": depth_out.score,
            "SPOOF_LIST": spoof_out.metadata.get("spoofs", []),
            "SPOOF_SCORE": spoof_out.score,
            "CLEARANCE_STATUS": clearance_out.metadata.get("status", "NORMAL"),
            "CLEARANCE_SCORE": clearance_out.score,
            "OPTIONS_SCORE": options_out.score,
        }

        decision = ce.evaluate(signals, vpin_val, spread_status, regime)
        ce_scores.append(decision.score)
        ce_actions[decision.action.value] += 1

    print("\n=== SIGNAL DIAGNOSTICS ===\n")
    print(f"Total ticks: {len(ticks)}")
    print(f"VPIN buckets filled: {vpin._total_buckets_filled}")
    print(f"VPIN ready: {vpin.is_ready}")
    print()

    _print_stats("OBI Score", obi_scores)
    _print_stats("VPIN Value", vpin_values)
    _print_stats("Depth Score", depth_scores)
    _print_stats("Spoof Score", spoof_scores)
    _print_stats("Clearance Score", clearance_scores)
    _print_stats("CE Composite Score", ce_scores)

    print(f"\nSpread Status Distribution: {dict(spread_statuses)}")
    print(f"CE Action Distribution:    {dict(ce_actions)}")


def _print_stats(label: str, values: list[float]) -> None:
    if not values:
        print(f"{label}: no data")
        return
    mn, mx = min(values), max(values)
    avg = sum(values) / len(values)
    positive = sum(1 for v in values if v > 0)
    negative = sum(1 for v in values if v < 0)
    zero = sum(1 for v in values if v == 0)
    print(
        f"{label:25s}  "
        f"min={mn:+.4f}  max={mx:+.4f}  avg={avg:+.4f}  "
        f"(+{positive}/-{negative}/0={zero}  n={len(values)})"
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    trades_dir = sys.argv[1] if len(sys.argv) > 1 else "historical_data/BTCUSDT/trades"
    bucket_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 60000

    print(f"Loading from {trades_dir} (bucket={bucket_ms}ms)...")
    ticks = trades_to_ticks(trades_dir, bucket_ms=bucket_ms)
    diagnose(ticks)
