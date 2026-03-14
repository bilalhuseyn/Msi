"""
Full signal-level diagnostic for all 10 modules.
Usage: python -m scripts.diagnose_full [month_pattern] [sample]
"""
from __future__ import annotations
import gzip, csv, logging, sys, pathlib
from collections import Counter, deque

logging.basicConfig(level=logging.WARNING)

# ── Load data ──────────────────────────────────────────────────────────────
DATA_DIR = "historical_data/tardis/binance-futures"
MONTH = sys.argv[1] if len(sys.argv) > 1 else "2026-02"
SAMPLE = int(sys.argv[2]) if len(sys.argv) > 2 else 20
LIMIT  = int(sys.argv[3]) if len(sys.argv) > 3 else 20000

from scripts.diagnose_real_ob import load_single_month
ticks = load_single_month(DATA_DIR, MONTH, SAMPLE, LIMIT)
print(f"Loaded {len(ticks)} ticks for {MONTH}")

# ── Build candles from same month ───────────────────────────────────────────
from signals.market_structure import build_candles_from_trades, MarketStructureReader
trades_by_sec: dict = {}
for tf in sorted(pathlib.Path(DATA_DIR).glob(f"*trades*{MONTH}*")):
    with gzip.open(tf, "rt") as f:
        for row in csv.DictReader(f):
            try:
                ts = int(row["timestamp"]) // 1_000_000
                trades_by_sec.setdefault(ts, []).append(
                    {"price": float(row["price"]), "qty": float(row["amount"])}
                )
            except Exception:
                pass
candles = build_candles_from_trades(trades_by_sec, interval_sec=900)
print(f"Built {len(candles)} x 15-min candles\n")

# ── Instantiate all modules ─────────────────────────────────────────────────
from config.settings import (
    OBISettings, VPINSettings, DepthErosionSettings,
    SpoofingSettings, ClearanceSettings, BacktestSettings,
)
from signals.obi import OBIModule
from signals.vpin import VPINModule
from signals.spread_monitor import SpreadMonitor
from signals.depth_erosion import DepthErosionMonitor
from signals.spoofing_detector import SpoofingDetector
from signals.clearance_detector import ClearanceDetector
from signals.momentum import MomentumModule
from signals.volume_profile import VolumeProfileModule
from signals.htf_trend import HTFTrendModule
from signals.confirmation import ConfirmationEngine
from signals.options_layer import OptionsLayer
from backtest.sim_engine import BacktestEngine

