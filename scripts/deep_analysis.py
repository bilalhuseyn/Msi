"""
Deep analysis: traces every decision the bot makes on real OB data.
Produces a complete breakdown of WHY trades fail.
"""

from __future__ import annotations

import gzip
import csv
import logging
import sys
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, ".")

from backtest.data_loader import BacktestTick
from backtest.fee_model import FeeModel
from backtest.portfolio import Portfolio, Position
from config.settings import (
    BacktestSettings, OBISettings, VPINSettings, SpreadSettings,
    DepthErosionSettings, SpoofingSettings, ClearanceSettings,
    ConfirmationSettings,
)
from config.constants import DecisionAction
from signals.obi import OBIModule
from signals.vpin import VPINModule
from signals.spread_monitor import SpreadMonitor
from signals.depth_erosion import DepthErosionMonitor
from signals.spoofing_detector import SpoofingDetector
from signals.clearance_detector import ClearanceDetector
from signals.options_layer import OptionsLayer
from signals.confirmation import ConfirmationEngine
from backtest.sim_engine import BacktestEngine

logging.basicConfig(level=logging.WARNING, format="%(message)s")


def load_month(data_dir: str, month: str, sample: int = 100, limit: int = 15000):
    data_path = Path(data_dir)
    snap_files = sorted(data_path.glob(f"*book_snapshot_25*{month}*"))
    trade_files = sorted(data_path.glob(f"*trades*{month}*"))
    if not snap_files:
        return []

    trades_by_sec: dict[int, list[dict]] = {}
    for tf in trade_files:
        with gzip.open(tf, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
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

    ticks: list[BacktestTick] = []
    row_count = 0
    for sf in snap_files:
        with gzip.open(sf, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
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
                    timestamp=ts_sec, bids=bids, asks=asks,
                    trades=trades_by_sec.get(sec_key, []),
                    best_bid=bids[0]["price"], best_ask=asks[0]["price"],
                ))
                if len(ticks) >= limit:
                    break
        if len(ticks) >= limit:
            break

    ticks.sort(key=lambda t: t.timestamp)
    return ticks


