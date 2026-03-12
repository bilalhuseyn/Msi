# OFI Pro — Known Problems & Research Document

> Created: 2026-03-08  
> Purpose: Catalogue every barrier to high win rate. One section per problem.  
> Research status tracked inline.

---

## MASTER LIST

| # | Problem | Severity | Category | Status |
|---|---------|----------|----------|--------|
| P1 | VETO force-closes open positions | CRITICAL | Architecture | **DECIDED** — VETO blocks new entries only; SL manages open positions |
| P2 | VPIN bucket_size miscalibrated for real volume | CRITICAL | Calibration | **DECIDED** — Apply Option B (dynamic bucket sizing) |
| P3 | 91% SHORT bias — no trend filter in backtest | CRITICAL | Missing Feature | **DECIDED** — Replace PA Filter with Market Structure reader (CHoCH/BOS) |
| P4 | Spread score always 0 or -1 — structural negative bias | HIGH | Bug / Design | **DECIDED** — Apply Option B (spread as confidence multiplier) |
| P5 | LONG_GAMMA static regime dampens all scores by 15% | HIGH | Calibration | **DECIDED** — Apply Option B (download Deribit options data) |
| P6 | OBI loses continuous signal — discretized to {-1, 0, +1} | HIGH | Signal Design | **DECIDED** — Apply Option A (pass continuous OBI to CE) |
| P7 | OBI consistency check too strict — 90% of ticks fire NEUTRAL | HIGH | Calibration | **DECIDED** — Apply Option B + C (MA-based consistency) |
| P8 | Clearance detector too sensitive — kills positions prematurely | HIGH | Calibration | **DECIDED** — Apply Option A + B (all 4 traces + raised thresholds) |
| P9 | PA Filter not wired into backtest engine | HIGH | Missing Feature | **REDESIGN** — PA Filter SUSPENDED; replace with Market Structure module |
| P10 | ATR calculation wrong for sampled ticks — stop distances distorted | MEDIUM | Bug | **DECIDED** — Apply Option B (15-min candle ATR) |
| P11 | Spoofing detector blind with sampled OB data | MEDIUM | Architecture | **DECIDED** — Apply Option C (backtest) + Option B (live) |
| P12 | TP2 is still hardcoded 2.5R — S/R-based TP2 not active | MEDIUM | Missing Feature | **DECIDED** — Apply Option A (wire S/R-based TP2) |
| P13 | No historical Deribit options data — M7 is a static assumption | MEDIUM | Data Gap | **DECIDED** — Apply Option B (download Deribit data) + DVOL proxy |
| P14 | Sampling artifacts distort time-based modules | MEDIUM | Data/Architecture | **DECIDED** — Apply Option B (auto-scaling windows) |
| P15 | Fee drag: 0.25% round-trip costs require >60% WR at 1.5R to break even | MEDIUM | Math | **DECIDED** — Apply Option B (maker orders) + Option A (increase R:R) |

---

## P1 — VETO Force-Closes Open Positions

### What is happening
When any VETO fires (VPIN_CRITICAL, SPREAD_CRISIS, CLEARANCE_ACTIVE), the engine calls
`portfolio.force_close_all()` immediately. Every open position is liquidated at market price.

This means a position opened at tick N is force-closed seconds later when VPIN or Clearance
fires at tick N+1. No TP or SL logic ever runs.

### Evidence from backtest
```
Trade 1:  SHORT $78,622 → closed at $78,940  | +5min | reason: veto:SPREAD_CRISIS    → -$6.05
Trade 2:  SHORT $78,786 → closed at $79,056  | +8min | reason: veto:SPREAD_CRISIS    → -$5.43
Trade 3:  LONG  $78,985 → closed at $78,853  | +3.5m | reason: veto:CLEARANCE_ACTIVE → -$3.67
...
Trade 8:  SHORT $78,751 → closed at $78,689  | +3min | reason: veto:CLEARANCE_ACTIVE → -$1.21

0 of 8 trades exited via Stop Loss or Take Profit.
100% of trades force-closed by VETO.
TP1 / TP2 / TP3 / trailing stop: NEVER RAN.
```

### Why it was built this way
The PRD says "when VPIN spikes, Market Makers are withdrawing — exit immediately."
The intention is correct: if liquidity is collapsing, get out. But the implementation
force-closes on the *next tick* after entry, before the trade has any room to work.

### Research Questions & Answers
1. Should VETO block NEW entries vs. close EXISTING positions?
   > **DECIDED:** VETO must ONLY block new entries. Once a position is opened, the stop loss is the mitigation — VETO should never touch an open position.

2. Are there conditions where existing positions SHOULD be force-closed (large adverse move)?
   > **DECIDED:** Open positions can ONLY be force-closed by the Final Output of Signal Modules (e.g., a signal flip from LONG to SHORT). Signal Module output manages: (1) ongoing positions, (2) not-yet-opened positions in the setting phase. No arbitrary force-close from VETO gates.

