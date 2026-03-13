"""
Diagnostic: run signal modules on real OB data to understand distributions.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter, deque

sys.path.insert(0, ".")

from backtest.data_loader import BacktestTick
from config.settings import (
    BacktestSettings, OBISettings, VPINSettings, DepthErosionSettings,
    SpoofingSettings, ClearanceSettings,
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
from data.converter import tardis_ob_to_ticks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger(__name__)


def diagnose_real_ob(ticks: list[BacktestTick]) -> None:
    bt = BacktestSettings()
    obi = OBIModule(OBISettings(depth=20))
    vpin = VPINModule()
    spread = SpreadMonitor()
    options = OptionsLayer()

    de_cfg = DepthErosionSettings()
    depth_mod = DepthErosionMonitor(
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

    total_trades_matched = 0

    for i, tick in enumerate(ticks):
        mid = tick.mid_price
        if mid <= 0:
            continue

        for t in tick.trades:
            recent_trades.append(t)
        total_trades_matched += len(tick.trades)

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

        depth_out = depth_mod.update({
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

    print("\n" + "=" * 60)
    print("  REAL ORDER BOOK SIGNAL DIAGNOSTICS")
    print("=" * 60)
    print(f"\n  Ticks analyzed:     {len(ticks):,}")
    print(f"  Trades matched:     {total_trades_matched:,}")
    print(f"  VPIN buckets:       {vpin._total_buckets_filled}")
    print(f"  VPIN ready:         {vpin.is_ready}")
    print()

    _print_stats("OBI Score", obi_scores)
    _print_stats("VPIN Value", vpin_values)
    _print_stats("Depth Score", depth_scores)
    _print_stats("Spoof Score", spoof_scores)
    _print_stats("Clearance Score", clearance_scores)
    _print_stats("CE Score", ce_scores)

    print(f"\n  Spread Status: {dict(spread_statuses)}")
    print(f"  CE Actions:    {dict(ce_actions)}")

    if obi_scores:
        strong_bull = sum(1 for s in obi_scores if s > 0.15)
        strong_bear = sum(1 for s in obi_scores if s < -0.15)
        print(f"\n  OBI > 0.15 (bullish):   {strong_bull} ({100*strong_bull/len(obi_scores):.1f}%)")
        print(f"  OBI < -0.15 (bearish):  {strong_bear} ({100*strong_bear/len(obi_scores):.1f}%)")

    if vpin_values:
        high_vpin = sum(1 for v in vpin_values if v > 0.65)
        veto_vpin = sum(1 for v in vpin_values if v > 0.80)
        print(f"\n  VPIN > 0.65 (warning):  {high_vpin} ({100*high_vpin/len(vpin_values):.1f}%)")
        print(f"  VPIN > 0.80 (veto):     {veto_vpin} ({100*veto_vpin/len(vpin_values):.1f}%)")

    print("\n" + "=" * 60)


def _print_stats(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: no data")
        return
    mn, mx = min(values), max(values)
    avg = sum(values) / len(values)
    positive = sum(1 for v in values if v > 0)
    negative = sum(1 for v in values if v < 0)
    zero = sum(1 for v in values if v == 0)

    sorted_v = sorted(values)
    p10 = sorted_v[len(sorted_v) // 10]
    p50 = sorted_v[len(sorted_v) // 2]
    p90 = sorted_v[9 * len(sorted_v) // 10]

    print(
        f"  {label:22s}  "
        f"min={mn:+.4f}  p10={p10:+.4f}  p50={p50:+.4f}  p90={p90:+.4f}  max={mx:+.4f}  "
        f"(+{positive}/-{negative}/0={zero})"
    )


def load_single_month(data_dir: str, month_pattern: str = "2026-02", sample: int = 50, limit: int = 20000):
    """Load just one month for fast diagnostics."""
    from pathlib import Path
    import gzip
    import csv
    from backtest.data_loader import BacktestTick

    data_path = Path(data_dir)
    snap_files = sorted(data_path.glob(f"*book_snapshot_25*{month_pattern}*"))
    trade_files = sorted(data_path.glob(f"*trades*{month_pattern}*"))

    if not snap_files:
        print(f"No snapshot files for {month_pattern}")
        return []

    trades_by_sec: dict[int, list[dict]] = {}
    for tf in trade_files:
        print(f"Loading trades: {tf.name}")
        with gzip.open(tf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_us = int(row["timestamp"])
                    sec_key = ts_us // 1_000_000
                    trades_by_sec.setdefault(sec_key, []).append({
                        "price": float(row["price"]),
                        "qty": float(row["amount"]),
                        "side": row["side"].strip().lower(),
                        "timestamp": ts_us / 1_000_000,
                    })
                except (ValueError, KeyError):
                    continue

    print(f"Indexed trades for {len(trades_by_sec)} seconds")

    ticks: list[BacktestTick] = []
    row_count = 0
    for sf in snap_files:
        print(f"Processing {sf.name}...")
        with gzip.open(sf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_count += 1
                if row_count % sample != 0:
                    continue
                try:
                    ts_us = int(row.get("timestamp", 0))
                except (ValueError, TypeError):
                    continue
                ts_sec = ts_us / 1_000_000
                bids, asks = [], []
                for i in range(20):
                    bp = row.get(f"bids[{i}].price", "")
                    bq = row.get(f"bids[{i}].amount", "")
                    ap = row.get(f"asks[{i}].price", "")
                    aq = row.get(f"asks[{i}].amount", "")
                    if bp and bq:
                        bids.append({"price": float(bp), "qty": float(bq)})
                    if ap and aq:
                        asks.append({"price": float(ap), "qty": float(aq)})
                if len(bids) < 3 or len(asks) < 3:
                    continue
                sec_key = int(ts_sec)
                ticks.append(BacktestTick(
                    timestamp=ts_sec,
                    bids=bids, asks=asks,
                    trades=trades_by_sec.get(sec_key, []),
                    best_bid=bids[0]["price"],
                    best_ask=asks[0]["price"],
                ))
                if len(ticks) >= limit:
                    break
        if len(ticks) >= limit:
            break

    ticks.sort(key=lambda t: t.timestamp)
    print(f"Loaded {len(ticks)} ticks from {row_count} rows")
    return ticks


if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "historical_data/tardis/binance-futures"
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-02"
    sample = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 20000

    print(f"Loading {month} from {data_dir} (sample_every={sample}, limit={limit})...")
    ticks = load_single_month(data_dir, month, sample, limit)
    if ticks:
        diagnose_real_ob(ticks)