def deep_analyze(ticks: list[BacktestTick]):
    bt = BacktestSettings(cooldown_seconds=600)
    vpin_s = VPINSettings(bucket_size=5.0, window=50, veto_threshold=0.85,
                          warning_threshold=0.70, min_buckets=30)
    obi_s = OBISettings(depth=20, bullish_threshold=0.55, bearish_threshold=0.45,
                        consistency_window=2)
    ce_s = ConfirmationSettings(long_threshold=0.20, short_threshold=-0.20)

    obi = OBIModule(obi_s)
    vpin = VPINModule(vpin_s)
    spread = SpreadMonitor()
    options = OptionsLayer()
    de_cfg = DepthErosionSettings()
    depth_mod = DepthErosionMonitor(
        check_interval=de_cfg.check_interval, erosion_threshold=de_cfg.erosion_threshold,
        price_stability=de_cfg.price_stability, depth=de_cfg.depth,
    )
    sp_cfg = SpoofingSettings()
    spoof = SpoofingDetector(size_threshold=sp_cfg.size_threshold,
                             cancel_window_ms=sp_cfg.cancel_window_ms)
    cl_cfg = ClearanceSettings()
    clearance = ClearanceDetector(one_sided_threshold=cl_cfg.one_sided_threshold,
                                  ask_slide_pct=cl_cfg.ask_slide_pct)
    ce = ConfirmationEngine(ce_s)
    regime = BacktestEngine._build_regime(bt.options_regime)

    fee_model = FeeModel(taker_fee_pct=bt.taker_fee_pct, slippage_pct=bt.slippage_pct)
    portfolio = Portfolio(initial_balance=bt.initial_balance, fee_model=fee_model,
                          cooldown_seconds=bt.cooldown_seconds,
                          max_hold_seconds=bt.max_hold_hours * 3600)

    recent_trades: deque = deque(maxlen=300)
    atr_window: deque = deque(maxlen=14)
    prev_price = None

    ce_action_counts = Counter()
    veto_reasons = Counter()
    block_reasons = Counter()
    signal_directions = Counter()
    exit_reasons = Counter()

    obi_at_entry = []
    vpin_at_entry = []
    spread_at_entry = []
    depth_at_entry = []
    ce_scores_at_entry = []
    price_at_entry = []
    direction_at_entry = []

    all_obi = []
    all_vpin = []
    all_ce = []

    trade_details = []

    ticks_with_signal = 0
    ticks_with_strong_obi = 0

    for i, tick in enumerate(ticks):
        mid = tick.mid_price
        if mid <= 0:
            continue

        if prev_price is not None:
            atr_window.append(abs(mid - prev_price))
        prev_price = mid
        atr = sum(atr_window) / len(atr_window) if atr_window else mid * 0.005

        for t in tick.trades:
            recent_trades.append(t)

        closed = portfolio.update(mid, tick.timestamp, atr)
        for c in closed:
            exit_reasons[c.exit_reason] += 1
            trade_details.append({
                "entry_price": c.entry_price,
                "exit_price": c.exit_price,
                "direction": "LONG" if c.direction == 1 else "SHORT",
                "pnl_usd": c.pnl_usd,
                "pnl_r": c.pnl_r,
                "fees": c.fees_total,
                "exit_reason": c.exit_reason,
                "hold_secs": c.hold_time_seconds,
                "size": c.size,
            })

        obi_out = obi.update({"bids": tick.bids, "asks": tick.asks})
        for t in tick.trades:
            vpin.process_trade(t["price"], t["qty"], mid)
        vpin_out = vpin.update({})
        spread_out = spread.update({"best_bid": tick.best_bid, "best_ask": tick.best_ask})
        spoof_out = spoof.update({"bids": tick.bids, "asks": tick.asks,
                                   "timestamp_ms": int(tick.timestamp * 1000)})
        depth_out = depth_mod.update({"bids": tick.bids, "asks": tick.asks,
                                       "mid_price": mid, "timestamp": tick.timestamp,
                                       "spoof_active": spoof_out.metadata.get("is_active", False)})
        clearance_out = clearance.update({"bids": tick.bids, "asks": tick.asks,
                                           "recent_trades": list(recent_trades)[-200:]})
        options_out = options.update({"options_chain": [], "spot_price": mid})

        vpin_val = vpin_out.metadata.get("vpin")
        spread_status = spread_out.metadata.get("status", "MM_ACTIVE")

        signals = {
            "OBI": obi_out.score, "OBI_HISTORY": obi.history[-10:],
            "SPREAD_SCORE": spread_out.metadata.get("score", 0),
            "DEPTH_SCORE": depth_out.score,
            "SPOOF_LIST": spoof_out.metadata.get("spoofs", []),
            "SPOOF_SCORE": spoof_out.score,
            "CLEARANCE_STATUS": clearance_out.metadata.get("status", "NORMAL"),
            "CLEARANCE_SCORE": clearance_out.score,
            "OPTIONS_SCORE": options_out.score,
        }

        decision = ce.evaluate(signals, vpin_val, spread_status, regime)
        ce_action_counts[decision.action.value] += 1

        all_obi.append(obi_out.score)
        if vpin_val is not None:
            all_vpin.append(vpin_val)
        all_ce.append(decision.score)

        if obi_out.score != 0:
            ticks_with_strong_obi += 1

        if decision.action == DecisionAction.VETO:
            veto_reasons[decision.reason] += 1
            veto_closed = portfolio.force_close_all(mid, tick.timestamp, reason=f"veto:{decision.reason}")
            for c in veto_closed:
                exit_reasons[c.exit_reason] += 1
                trade_details.append({
                    "entry_price": c.entry_price, "exit_price": c.exit_price,
                    "direction": "LONG" if c.direction == 1 else "SHORT",
                    "pnl_usd": c.pnl_usd, "pnl_r": c.pnl_r, "fees": c.fees_total,
                    "exit_reason": c.exit_reason, "hold_secs": c.hold_time_seconds,
                    "size": c.size,
                })
            continue

        if decision.action in (DecisionAction.LONG, DecisionAction.SHORT):
            ticks_with_signal += 1
            signal_directions[decision.action.value] += 1

            can_open, block_reason = portfolio.can_open(tick.timestamp)
            if can_open:
                direction = decision.direction
                stop_dist = max(atr * bt.stop_atr_multiplier, mid * bt.min_stop_pct)
                stop_price = mid - direction * stop_dist
                tp1_price = mid + direction * stop_dist * bt.tp1_rr
                tp2_price = mid + direction * stop_dist * bt.tp2_rr

                risk_usd = portfolio.balance * 0.01
                size = risk_usd / stop_dist if stop_dist > 0 else 0
                max_size = portfolio.balance * 0.10 / mid if mid > 0 else 0
                size = min(size, max_size)

                if size > 0:
                    obi_at_entry.append(obi_out.score)
                    vpin_at_entry.append(vpin_val)
                    spread_at_entry.append(spread_status)
                    depth_at_entry.append(depth_out.score)
                    ce_scores_at_entry.append(decision.score)
                    price_at_entry.append(mid)
                    direction_at_entry.append("LONG" if direction == 1 else "SHORT")

                    portfolio.open_position(
                        price=mid, size=size, direction=direction,
                        stop_price=stop_price, tp1_price=tp1_price,
                        tp2_price=tp2_price, timestamp=tick.timestamp,
                    )
            else:
                block_reasons[block_reason] += 1

    final_closed = portfolio.force_close_all(ticks[-1].mid_price, ticks[-1].timestamp, "end")
    for c in final_closed:
        exit_reasons[c.exit_reason] += 1
        trade_details.append({
            "entry_price": c.entry_price, "exit_price": c.exit_price,
            "direction": "LONG" if c.direction == 1 else "SHORT",
            "pnl_usd": c.pnl_usd, "pnl_r": c.pnl_r, "fees": c.fees_total,
            "exit_reason": c.exit_reason, "hold_secs": c.hold_time_seconds,
            "size": c.size,
        })

    print("\n" + "=" * 72)
    print("  DEEP ANALYSIS: HOW THE BOT THINKS AND TRADES")
    print("=" * 72)

    print(f"\n  Ticks processed: {len(ticks):,}")
    print(f"  Price range: ${min(t.mid_price for t in ticks):,.2f} - ${max(t.mid_price for t in ticks):,.2f}")
    time_span_h = (ticks[-1].timestamp - ticks[0].timestamp) / 3600
    print(f"  Time span: {time_span_h:.1f} hours ({time_span_h/24:.1f} days)")
    print(f"  Final balance: ${portfolio.balance:,.2f} (from $10,000)")

    print("\n" + "-" * 72)
    print("  SECTION 1: DECISION PIPELINE BREAKDOWN")
    print("-" * 72)
    total = sum(ce_action_counts.values())
    for action, count in sorted(ce_action_counts.items()):
        pct = 100 * count / total if total else 0
        print(f"    {action:12s}: {count:6d} ({pct:5.1f}%)")

    print(f"\n  Total signals (LONG+SHORT): {ticks_with_signal}")
    print(f"  Ticks with OBI != 0:        {ticks_with_strong_obi} ({100*ticks_with_strong_obi/len(ticks):.1f}%)")

    print("\n  Signal direction breakdown:")
    for d, c in sorted(signal_directions.items()):
        print(f"    {d:12s}: {c:6d}")

    print("\n  VETO reason breakdown:")
    for r, c in sorted(veto_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:30s}: {c:6d}")

    print("\n  Signal BLOCKED reasons:")
    for r, c in sorted(block_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:30s}: {c:6d}")

    print("\n" + "-" * 72)
    print("  SECTION 2: VPIN STATE")
    print("-" * 72)
    print(f"  Buckets filled: {vpin._total_buckets_filled}")
    print(f"  VPIN ready: {vpin.is_ready}")
    if all_vpin:
        print(f"  VPIN range: {min(all_vpin):.4f} - {max(all_vpin):.4f}")
        print(f"  VPIN median: {sorted(all_vpin)[len(all_vpin)//2]:.4f}")
        veto_count = sum(1 for v in all_vpin if v > 0.85)
        warn_count = sum(1 for v in all_vpin if 0.70 < v <= 0.85)
        print(f"  VPIN > 0.85 (veto): {veto_count}")
        print(f"  VPIN 0.70-0.85 (warning): {warn_count}")
    else:
        print("  VPIN: NEVER ACTIVATED (0 valid readings)")

    print("\n" + "-" * 72)
    print("  SECTION 3: REGIME MODIFIER EFFECT")
    print("-" * 72)
    print(f"  Active regime: {bt.options_regime}")
    if bt.options_regime == "LONG_GAMMA":
        print(f"  Multiplier: x0.85 (dampens all CE scores by 15%)")
        print(f"  Impact: A raw CE score of +0.24 becomes +0.20 (barely LONG)")
        print(f"         A raw CE score of +0.23 becomes +0.196 -> NEUTRAL (blocked!)")
    print(f"  CE scores at entry: ", end="")
    if ce_scores_at_entry:
        print(f"min={min(ce_scores_at_entry):+.4f} max={max(ce_scores_at_entry):+.4f} "
              f"avg={sum(ce_scores_at_entry)/len(ce_scores_at_entry):+.4f}")
    else:
        print("none")

    print("\n" + "-" * 72)
    print("  SECTION 4: TRADE-BY-TRADE RESULTS")
    print("-" * 72)

    if trade_details:
        print(f"\n  {'#':>3}  {'Dir':5}  {'Entry':>10}  {'Exit':>10}  {'PnL':>8}  {'Fees':>6}  {'Hold':>8}  {'Exit Reason':15}")
        print(f"  {'---':>3}  {'---':5}  {'---':>10}  {'---':>10}  {'---':>8}  {'---':>6}  {'---':>8}  {'---':15}")
        for idx, td in enumerate(trade_details, 1):
            hold_min = td["hold_secs"] / 60
            print(f"  {idx:3d}  {td['direction']:5}  ${td['entry_price']:>9,.2f}  ${td['exit_price']:>9,.2f}  "
                  f"${td['pnl_usd']:>+7.2f}  ${td['fees']:>5.2f}  {hold_min:>6.1f}m  {td['exit_reason']:15}")

        total_pnl = sum(t["pnl_usd"] for t in trade_details)
        total_fees = sum(t["fees"] for t in trade_details)
        winners = sum(1 for t in trade_details if t["pnl_usd"] > 0)
        print(f"\n  Total PnL: ${total_pnl:+.2f}  |  Total Fees: ${total_fees:.2f}  |  "
              f"Win rate: {100*winners/len(trade_details):.1f}%")

    print("\n  Exit reason distribution:")
    for r, c in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:20s}: {c:4d}")

    print("\n" + "-" * 72)
    print("  SECTION 5: ENTRY CONDITIONS AT TRADE OPEN")
    print("-" * 72)
    if obi_at_entry:
        print(f"  OBI at entry: {Counter(obi_at_entry)}")
    if spread_at_entry:
        print(f"  Spread at entry: {Counter(spread_at_entry)}")
    if depth_at_entry:
        print(f"  Depth at entry: {Counter(depth_at_entry)}")
    print(f"  Directions opened: {Counter(direction_at_entry)}")

    print("\n" + "-" * 72)
    print("  SECTION 6: FEE vs PNL ANALYSIS")
    print("-" * 72)
    if trade_details:
        gross_pnl = sum(t["pnl_usd"] + t["fees"] for t in trade_details)
        total_fees = sum(t["fees"] for t in trade_details)
        avg_notional = sum(t["entry_price"] * t["size"] for t in trade_details) / len(trade_details)
        print(f"  Gross PnL (before fees): ${gross_pnl:+.2f}")
        print(f"  Total fees paid:         ${total_fees:.2f}")
        print(f"  Net PnL (after fees):    ${gross_pnl - total_fees:+.2f}")
        print(f"  Fee as % of gross PnL:   {abs(total_fees/gross_pnl)*100:.1f}%" if gross_pnl != 0 else "  Fee/PnL: N/A")
        print(f"  Average notional/trade:  ${avg_notional:.2f}")
        print(f"  Average fee/trade:       ${total_fees/len(trade_details):.2f}")

    print("\n" + "-" * 72)
    print("  SECTION 7: STOP LOSS DISTANCE ANALYSIS")
    print("-" * 72)
    sl_trades = [t for t in trade_details if t["exit_reason"] == "stop_loss"]
    if sl_trades:
        avg_sl_loss = sum(t["pnl_usd"] for t in sl_trades) / len(sl_trades)
        avg_sl_hold = sum(t["hold_secs"] for t in sl_trades) / len(sl_trades) / 60
        for t in sl_trades[:5]:
            sl_dist_pct = abs(t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
            print(f"    {t['direction']:5} entry=${t['entry_price']:,.2f} SL=${t['exit_price']:,.2f} "
                  f"dist={sl_dist_pct:.3f}% loss=${t['pnl_usd']:+.2f} hold={t['hold_secs']/60:.1f}m")
        print(f"\n  Avg SL loss: ${avg_sl_loss:.2f}")
        print(f"  Avg SL hold time: {avg_sl_hold:.1f} min")
        print(f"  Total SL trades: {len(sl_trades)} / {len(trade_details)} ({100*len(sl_trades)/len(trade_details):.0f}%)")

    print("\n" + "=" * 72)
    print("  BOTTLENECK SUMMARY")
    print("=" * 72)

    bottlenecks = []

    if not all_vpin:
        bottlenecks.append("[CRITICAL] VPIN never activates -> veto gate is BLIND (no flow toxicity filtering)")
    elif vpin.is_ready and sum(1 for v in all_vpin if v > 0.85) > 0.3 * len(all_vpin):
        bottlenecks.append("[HIGH] VPIN vetoing >30% of ticks -> too aggressive")

    long_signals = signal_directions.get("LONG", 0)
    short_signals = signal_directions.get("SHORT", 0)
    if long_signals + short_signals > 0:
        bias = short_signals / (long_signals + short_signals) if (long_signals + short_signals) > 0 else 0
        if bias > 0.75:
            bottlenecks.append(f"[CRITICAL] Extreme SHORT bias: {short_signals}S vs {long_signals}L ({bias*100:.0f}% short)")
        elif bias < 0.25:
            bottlenecks.append(f"[HIGH] Extreme LONG bias: {long_signals}L vs {short_signals}S")

    if sl_trades and len(sl_trades) / max(len(trade_details), 1) > 0.7:
        bottlenecks.append(f"[CRITICAL] {len(sl_trades)}/{len(trade_details)} trades hit stop loss -> entries or stops miscalibrated")

    if trade_details:
        gross_pnl = sum(t["pnl_usd"] + t["fees"] for t in trade_details)
        total_fees = sum(t["fees"] for t in trade_details)
        if total_fees > abs(gross_pnl):
            bottlenecks.append(f"[HIGH] Fees (${total_fees:.0f}) > gross PnL (${abs(gross_pnl):.0f}) -> fees eating all edge")

    blocked = sum(block_reasons.values())
    if blocked > ticks_with_signal * 0.5:
        bottlenecks.append(f"[MEDIUM] {blocked} signals blocked ({100*blocked/max(ticks_with_signal,1):.0f}%) -> cooldown too aggressive")

    vetoes = sum(veto_reasons.values())
    if vetoes > 0.5 * len(ticks):
        bottlenecks.append(f"[HIGH] {vetoes} vetoes ({100*vetoes/len(ticks):.0f}% of ticks) -> veto gates too sensitive")

    if bt.options_regime == "LONG_GAMMA":
        bottlenecks.append("[MEDIUM] LONG_GAMMA regime dampens all scores by 15% (x0.85) -> marginal signals lost")

    for b in bottlenecks:
        print(f"  {b}")
    if not bottlenecks:
        print("  No critical bottlenecks detected.")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "historical_data/tardis/binance-futures"
    month = sys.argv[2] if len(sys.argv) > 2 else "2026-02"
    sample = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    limit = int(sys.argv[4]) if len(sys.argv) > 4 else 15000

    print(f"Loading {month} from {data_dir}...")
    ticks = load_month(data_dir, month, sample, limit)
    if ticks:
        deep_analyze(ticks)
    else:
        print("No ticks loaded!")