3. What is the Deribit / professional desk convention — do MMs close positions on high VPIN?
   > **RESEARCHED:** Professional MMs do NOT instantly force-close all positions on high VPIN; they *reduce inventory selectively*, widen spreads, and provide selective liquidity — a gradual de-risking approach, not a binary kill switch (sources: VisualHFT, CEED Trading, 90th-95th percentile VPIN ~0.85 triggers spread widening, not mass liquidation).

4. Is there a VETO grace period that makes sense (e.g., force-close only if held > X min)?
   > **DECIDED:** No time-gated veto. All decisions must be based on Signal Module output (bias, price action, OB reading), not arbitrary time gates.

### Approved Solution — VETO as Entry Gate Only

**Architecture:**
- VETO checks (VPIN, Spread, Clearance) set a flag `_veto_active = True`
- Flag blocks `can_open()` → no new positions while veto is active
- Existing positions CONTINUE with their own stop/TP logic, unaffected
- Position closure only happens via: (a) Stop Loss hit, (b) Take Profit hit, (c) Signal Module output flips direction
- VPIN_CRITICAL (> 0.90) → block new entries + CONFIRMATION required (e.g., sustained > 0.90 for N ticks) before any emergency action; a single spike that immediately drops back below 0.90 should NOT trigger emergency closure
- SPREAD_CRISIS → block new entries only, keep existing positions
- CLEARANCE_ACTIVE → block new entries only, keep existing positions
- Veto deactivates when condition clears

**Key principle:** The bot is NOT an HFT bot. It analyzes on medium (15min) to long (4h) timeframes, makes few high-quality trades per day, and relies on signal quality over quantity.

---

## P2 — VPIN Bucket Size Miscalibrated

### What is happening
With `bucket_size=5.0` BTC and 43.5M trades/day on BTCUSDT futures:
- Each bucket fills in < 1 second (500 trades/second ÷ 5 BTC/trade ≈ 10 buckets/second)
- VPIN median = 0.6405 (already in warning zone on a normal day)
- VPIN > 0.85 (veto): 3,342 of 7,500 readings = **44% of the time**
- Result: system is in VETO state for nearly half of every trading day

### The math
```
BTCUSDT futures avg trade size ≈ ~0.01-0.1 BTC per trade
Daily trades ≈ 43,500,000
Daily volume ≈ 43.5M × 0.03 BTC avg ≈ 1.3M BTC/day

bucket_size = 5.0 BTC
Buckets per day = 1,300,000 / 5 = 260,000 buckets/day
= 3 buckets per second
= 1 bucket per 333ms

With window=50, VPIN reacts to the last 50/3 = 17 seconds of flow.
This is microstructure noise, not informed trading signal.
```

### Original PRD design intent
- `bucket_size = 500 BTC` → 1,300,000 / 500 = 2,600 buckets/day
- 1 bucket per ~33 seconds
- With window=50: VPIN reacts to last 50 × 33s = 27 minutes of flow
- This is the correct "informed trading" timescale

### Research Questions & Answers
1. What is the academically-validated bucket_size for BTC futures (Easley et al.)?
   > **RESEARCHED:** Easley et al. use a default of 50 volume-synchronized buckets per day; for BTCUSDT with ~1.3M BTC/day, that yields ~26,000 BTC per bucket — far larger than our 5.0 BTC setting. The academic literature warns that "small volume buckets make VPIN unstable with infrequent informed trades."

2. Should bucket_size auto-scale with rolling daily volume?
   > **DECIDED:** Yes — auto-scaling preserves signal quality across volume regimes (low-volume weekends vs high-volume liquidation events). Applying Option B.

3. What VPIN level corresponds to actual MM withdrawal on Binance futures?
   > **RESEARCHED:** For liquid contracts, the 90th-95th percentile of VPIN distributions (~0.85) correlates with MM spread widening and depth reduction; sustained readings above 0.85 trigger selective liquidity withdrawal, not instant exit.

4. Is a rolling ADV (average daily volume) normalization appropriate?
   > **DECIDED:** Yes, rolling ADV normalization is appropriate — it adapts bucket sizes to current market conditions.

### Approved Solution — Option B: Dynamic Bucket Sizing

```python
hourly_vol = sum(t["qty"] for t in last_3600s_trades)
bucket_size = hourly_vol * 0.0005  # 0.05% of hourly volume per bucket
```
Adapts automatically to volume regimes (high-volume days vs slow days).
Combined with rolling ADV normalization for consistency across sessions.

---

## P3 — 91% SHORT Bias Without Trend Filter

### What is happening
In February 2026, the bot generated 478 SHORT vs 50 LONG signals (91% short).
BTC price was $75K-$79K — a ranging/sideways market. The shorts had no macro support.
All shorts hit losses within minutes via VETO force-close.