si = SAMPLE  # sample interval seconds
obi      = OBIModule(OBISettings(depth=20, bullish_threshold=0.55, bearish_threshold=0.45,
                                  ma_window=max(5, 600 // si)))
vpin     = VPINModule(VPINSettings(bucket_size=500.0, window=50,
                                   veto_threshold=0.90, warning_threshold=0.70, min_buckets=20))
spread   = SpreadMonitor()
depth_m  = DepthErosionMonitor()
spoof    = SpoofingDetector()
clearance = ClearanceDetector()
options  = OptionsLayer()
momentum = MomentumModule()
vol_prof = VolumeProfileModule()
htf      = HTFTrendModule()
ms       = MarketStructureReader(swing_lookback=3, min_swings=4)
ce       = ConfirmationEngine()
regime   = BacktestEngine._build_regime("NEUTRAL")

# ── Warm up modules with candles BEFORE last 5 ─────────────────────────────
WARMUP = max(0, len(candles) - 5)
for c in candles[:WARMUP]:
    ms.add_candle(c)
    momentum.add_candle(c.close)
    vol_prof.add_candle(c.high, c.low, c.close, c.volume)
    htf.add_candle_15m(c.open, c.high, c.low, c.close)

print("=== MODULE WARM-UP STATE ===")
print(f"  Candles used for warm-up : {WARMUP}/{len(candles)}")
print(f"  MS trend    : {ms.trend.value}")
print(f"  MS is_ready : {ms.is_ready}  "
      f"(swing_highs={len(ms._swing_highs)}, swing_lows={len(ms._swing_lows)}, need={ms._min_swings} each)")
print(f"  Momentum    : ready={momentum.is_ready}")
print(f"  VolProfile  : ready={vol_prof.is_ready}")
print(f"  HTFTrend    : ready={htf.is_ready}")
print()

# ── Replay loop ─────────────────────────────────────────────────────────────
EVAL_INTERVAL = 900
last_boundary = (int(ticks[0].timestamp) // EVAL_INTERVAL) * EVAL_INTERVAL
candle_idx = WARMUP
recent_trades: deque = deque(maxlen=300)

obi_vals, vpin_vals, ce_scores, depth_scores, clearance_scores = [], [], [], [], []
spread_counter: Counter = Counter()
ce_actions:     Counter = Counter()
veto_reasons:   Counter = Counter()
ms_blocks = 0
spoof_active_count = 0
eval_count = 0

for tick in ticks:
    mid = tick.mid_price
    if mid <= 0:
        continue

    # advance candles
    while candle_idx < len(candles):
        c = candles[candle_idx]
        if c.timestamp + 900 > tick.timestamp:
            break
        ms.add_candle(c)
        momentum.add_candle(c.close)
        vol_prof.add_candle(c.high, c.low, c.close, c.volume)
        htf.add_candle_15m(c.open, c.high, c.low, c.close)
        candle_idx += 1

    for t in tick.trades:
        recent_trades.append(t)
        vpin.process_trade(t["price"], t["qty"], mid)

    boundary = (int(tick.timestamp) // EVAL_INTERVAL) * EVAL_INTERVAL
    if boundary <= last_boundary:
        continue
    last_boundary = boundary
    eval_count += 1

    obi_out       = obi.update({"bids": tick.bids, "asks": tick.asks})
    vpin_out      = vpin.update({})
    spread_out    = spread.update({"best_bid": tick.best_bid, "best_ask": tick.best_ask})
    spoof_out     = spoof.update({"bids": tick.bids, "asks": tick.asks,
                                   "timestamp_ms": int(tick.timestamp * 1000)})
    depth_out     = depth_m.update({"bids": tick.bids, "asks": tick.asks,
                                     "mid_price": mid, "timestamp": tick.timestamp,
                                     "spoof_active": spoof_out.metadata.get("is_active", False)})
    clearance_out = clearance.update({"bids": tick.bids, "asks": tick.asks,
                                      "recent_trades": list(recent_trades)[-200:]})
    options_out   = options.update({"options_chain": [], "spot_price": mid})

    obi_ma       = obi_out.metadata.get("obi_ma", float(obi_out.score))
    vpin_val     = vpin_out.metadata.get("vpin")
    spread_status = spread_out.metadata.get("status", "MM_ACTIVE")

    obi_vals.append(obi_ma)
    if vpin_val is not None:
        vpin_vals.append(vpin_val)
    depth_scores.append(depth_out.score)
    clearance_scores.append(clearance_out.score)
    spread_counter[spread_status] += 1
    if spoof_out.metadata.get("is_active"):
        spoof_active_count += 1

    signals = {
        "OBI": obi_ma,
        "OBI_HISTORY": obi.history[-10:],
        "SPREAD_SCORE": spread_out.metadata.get("score", 0),
        "DEPTH_SCORE": depth_out.score,
        "SPOOF_LIST": spoof_out.metadata.get("spoofs", []),
        "SPOOF_SCORE": spoof_out.score,
        "CLEARANCE_STATUS": clearance_out.metadata.get("status", "NORMAL"),
        "CLEARANCE_SCORE": clearance_out.score,
        "OPTIONS_SCORE": options_out.score,
        "SR_PROXIMITY": ms.sr_proximity_score(mid),
        "MOMENTUM": momentum.score,
        "VOLUME_PROFILE": vol_prof.score,
        "HTF_TREND": htf.score,
        "_READY": {
            "SR_PROXIMITY": ms.is_ready,
            "MOMENTUM":     momentum.is_ready,
            "VOLUME_PROFILE": vol_prof.is_ready,
            "HTF_TREND":    htf.is_ready,
        },
    }
    decision = ce.evaluate(signals, vpin_val, spread_status, regime)
    ce_scores.append(decision.score)
    ce_actions[decision.action.value] += 1

    if decision.action.value in ("LONG", "SHORT"):
        if not ms.allows_direction(decision.direction):
            ms_blocks += 1

    if "veto" in decision.reason.lower() or "vpin" in decision.reason.lower():
        veto_reasons[decision.reason[:60]] += 1

# ── Print results ────────────────────────────────────────────────────────────
def pct(vals, cond): return 100 * sum(1 for v in vals if cond(v)) / max(len(vals), 1)
def median(vals): s = sorted(vals); return s[len(s)//2] if s else 0

SEP = "=" * 65
print(SEP)
print(f"  SIGNAL DIAGNOSTICS — {MONTH}  ({eval_count} x 15-min windows)")
print(SEP)

print("\n[1] OBI — Order Book Imbalance (P6: continuous gradient)")
print(f"    Bullish (obi_ma > +0.05):  {sum(1 for v in obi_vals if v> 0.05):4d}  ({pct(obi_vals, lambda v: v> 0.05):.1f}%)")
print(f"    Neutral (-0.05 to +0.05):  {sum(1 for v in obi_vals if -0.05<=v<=0.05):4d}  ({pct(obi_vals, lambda v: -0.05<=v<=0.05):.1f}%)")
print(f"    Bearish (obi_ma < -0.05):  {sum(1 for v in obi_vals if v<-0.05):4d}  ({pct(obi_vals, lambda v: v<-0.05):.1f}%)")
if obi_vals:
    print(f"    Range [{min(obi_vals):+.4f}, {max(obi_vals):+.4f}]  Median {median(obi_vals):+.4f}")

print("\n[2] VPIN — Flow Toxicity (P2: dynamic bucket size)")
print(f"    Buckets filled : {vpin._total_buckets_filled}  Ready: {vpin.is_ready}  (need min_buckets=20)")
if vpin_vals:
    print(f"    Range [{min(vpin_vals):.4f}, {max(vpin_vals):.4f}]  Median {median(vpin_vals):.4f}")
    print(f"    Warning >0.70  : {sum(1 for v in vpin_vals if v>0.70):4d}  ({pct(vpin_vals, lambda v: v>0.70):.1f}%)")
    print(f"    Veto    >0.90  : {sum(1 for v in vpin_vals if v>0.90):4d}  ({pct(vpin_vals, lambda v: v>0.90):.1f}%)")
else:
    print("    *** NO VPIN VALUES — not reaching ready state ***")

print("\n[3] Spread Monitor (P4: used as CE multiplier, not score)")
for st, cnt in spread_counter.most_common():
    print(f"    {st:22s}: {cnt:4d} ({100*cnt/max(eval_count,1):.1f}%)")

print("\n[4] Depth Erosion")
print(f"    HIDDEN_BUY  (+1): {sum(1 for v in depth_scores if v>0):4d}  ({pct(depth_scores, lambda v: v>0):.1f}%)")
print(f"    NEUTRAL      (0): {sum(1 for v in depth_scores if v==0):4d}")
print(f"    HIDDEN_SELL (-1): {sum(1 for v in depth_scores if v<0):4d}  ({pct(depth_scores, lambda v: v<0):.1f}%)")

print("\n[5] Spoofing (P11: accepted blind with sampled OB)")
print(f"    Active spoof evals: {spoof_active_count} of {eval_count}  (expected ~0)")

print("\n[6] Clearance (P8: all 4 traces required)")
print(f"    Score=4 ACTIVE veto  : {sum(1 for v in clearance_scores if v>=4):4d}  ({pct(clearance_scores, lambda v: v>=4):.1f}%)")
print(f"    Score=3 POSSIBLE     : {sum(1 for v in clearance_scores if v==3):4d}  ({pct(clearance_scores, lambda v: v==3):.1f}%)")
print(f"    Score=0-2 NORMAL     : {sum(1 for v in clearance_scores if v<3):4d}")

print("\n[7] Options / Regime (P5: NEUTRAL default)")
print("    Regime: NEUTRAL (multiplier x1.0) — no Deribit data yet")

print("\n[8] Market Structure — FINAL GATE")
print(f"    Trend: {ms.trend.value}  Ready: {ms.is_ready}")
print(f"    Swing highs: {len(ms._swing_highs)}  Swing lows: {len(ms._swing_lows)}")
print(f"    Signals blocked by MS this run: {ms_blocks}")

print("\n[9] Candle-based modules (Momentum / VolProfile / HTFTrend)")
print(f"    Momentum  ready: {momentum.is_ready}  score: {momentum.score:+.4f}")
print(f"    VolProfile ready: {vol_prof.is_ready}  score: {vol_prof.score:+.4f}")
print(f"    HTFTrend  ready: {htf.is_ready}  score: {htf.score:+.4f}")

print("\n[CE] Confirmation Engine Score Distribution")
if ce_scores:
    print(f"    Range [{min(ce_scores):+.4f}, {max(ce_scores):+.4f}]  Median {median(ce_scores):+.4f}")
print(f"    LONG  (>+0.20) : {sum(1 for v in ce_scores if v>0.20):4d}  ({pct(ce_scores, lambda v: v>0.20):.1f}%)")
print(f"    NEUTRAL         : {sum(1 for v in ce_scores if -0.20<=v<=0.20):4d}  ({pct(ce_scores, lambda v: -0.20<=v<=0.20):.1f}%)")
print(f"    SHORT (<-0.20) : {sum(1 for v in ce_scores if v<-0.20):4d}  ({pct(ce_scores, lambda v: v<-0.20):.1f}%)")
print(f"    CE Actions: {dict(ce_actions)}")
print(f"    Veto reasons: {dict(veto_reasons)}")

print("\n" + SEP)