### Root causes (cascading)
```
Why 91% short?
  ↓
1. OBI reads ask-side pressure more often (see P6, P7)
   OBI -1: 6 entries, OBI +1: 2 entries (75% bearish at entry)

2. Spread score NEVER contributes positive (see P4)
   Spread is always 0 or -1, dragging CE score down permanently

3. LONG_GAMMA x0.85 dampens marginal LONG signals below threshold (see P5)
   A borderline +0.22 LONG signal becomes +0.187 → NEUTRAL

4. No PA Filter checking EMA trend or S/R context
   Bot doesn't know BTC is in a range, not a downtrend
```

### Evidence & Root Cause Analysis

**Why was the bot 91% SHORT?** Cascading biases:
1. OBI reads ask-side pressure more often → OBI score skews bearish (75% at entry)
2. Spread score is NEVER positive (P4) → permanently drags CE negative → favors SHORT
3. LONG_GAMMA x0.85 kills marginal LONG signals below threshold (P5) → asymmetric filter
4. No trend context → bot doesn't know BTC is in a range, not a downtrend

**Why didn't VETO filter before opening?** Because VETO fires AFTER the CE makes its decision.
The CE says "SHORT", the position opens, then on the NEXT tick VETO fires (Clearance/Spread)
and force-closes the position. VETO was designed as a safety net, not a pre-entry filter.
With P1 fix (VETO = entry gate only), this specific problem is resolved.

**PA Filter (pa_filter.py) assessment:**
The existing PA Filter uses only indicator-based analysis: EMA(8)/EMA(21) crossover,
candlestick patterns (pin bars, engulfing), and pivot-based S/R. These indicator-only
approaches will likely DECREASE win rate — they lag price and generate false signals.

**DECISION: PA Filter is SUSPENDED. Replace with Market Structure reader.**

### Research Questions & Answers
1. At what phase should the trend/structure filter be applied?
   > **DECIDED:** After all signal modules produce their output and CE calculates its score, but BEFORE opening a position. It is the last layer of decision-making.

2. Should the filter block trades or re-weight signals?
   > **DECIDED:** If the signal does not pass the structure filter, it must be BLOCKED entirely. No re-weighting — the whole point is quality over quantity.

3. Can PA Filter data be generated from Tardis OB data?
   > **ANSWERED:** We need raw price data for Market Structure analysis, not OB data. Binance public API provides raw kline/candle data for free (historical + real-time). Tardis trade data can also be used to reconstruct candles, but Binance klines are simpler and more reliable.

4. What minimum candle history does the filter need?
   > **ANSWERED:** Depends on timeframe: for 15min candles, ~50-100 candles (12-24h) to establish swing structure; for 4h candles, ~50 candles (8 days) to see higher-timeframe structure.

### Approved Solution — Replace PA Filter with Market Structure Reader

**What we need instead of PA Filter (indicator-based):**
A **Market Structure module** that evaluates:
- **CHoCH (Change of Character):** Detects potential trend reversals when price breaks a prior swing low (in uptrend) or swing high (in downtrend) — 2 consecutive candle closes beyond the level
- **BOS (Break of Structure):** Detects trend continuation when price closes beyond a recent swing high (bullish) or swing low (bearish) in the prevailing trend direction
- **Swing Highs/Lows:** Fractal-based pivot identification as anchor points for structure
- **Trend Classification:** Based on sequence of higher highs/higher lows (bullish) or lower highs/lower lows (bearish) or range (neither)
- **Multi-timeframe:** Analyze on both 15min and 4h timeframes simultaneously

**Architecture:**
```
Market Data (Binance klines) → Market Structure Reader
                                    ├─ Swing point detection (fractal pivots)
                                    ├─ BOS detection (trend continuation)
                                    ├─ CHoCH detection (trend reversal)
                                    └─ Trend classification (bullish / bearish / range)
                                           ↓
                        Final gate: CE decision must ALIGN with structure
                        - CE says LONG + structure = bullish/range → ALLOW
                        - CE says LONG + structure = bearish → BLOCK
                        - CE says SHORT + structure = bearish/range → ALLOW
                        - CE says SHORT + structure = bullish → BLOCK
```

**Data source:** Binance public REST API provides free historical kline data:
```
GET /fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=1500
GET /fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit=500
```
No API key required. This gives us reliable candle data without reconstructing from ticks.

**Note:** PA Filter (pa_filter.py) remains in codebase but is NOT called. It will be
fully replaced once Market Structure module is built and tested.

---

## P4 — Spread Score Creates Permanent Negative Bias

### What is happening
The `SpreadMonitor._score()` method returns:
```python
def _score(self, ratio: float) -> int:
    if ratio < 1.20:   return 0   # MM_ACTIVE — no contribution
    if ratio < 1.80:   return 0   # MM_CAUTIOUS — no contribution  
    if ratio < 3.00:   return -1  # MM_THINNING — negative contribution
    return -1                      # MM_WITHDRAWN — negative + VETO
```
**Spread can ONLY be 0 or -1. It never contributes +1 (bullish).**

With weight 0.20 (the highest in the matrix), spread drags the CE score negative
on every tick where MM is even slightly cautious. On normal market days, spread
occasionally widens to cautious/thinning levels during volatile minutes.

### The math
```
Maximum possible CE score (all modules at +1, before spread):
  OBI(0.18) + DEPTH(0.18) + SPOOF(0.16) + CLEARANCE(0.14) + OPTIONS(0.14) = 0.80
  SPREAD = 0 at best → total max = 0.80

But the threshold for LONG is 0.35. So the max theoretical headroom is 0.80.

When spread goes to -1 (MM_THINNING):
  Max CE = 0.80 - 0.20 = 0.60  ← still enough for LONG but score is dragged

In practice, other modules are mostly 0, so CE is near 0.
If spread=-1, CE becomes -0.20, which is already SHORT territory.
```

### Why it was designed this way
The PRD says "spread widening = MM stepping back = bearish for signal quality."
This is correct — but the encoding is asymmetric. Tight spread should mean
"MM fully engaged, high confidence → BULLISH signal for CE" not just "no penalty."

### Research Questions & Answers
1. Does a very tight spread (MM_ACTIVE) actually predict up-moves? Or just confidence?
   > **RESEARCHED:** Tight spread does NOT directly predict price direction; it signals MM confidence and low adverse selection risk. Research shows "micro-price" (order book adjusted mid) predicts short-term direction, but spread itself is a liquidity/confidence metric, not directional.

2. Should tight spread boost all signals (multiplier) rather than add a directional score?
   > **RESEARCHED:** Yes — spread as a confidence multiplier is more appropriate than a directional score. Spread widening/tightening reflects market-maker risk perception, not price direction.

3. Is the spread module supposed to be a pure veto mechanism (no directional signal at all)?
   > **DECIDED:** Absolutely not a veto mechanism. Spread module should listen to other signals and modify confidence level. It should not generate directional votes or vetoes.

### Approved Solution — Option B: Spread as Confidence Multiplier

- Remove spread from the CE weighted score entirely
- Spread ratio determines a multiplier for the final CE score:
  - MM_ACTIVE → multiply by 1.0 (no effect)
  - MM_CAUTIOUS → multiply by 0.7 (reduce confidence)
  - MM_THINNING → multiply by 0.3 (near-zero confidence)
  - MM_WITHDRAWN → VETO — block new entries only (per P1 decision)

---

## P5 — LONG_GAMMA Static Assumption Suppresses LONG Signals

### What is happening
Without real Deribit options data, the backtest uses a hardcoded regime:
`options_regime = "LONG_GAMMA"` → multiplier = x0.85

This dampens ALL CE scores by 15% every single tick:
```
Raw CE score = +0.235  → after x0.85 = +0.1998 → NEUTRAL (threshold is 0.20)
Raw CE score = +0.240  → after x0.85 = +0.2040 → LONG (barely)

Raw CE score = -0.235  → after x0.85 = -0.1998 → NEUTRAL  
Raw CE score = -0.240  → after x0.85 = -0.2040 → SHORT (barely)
```

Because SHORT signals from OBI/Clearance/Spread tend to be stronger than LONG signals
(due to the structural bias from P4), the dampening asymmetrically kills LONG signals
while SHORT signals survive. This deepens the 91% SHORT bias.

### Research Questions & Answers
1. What is the actual GEX regime for BTC futures during Apr 2025 - Mar 2026?
   > **RESEARCHED:** GEX flips frequently at specific price levels ("gamma flip levels"); GammaFlip.io provides full GEX replay hour-by-hour across any historical date showing how dealer positioning shifts dynamically with price movement.

2. How frequently does regime flip between LONG and SHORT GAMMA?
   > **RESEARCHED:** GEX regime can flip multiple times per day at "zero-gamma crossover" price levels; it is NOT a static daily or weekly state — it changes dynamically as BTC price moves through key strike concentrations.

3. Is there a free data source for historical BTC options GEX (Deribit public API)?
   > **RESEARCHED:** Yes — Deribit public REST API provides book_summary with open interest, gamma, delta per strike (no auth required); GitHub tools exist (huenique/deribit-historical-options-data, bottama/Deribit-Option-Data) for downloading and storing this data in SQLite; Tardis.dev also has comprehensive tick-level Deribit options data.

4. Should the backtest default be NEUTRAL (x1.0) until real data is available?
   > **DECIDED:** Set NEUTRAL as interim default while downloading Deribit data. Once data is acquired, use real GEX values.

### Approved Solution — Option B: Download Historical Deribit Options Data

Deribit provides free public REST API for options data:
```
GET /api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option
GET /api/v2/public/get_historical_volatility?currency=BTC
```
- Reconstruct approximate GEX per day from open interest + delta data
- Use DVOL as supplementary proxy (DVOL < 50 → LONG_GAMMA, 50-70 → NEUTRAL, > 70 → SHORT_GAMMA)
- Set NEUTRAL (x1.0) as default until Deribit data is downloaded and processed
- GitHub tool (bottama/Deribit-Option-Data) can automate collection into SQLite

---

## P6 — OBI Loses Continuous Signal Information

### What is happening
OBI calculates a raw float (bid_vol / total_vol) from 0.0 to 1.0.
Then it classifies: raw > 0.65 → BULLISH (+1), raw < 0.35 → BEARISH (-1), else → 0.

The CE receives `obi_out.score` which is **{-1, 0, +1}** — an integer.

Information lost:
```
OBI raw = 0.72 (strong bullish)  → score = +1 → CE contribution = +0.18
OBI raw = 0.66 (marginal bullish) → score = +1 → CE contribution = +0.18
OBI raw = 0.90 (dominant bullish) → score = +1 → CE contribution = +0.18
```
A board-clearing institutional bid (OBI=0.90) has identical CE contribution
to a barely-bullish reading (OBI=0.66). All signal gradient is lost.

Additionally, the MA-smoothed OBI value is never passed to the CE — only the
final integer classification.

### Research Questions & Answers
1. What is the information-theoretic loss from this discretization?
   > **RESEARCHED:** Raw OBI computed on trade events shows "stronger causal alignment with future price movements" than filtered/discretized versions; discretizing to {-1, 0, +1} loses the gradient that separates a marginal 0.66 reading from a dominant 0.90 reading — both map to identical +1.

2. Does a continuous OBI score improve CE decision quality?
   > **RESEARCHED:** Yes — deep learning research on order flow imbalance (Kolm 2023, Wiley) shows continuous OBI outperforms discrete scoring at multiple prediction horizons; the effective forecast horizon scales with signal granularity.

3. How should OBI confidence (raw distance from 0.5) be encoded in the CE?
   > **RESEARCHED:** Linear scaling `(obi_raw - 0.5) * 2` maps to [-1, +1] range, preserving full gradient; stronger imbalances get proportionally higher CE contribution.

### Approved Solution — Option A: Pass Continuous OBI to CE

```python
obi_contribution = (obi_raw_value - 0.5) * 2 * weights["OBI"]
# 0.72 raw → (0.72-0.5)*2 = +0.44 → CE gets +0.44 * 0.18 = +0.079
# 0.90 raw → (0.90-0.5)*2 = +0.80 → CE gets +0.80 * 0.18 = +0.144
```
Preserves full gradient — strong signals contribute more than weak ones.

---

## P7 — OBI Consistency Check Too Strict

### What is happening
`_is_consistent()` requires the last `consistency_window=2` raw OBI values to BOTH
exceed the threshold (0.55 or 0.45 in backtest tuning). With an MA window of 10,
the OBI-MA barely crosses the threshold and then retreats.

In the Feb 2026 diagnostic:
- OBI fires (score != 0) on 33.8% of ticks
- But only 10% of ticks generate a non-neutral CE signal
- Meaning the consistency check is filtering out ~23% of valid OBI signals

The consistency check uses the **raw OBI history** (not the MA) with the same
classification thresholds as the signal itself. So it's double-filtering.

### Research Questions & Answers
1. Is a 2-period consistency window appropriate for sampled data (100-second gaps)?
   > With continuous OBI (P6 fix), consistency matters less but still useful for noise filtering.

2. Should consistency use the MA or the raw OBI values?
   > **DECIDED:** Use MA values — MA is already smoothed, checking raw values double-filters.

3. Is there a less strict consistency check that still filters noise?
   > **DECIDED:** Use MA sustained above threshold for N ticks (Option B), and if MA is sustained, allow even if individual raw values dip (Option C fallback).

### Approved Solution — Option B + C: MA-Based Consistency with Sustained Check

```python
def _is_consistent(self) -> bool:
    # Option B: Use MA values for consistency, not raw
    # Option C fallback: If MA sustained on same side for last N ticks, allow signal
    if obi_ma > self._bull_thresh:
        last_ma = sum(list(self._history)[-(self._ma_win+1):-1]) / self._ma_win
        return last_ma > self._bull_thresh  # MA was also bullish last tick
    if obi_ma < self._bear_thresh:
        last_ma = sum(list(self._history)[-(self._ma_win+1):-1]) / self._ma_win
        return last_ma < self._bear_thresh
    return False
```

---

## P8 — Clearance Detector Too Sensitive

### What is happening
The Clearance Detector triggers CLEARANCE_ACTIVE (score ≥ 3) when any 3 of 4 traces fire:

**Trace 1 (ONE_SIDED):** >72% buy or sell volume in last 100 trades.
- With 26 trades/tick matched to real data, the 100-trade window spans ~4 ticks.
- Normal market microbursts (momentum) easily exceed 72% for short periods.

**Trace 2 (ASK_SLIDING):** Best ask drops > 0.08% over 5 OB history snapshots.
- With sample_every=100, 5 OB snapshots = ~500 seconds of elapsed time.
- Normal price moves of 0.08% happen frequently in 8 minutes.
- This trace fires on NORMAL TRENDING BEHAVIOR, not MM dumping.

**Trace 3 (LARGE_SELL_CLUSTER):** 3+ sells in last 20 trades > 4× average size.
- In a real Binance data stream, large block trades happen frequently.
- The 4× multiplier is not high enough to be selective.

**Trace 4 (BID_THINNING):** Bid depth drops > 45% over 5 snapshots.
- BTC order book refreshes constantly. Bid depth fluctuates 30-60% routinely.

### Evidence
- 242 CLEARANCE_ACTIVE vetoes in 13,870 ticks (1.7% of ticks)
- 6 of 8 executed trades force-closed by CLEARANCE_ACTIVE

### Research Questions & Answers
1. What percentage of CLEARANCE_ACTIVE fires are true MM dumping vs. false positives?
   > Evidence: 242 fires in 13,870 ticks (1.7%) with 6/8 trades killed — almost certainly majority false positives given BTC's normal volatility.

2. Should all 4 traces be required (not just 3)?
   > **DECIDED:** Yes — genuine MM dumping shows all 4 traces simultaneously.

3. What thresholds are appropriate for BTCUSDT futures specifically?
   > **DECIDED:** Raise all thresholds significantly (see below).

4. Should traces have time-decay (a stale ONE_SIDED reading should count less)?
   > Useful but secondary to the threshold/count fix.

### Approved Solution — Option A + B: All 4 Traces Required + Raised Thresholds

**APPROVED for implementation:**
```python
if score >= 4:    status = CLEARANCE_ACTIVE
elif score >= 3:  status = CLEARANCE_POSSIBLE  # warning only, NOT a veto

one_sided_threshold = 0.85     # was 0.72
ask_slide_pct = 0.003          # was 0.0008 (0.3% slide, not 0.08%)
large_trade_multiplier = 8.0   # was 4.0
bid_thin_pct = 0.40            # was 0.55 (60% depth loss, not 45%)
```

---

## P9 — PA Filter SUSPENDED → Replace with Market Structure Reader

### What is happening
The Price Action Filter (`signals/pa_filter.py`) was completed in Phase 5 with indicator-based
analysis only (EMA crossover, candlestick patterns, S/R pivots). It was never wired into
`sim_engine.py`, so every trade opens without any trend/structure confirmation.

### DECISION: PA Filter SUSPENDED

PA Filter uses only lagging indicators (EMA crossover) and pattern recognition (pin bar, engulfing).
These indicator-only approaches would decrease win rate — they lag price and generate false signals.

**Replacement: Market Structure Reader module** (see P3 for full specification).

The Market Structure reader will detect:
- CHoCH (Change of Character) — trend reversal signals
- BOS (Break of Structure) — trend continuation signals
- Swing Highs/Lows via fractal pivot detection
- Trend classification (bullish / bearish / range)
- Multi-timeframe analysis (15min + 4h)

### What needs to happen
1. Download historical kline data from Binance (15min + 4h candles) — free, no API key
2. Build Market Structure reader module (`signals/market_structure.py`)
3. Wire Market Structure as FINAL GATE in sim_engine.py (after CE, before position open)
4. Use S/R from structure analysis for dynamic TP2 (replacing hardcoded 2.5R)

### Approved Solution — All three options combined where applicable
- **EMA trend** (from Option A) can serve as a quick interim filter while Market Structure is built
- **Candle reconstruction** (from Option B) needed for Market Structure analysis — use Binance klines
- **S/R-based TP2** (from Option C) will come from Market Structure swing points

**PA Filter remains in codebase but is NOT imported or called.**

---

## P10 — ATR Calculation Distorted for Sampled Ticks

### What is happening
In `sim_engine.py`, ATR is calculated as:
```python
atr_window.append(abs(mid - prev_price))
```
For sampled OB data (every 100th snapshot = ~100-second gaps), the price difference
between consecutive ticks is the total drift over 100 seconds, not the intrabar range.

**Actual values:**
- Sampled tick gap: ~100 seconds
- BTC price drift over 100s at $80K: typically $5-50 (0.006%-0.06%)
- Calculated ATR: $5-50
- min_stop_pct override: 0.5% of $80K = $400

Since ATR is far smaller than min_stop_pct, the stop is always determined by:
`stop_dist = mid * 0.005 = $400`

This is correct behavior (the min_stop floor kicks in), but the ATR used for TP3
trailing stop is wrong — it's tiny, so the trailing stop hugs price too tight.

**True ATR for BTC** (from 15-min candles): typically $200-$800.

### Possible Solutions
**Option A — Track true candle range instead of tick-to-tick**
```python
candle_high = max(recent_prices[-N:])
candle_low = min(recent_prices[-N:])
atr_estimate = (candle_high - candle_low)
```

**Option B — Reconstruct 15-min candles from Binance klines (APPROVED)**
Same candles used for Market Structure reader also provide true ATR.
`atr = mean(high - low for each 15-min candle, last 14 candles)`

**Option C — Use % ATR**
`atr_pct = mid * 0.005` (0.5%). Already effectively what happens via min_stop_pct.

### Approved Solution — Option B
Use 15-min candle ATR from Binance kline data. Same data feed as Market Structure module — shared infrastructure.

---

## P11 — Spoofing Detector Blind with Sampled OB Data

### What is happening
The Spoofing Detector tracks large orders (≥ 50 BTC) and flags cancellations within
800ms as spoofs. This requires **consecutive OB snapshots < 800ms apart**.

With `sample_every=100`, consecutive sampled OB snapshots are ~100 seconds apart.
A spoof order placed and cancelled within 800ms spans a single tick gap of 100 seconds.
The detector sees: "order was there at t=0, not there at t=100s → cancelled within 800ms?
No, elapsed = 100,000ms > 800ms → not a spoof."

Result: **0 spoofs detected in all backtest runs.**

This is not a false negative — it is structural blindness due to sampling.

### Impact
With Spoof score always 0:
- Spoof→OBI interaction never fires (never reverses a bullish OBI to bearish)
- Spoof weight (0.16) contributes 0 to every CE score
- CE is missing 16% of its potential signal capacity

### Approved Solution — Option C (backtest) + Option B (live)

**Backtest:** Accept that Spoofing Detector requires live tick-level data (< 1s).
Set spoof score = 0 and redistribute CE weights:
```python
# Backtest CE weights (no spoof, spread removed as multiplier per P4):
OBI: 0.26, DEPTH: 0.26, CLEARANCE: 0.24, OPTIONS: 0.24
```
Note: Spread removed from weighted score (now a confidence multiplier per P4 fix).

**Live trading:** Use Option B (order size change detection across snapshots).

---

## P12 — TP2 Still Hardcoded at 2.5R

### What is happening
`BacktestSettings.tp2_rr = 2.5` — a comment says "Phase 5 replaces with PA Filter S/R."
This was never implemented. 30% of the position is exited at a fixed 2.5× stop distance
regardless of where actual support/resistance levels are.

Impact:
- TP2 may be placed in the middle of a strong S/R cluster (gets rejected before hitting)
- TP2 may be placed way past any meaningful S/R level (never gets hit)
- The PA Filter's S/R detection (`find_tp2_level()`) exists and is tested but unused

### Approved Solution — Option A: Wire S/R-based TP2

Once Market Structure module (P9) is built, use its swing point detection for S/R-based TP2:
```python
tp2_from_sr = market_structure.find_next_sr_level(entry_price, direction)
tp2_price = tp2_from_sr if tp2_from_sr else entry_price + direction * stop_dist * 2.5
```
Fallback to hardcoded 2.5R if no S/R level is found. Dynamic R:R from Option B can
be added as an additional refinement based on ATR regime.

---

## P13 — No Historical Deribit Options Data

### What is happening
M7 (Options Layer) is designed to compute live GEX and PCR from Deribit options chain.
In backtest, `options_chain = []` on every tick → Options Layer returns score=0, regime=None.
The backtest falls back to `BacktestSettings.options_regime = "LONG_GAMMA"` (hardcoded).

This creates P5 (dampening) AND means the 14% weight for Options is permanently wasted.

### Approved Solution — Option B: Download Historical Deribit Options Data

Deribit provides free public REST API:
```
GET /api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option
GET /api/v2/public/get_historical_volatility?currency=BTC
```
- Download historical options data using GitHub tool (bottama/Deribit-Option-Data)
- Reconstruct daily GEX from open interest + delta per strike
- Use DVOL as supplementary regime proxy
- Start `DeribitPoller` (scripts/record_data.py) for ongoing live collection
- Run sensitivity analysis across all 3 regimes in backtests

---

## P14 — Sampling Artifacts Distort Time-Based Modules

### What is happening
Taking every 100th OB snapshot creates an irregular time series where consecutive
ticks are ~100 seconds apart. Several modules assume more frequent updates:

| Module | Designed for | With sampling |
|--------|-------------|---------------|
| Depth Erosion | 60s check interval | May check every 100s or skip |
| Clearance Detector | OB history of 5 consecutive snapshots | 5 snapshots = 500s = 8.3 minutes |
| VPIN | Sub-second bucket fills | Works correctly (uses all trades) |
| Spread | Baseline window of 1440 samples | 1440 × 100s = 40 hours of data |
| OBI MA | 10-period MA | 10 ticks × 100s = 16.7 minutes |

The Spread baseline window of 1440 samples at 100s spacing means it doesn't establish
a proper baseline until 40 hours of sampled data are processed. For single-day analysis,
it never stabilizes.

### Approved Solution — Option B: Auto-Scaling Windows

```python
spread_baseline_window = max(10, 86400 // sample_interval)  # 864 samples = 24h
obi_ma_window = max(5, 600 // sample_interval)               # 6 samples = 10 min
clearance_ob_history = max(3, 300 // sample_interval)        # 3 snapshots
```
Auto-scales all time-dependent windows based on the actual sample interval.
Most practical fix without changing the fundamental architecture.

---

## P15 — Fee Drag Requires High Win Rate Just to Break Even

### What is happening
Round-trip trading cost on BTCUSDT futures:
```
Entry fee:   0.10% of notional
Exit fee:    0.10% of notional
Avg slippage: ~0.025% per side = 0.05% round trip
Total cost:  0.25% round trip

On $1,000 position (typical): $2.50 per round trip
```

Break-even win rate at 1.5R reward (TP1):
```
Let W = win rate, L = 1 - W
Win profit = (stop_dist × 1.5 × size) - fees
Loss cost  = (stop_dist × 1.0 × size) + fees

Break-even: W × profit = (1-W) × cost
W × (1.5R × size - $2.50) = (1-W) × (1.0R × size + $2.50)

For a typical trade (R = $5.00, size ≈ 0.0125 BTC):
W × ($7.50 - $2.50) = (1-W) × ($5.00 + $2.50)
W × $5.00 = (1-W) × $7.50
5W = 7.5 - 7.5W
12.5W = 7.5
W = 60%
```

**The system needs a 60% win rate just to break even on TP1 exits.**

Current win rate: 0-12%. We are nowhere near break-even.

### Approved Solution — Option B (maker orders) + Option A (increase R:R)

**Live trading:** Use limit orders (maker fee 0.02% vs 0.10% taker).
Round-trip fee drops from 0.25% to 0.04%.

**R:R adjustment:** Increase TP1 R:R to 2.0 (from 1.5).
Break-even WR at 2.0R with maker fees: ~40% — very achievable.

**Backtest:** Simulate with maker fee rates (0.02%) for realistic live projections.

---

## PRIORITY ORDER FOR FIXES (UPDATED WITH DECISIONS)

```
Phase 1 — Immediate (fix the core architecture):
  P1  — VETO: entry gate only, never touch open positions            [DECIDED]
  P2  — VPIN: dynamic bucket sizing (Option B)                       [DECIDED]
  P5  — Regime: set NEUTRAL default, download Deribit data           [DECIDED]
  P4  — Spread: confidence multiplier, not weighted score            [DECIDED]

Phase 2 — High impact signal fixes:
  P6  — OBI: pass continuous score to CE                             [DECIDED]
  P7  — OBI consistency: MA-based (Option B+C)                       [DECIDED]
  P8  — Clearance: all 4 traces + raised thresholds                  [DECIDED]
  P11 — Spoofing: redistribute CE weights for backtest (spoof=0)     [DECIDED]
  P14 — Sampling: auto-scaling windows                               [DECIDED]

Phase 3 — Market Structure integration (replaces PA Filter):
  P3  — Build Market Structure reader (CHoCH, BOS, swing points)     [DECIDED]
  P9  — Wire Market Structure as final gate in sim_engine.py         [DECIDED]
  P10 — Fix ATR using 15-min Binance klines                          [DECIDED]
  P12 — S/R-based TP2 from Market Structure swing points             [DECIDED]

Phase 4 — Data and economics:
  P13 — Download Deribit historical options data                     [DECIDED]
  P15 — Maker orders + increase TP1 R:R to 2.0                      [DECIDED]
```

### KEY ARCHITECTURE PRINCIPLES (from user review)
1. **NOT an HFT bot.** Focus on quality over quantity. Few trades/day is fine.
2. **Medium-to-long timeframe analysis:** 15min to 4h candles.
3. **Signal quality is everything:** Filter noise aggressively, wait for exact setups.
4. **VETO = entry gate only.** Stop loss manages open positions, not VETO.
5. **Market Structure > Indicators.** CHoCH/BOS/swing analysis, not EMA/pattern recognition.
6. **Spread = confidence modifier**, not directional signal.
7. **Signal Modules manage positions.** Open positions closed by signal output, not arbitrary gates.

---

*This document is a living research file. All decisions marked [DECIDED] are approved for implementation.*
