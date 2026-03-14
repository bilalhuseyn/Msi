# OFI Pro — Project Memory

> Bookmark file for AI context continuity across sessions.
> Last updated: 2026-03-14 (P3/P9 MarketStructureReader → backtest bağlandı)

---

## What Is OFI Pro?

OFI Pro (Order Flow Intelligence) is an **algorithmic trading bot** for crypto markets
(BTC/USDT, ETH/USDT) that generates **low-frequency, high-quality** trading signals by
reading Market Maker behaviour, institutional order flow, and options gamma positioning.

- **Markets:** Binance, Bybit (spot/perp), Deribit (options data only)
- **Position hold time:** 15 min to 4 hours
- **Philosophy:** Single signal = noise. Multiple independent signals aligning = opportunity.
- **Core principle:** The system does NOT predict price. It detects structural footprints
  left by Market Makers, institutional funds, and spoofers, then trades when multiple
  modules confirm simultaneously.

---

## Architecture — 3 Layers

```
Layer 1: MM Regime Detector
  └── Deribit REST API (5min) → GEX/PCR/IV → Long/Short Gamma regime
  └── Modifies ALL signal weights via multiplier

Layer 2: Order Flow Engine (7 modules → Confirmation Engine → Market Structure)
  └── Binance/Bybit WebSocket (100ms) feeds into:
      M1: OBI          — Order Book Imbalance
      M2: VPIN         — Flow Toxicity (informed trading probability)
      M3: Spread Mon   — MM activity via bid-ask spread (MULTIPLIER only, not score)
      M4: Depth Erosion — Hidden institutional pressure
      M5: Spoof Det    — Manipulation detection
      M6: Clearance    — MM inventory dumping
      M7: Options      — GEX/PCR regime (Layer 1)
  └── Confirmation Engine aggregates → LONG/SHORT/NEUTRAL/VETO
  └── MarketStructureReader — FINAL GATE (CHoCH/BOS/trend filter)

Layer 3: Execution
  └── Position Manager, Dynamic Hedger, Paper Trader
```

---

## Completed Phases

### Phase 0 — Infrastructure (DONE)
- WebSocket feed managers: Binance (depth20@100ms, aggTrade, bookTicker),
  Bybit (orderbook.200, publicTrade), Deribit REST poller (5min options chain)
- Abstract base feed with exponential backoff reconnect (1s→60s max)
- Async event bus (asyncio.Queue) decoupling feeds from signal modules
- Data normalizer: UTC sync, price outlier filter (>5% = outlier), 24h rolling baselines
- InfluxDB 2.x async batched writer (100-point batches, 5s flush)
- SQLite store: trades, signal_logs, daily_stats tables
- Health monitor with component-level status + console dashboard
- Structured JSON logging (file + console)
- Docker + docker-compose (InfluxDB + app)

### Phase 1 — Core Signal Modules (DONE)
- **M1: OBI** — bid_vol/(bid_vol+ask_vol), 10-period MA, consistency check, continuous float gradient (P6 fix)
- **M2: VPIN** — dynamic bucket sizing (hourly_vol * 0.0005), 50-bucket rolling window, GLOBAL VETO >0.90 for 3 ticks
- **M3: Spread Monitor** — 24h baseline, MM_ACTIVE/CAUTIOUS/THINNING/WITHDRAWN. Acts as CE score MULTIPLIER only (P4 fix), not weighted score. MW_WITHDRAWN = VETO.
- **M7: Options Layer** (early inclusion) — GEX calc, PCR calc, regime detection, flip risk,
  multipliers: SHORT_GAMMA x1.20, LONG_GAMMA x0.85, GEX_FLIP x0.60

### Phase 2 — Advanced Signal Modules (DONE)
- **M4: Depth Erosion** — 60s periodic OB depth comparison, HIDDEN_BUY/HIDDEN_SELL when
  price stays stable but one side erodes >35%. Suppressed when spoof is active.
- **M5: Spoofing Detector** — Tracks large orders (>50 BTC), flags cancellation within
  800ms as spoof. BID spoof → SELL intent, ASK spoof → BUY intent. Consecutive tracking.
- **M6: Clearance Detector** — 4-trace algorithm (P8 fix — all 4 required, not 3):
  (1) One-sided trade flow >85% (was 72%)
  (2) Best ask sliding down >0.3% (was 0.08%)
  (3) Large sell cluster >8x avg (was 4x)
  (4) Bid-side thinning >60% loss (was 45%)
  Score = 4 → CLEARANCE_ACTIVE → GLOBAL VETO
  Score = 3 → CLEARANCE_POSSIBLE (warning only)

### Phase 3 — Confirmation Engine (DONE)
- VETO gates: VPIN >0.90 for 3 consecutive ticks (VPIN_CRITICAL), MM_WITHDRAWN (SPREAD_CRISIS), CLEARANCE_ACTIVE
- **VETO = entry gate only (P1 fix).** Open positions are NEVER touched by VETO. Stop loss manages open positions.
- Spread = confidence multiplier (P4 fix): ACTIVE 1.0, CAUTIOUS 0.7, THINNING 0.3, WITHDRAWN = VETO
- Spoof→OBI interaction: BID spoof reverses BULLISH OBI; 3+ spoofs zeros OBI
- Regime modifier applied to weighted score
- VPIN warning band (0.70-0.90) halves signal strength (was 0.55-0.65 → raised per P2 calibration)
- CE weights (backtest, no spoof since P11):
  OBI 0.26, DEPTH 0.26, CLEARANCE 0.24, OPTIONS 0.24
- Decision thresholds: score >=+0.35 → LONG, <=-0.35 → SHORT, else NEUTRAL
- OBI passes continuous gradient (obi_ma float), not discretized {-1,0,+1} (P6 fix)
- OBI consistency: MA-based sustained check, not raw double-filter (P7 fix)

### Risk Management Enhancements (DONE, beyond PRD)
- Drawdown-responsive scaling: 4 progressive tiers (1%→0.3% risk) instead of binary halt
- Circuit breaker: 3 losses → 50% size for 2 trades; 5 losses → 1h cooldown + 25% size
- Cross-asset correlation tracker: rolling Pearson, cap at 1.5x combined when r>0.85
- Funding rate awareness: annualized rate tracking, expensive hedge flagging
- Portfolio VaR: ATR-based 95% confidence, blocks new positions if VaR >2%
- Half-Kelly position sizer: caps size at 50% Kelly fraction

### Phase 4 — Backtest Framework (DONE)
- **BacktestEngine** — replays historical data through all 7 signal modules + CE + MarketStructureReader
- **DataLoader** — loads from CSV (klines, trades, orderbook snapshots). SyntheticGenerator for testing.
- **FeeModel** — 0.1% taker fee per trade, ±0.05% slippage (configurable, randomizable)
- **Portfolio** — up to 2 concurrent positions, cooldown, daily trade limits (8/day), max hold 4h.
  Partial exits: TP1 (50% at 1.5R), TP2 (30% at next S/R level), TP3 (trailing ATR×1.5). Break-even at 1R.
- **Metrics** — Win Rate, Gross P/L, Net PnL, Profit Factor, Avg R:R, Max DD (USD+%), Sharpe, Sortino, Calmar
- **WalkForwardSplitter** — chronological 70/30 train/OOS split. Robustness: WR<-30%, Sharpe<-50%, PF<-40% = fail
- **MonteCarloSimulator** — 1,000 iterations shuffling trade PnL order. 95th/99th pct DD, ruin probability
- **GridSearchOptimizer** — full grid or random search; objective: sharpe, profit_factor, or composite
- **BacktestReport** — text + JSON output with PRD target pass/fail, Monte Carlo, walk-forward

### Phase 4.5 — Data Infrastructure & Real-Data Backtest (DONE)
- **DataRecorder** — hooks into EventBus, writes OB snapshots/trades/tickers/options to daily CSV
- **BinanceDownloader** — data.binance.vision (aggTrades + klines), ZIP → CSV, skip existing
- **Data Converter** (`data/converter.py`):
  - `trades_to_ticks()` — aggTrades → BacktestTick (synthetic OB from VWAP + flow imbalance)
  - `merge_ob_and_trades()` — recorded OB + trade data highest-fidelity merge
  - `tardis_ob_to_ticks()` — Tardis book_snapshot_25 .csv.gz → BacktestTick
  - `load_klines_as_candles()` — **NEW (P3/P9, 2026-03-14)** kline CSV → list[Candle] for MS warm-up; auto-detects ms/s timestamps, aggregates any source interval to 15m
- **Tardis.dev:** 12 months free data acquired (Apr 2025 - Mar 2026), 17.3M OB snapshots + 43.5M trades ~1GB
- Scripts: `download_history.py`, `record_data.py`, `run_backtest.py`, `run_backtest_ob.py`, `diagnose_signals.py`, `diagnose_real_ob.py`

### Phase 5 — Live Execution Layer (DONE)
- **Price Action Filter** (`signals/pa_filter.py`) — **SUSPENDED (P9).** Built but NOT imported/called anywhere. Replaced by MarketStructureReader.
- **Position Manager** (`execution/position_manager.py`) — Live position lifecycle
- **Dynamic Hedger** (`execution/hedger.py`) — 5-trigger hedge system (SIZE, VPIN, TIME, ADVERSE, GEX_FLIP)
- **Paper Trading Mode** (`execution/paper_trader.py`) — Full simulation on live data
- **Settings:** PAFilterSettings, HedgerSettings

### Phase 5.5 — Market Structure Integration (DONE, 2026-03-14)
This is P3 + P9 + P10 + P12 from PROBLEMS.md, all resolved together.

**`signals/market_structure.py` — MarketStructureReader:**
- Fractal pivot swing high/low detection (`swing_lookback=3`)
- Trend classification: higher highs + higher lows = BULLISH; lower lows + lower highs = BEARISH; else RANGE
- BOS (Break of Structure): close beyond last swing in trend direction
- CHoCH (Change of Character): close beyond prior swing in opposite direction
- `allows_direction(direction)` → FINAL GATE in sim_engine.py; LONG blocked when BEARISH, SHORT blocked when BULLISH
- `sr_proximity_score(price)` → CE signal `SR_PROXIMITY` ∈ [-1, +1]; near support = +, near resistance = -
- `find_next_sr_level(price, direction)` → S/R-based TP2 (P12 fix, replaces hardcoded 2.5R)
- `is_ready` → True when min_swings=4 swing highs AND 4 swing lows found
- `_estimate_atr()` → quick ATR from recent candle ranges for proximity scoring
- `build_candles_from_trades(trades_by_sec, interval_sec=900)` → OHLCV candles from raw trade dict

**`backtest/sim_engine.py` — BacktestEngine full wiring:**
- `self._ms = MarketStructureReader(swing_lookback=3, min_swings=4)` instantiated in `__init__`
- `self._candles_15m = candles_15m or []` — passed at construction
- `_warm_up_structure(first_tick_ts)` — feeds all candles that close BEFORE first tick; logs trend+ready state
- `_advance_candles(timestamp)` — called on every tick; feeds candles as they close (real-time simulation)
- Candle-based modules also fed: MomentumModule, VolumeProfileModule, HTFTrendModule
- ATR = mean candle high-low from deque(maxlen=14); fallback mid*0.5% (P10 fix)
- `SR_PROXIMITY: self._ms.sr_proximity_score(mid)` in signals dict → CE weights it at 0.15
- `_READY` dict tells CE which candle-module scores to trust
- `if not self._ms.allows_direction(direction)` → structure_blocked++, decision logged as `blocked:structure_TREND`
- `sr_tp2 = self._ms.find_next_sr_level(mid, direction)` → TP2 price (P12), fallback 2.5R
- structure_blocked % logged at run end: "Structure blocked: N of M signals (X%)"
- MS eval at 15-min boundaries (same as signal eval), not every tick
- Signal eval: every 900s candle boundary, not every tick

**`scripts/run_backtest.py` — trades-only backtest (updated 2026-03-14):**
- `--klines-dir OPTIONAL` — directory with 1m or 15m kline CSVs
  - If provided: `load_klines_as_candles(klines_dir)` → `candles_15m` → passed to BacktestEngine → MS warm-up active
  - If omitted: `candles_15m=[]` passed; MS starts cold; is_ready=False until internal candles accumulate from ticks
- Usage example:
  ```
  python -m scripts.run_backtest \
    --trades historical_data/BTCUSDT/trades \
    --klines-dir historical_data/BTCUSDT/klines/1m \
    --output reports/ms_backtest
  ```

**`scripts/run_backtest_ob.py` — OB backtest (already correct before 2026-03-14):**
- Builds candles from Tardis trades via `build_candles_from_trades(trades_by_sec)` and passes to engine

---

## PROBLEMS.md — Status Summary

All 15 problems have been decided. Implementation status:

| # | Problem | Decision | Implemented |
|---|---------|----------|-------------|
| P1 | VETO force-closes open positions | VETO = entry gate only | ✅ sim_engine.py |
| P2 | VPIN bucket_size miscalibrated | Dynamic: hourly_vol × 0.0005 | ✅ vpin.py |
| P3 | 91% SHORT bias — no trend filter | Replace PA Filter with MarketStructureReader | ✅ market_structure.py |
| P4 | Spread score always 0 or -1 | Spread = CE score multiplier only | ✅ confirmation.py |
| P5 | LONG_GAMMA static dampens all | NEUTRAL default; download Deribit data | ✅ settings default=NEUTRAL |
| P6 | OBI discretized to {-1,0,+1} | Pass continuous obi_ma float to CE | ✅ sim_engine.py:271 |
| P7 | OBI consistency too strict | MA-based sustained check | ✅ obi.py |
| P8 | Clearance too sensitive | All 4 traces required; raised thresholds | ✅ clearance_detector.py |
| P9 | PA Filter not wired into backtest | PA Filter SUSPENDED; MS is the gate | ✅ sim_engine.py:303 |
| P10 | ATR wrong for sampled ticks | ATR from 15-min candle high-low ranges | ✅ sim_engine.py:402 |
| P11 | Spoofing blind with sampled OB | Accept 0 spoof in backtest; redistribute CE weights | ✅ sim_engine.py weights |
| P12 | TP2 hardcoded 2.5R | S/R from MS swing points; fallback 2.5R | ✅ sim_engine.py:316 |
| P13 | No historical Deribit options | Download via public Deribit API | ⬜ Not started |
| P14 | Sampling artifacts distort windows | Auto-scale windows by sample_interval_sec | ✅ sim_engine.py:92 |
| P15 | Fee drag — 60% WR to break even | Maker orders + increase TP1 R:R to 2.0 | ⬜ Not started |

---

## Key Thresholds & Parameters

| Parameter | Value | Context |
|-----------|-------|---------|
| OBI bullish threshold | 0.65 | OBI-MA above this → BULLISH |
| OBI bearish threshold | 0.35 | OBI-MA below this → BEARISH |
| OBI MA window | 10 (auto-scaled with P14) | MA smoothing window |
| VPIN veto threshold | 0.90 for 3 consecutive ticks | GLOBAL VETO — VPIN_CRITICAL |
| VPIN warning band | 0.70–0.90 | Signal strength halved |
| VPIN bucket size | hourly_vol × 0.0005 | Dynamic, ~27-min window |
| Spread multiplier ACTIVE | 1.0 | Full confidence |
| Spread multiplier CAUTIOUS | 0.7 | Moderate confidence |
| Spread multiplier THINNING | 0.3 | Near-zero confidence |
| Spread MM_WITHDRAWN | VETO | Block new entries only |
| Depth erosion threshold | 35% | OB side erosion to trigger |
| Clearance one_sided_threshold | 0.85 | Was 0.72 — P8 fix |
| Clearance ask_slide_pct | 0.003 (0.3%) | Was 0.0008 — P8 fix |
| Clearance large_trade_mult | 8.0x | Was 4.0 — P8 fix |
| Clearance bid_thin_pct | 0.40 (60% depth loss) | Was 0.55 — P8 fix |
| Clearance veto score | 4 of 4 traces | Was 3 of 4 — P8 fix |
| CE LONG threshold | +0.35 | Score above → LONG |
| CE SHORT threshold | -0.35 | Score below → SHORT |
| MS swing_lookback | 3 candles | Fractal pivot detection window |
| MS min_swings | 4 | Minimum swings before is_ready |
| MS EVAL_INTERVAL | 900s (15 min) | Candle + signal evaluation |
| Risk per trade | 1% of account | Base risk (drawdown-scaled) |
| Max daily loss | 3% | System halts |
| Max daily trades | 8 | Anti-overtrading |
| Cooldown between trades | 15 minutes (trades-only: 5 min) | Anti-impulse |
| Max open positions | 2 | Concentration limit |
| TP1 | 50% exit at 1.5R | Stop moves to break-even at TP1 |
| TP2 | 30% exit at next S/R swing point | Fallback: stop_dist × 2.5R |
| TP3 | 20% trailing ATR × 1.5 | Trailing stop |
| SHORT_GAMMA multiplier | x1.20 | Amplifies breakout signals |
| LONG_GAMMA multiplier | x0.85 | Dampens breakout signals |
| GEX_FLIP multiplier | x0.60 | Conservative — volatility incoming |

---

## CE Weight Matrix (Backtest — no spoof per P11)

| Signal | Weight | Notes |
|--------|--------|-------|
| OBI | 0.26 | Continuous float gradient (P6) |
| DEPTH | 0.26 | HIDDEN_BUY/HIDDEN_SELL score |
| CLEARANCE | 0.24 | Status mapped to score |
| OPTIONS | 0.24 | GEX/PCR or static regime |
| SR_PROXIMITY | 0.15 | From MarketStructureReader swing points |
| MOMENTUM | 0.10 | RSI + EMA crossover |
| VOLUME_PROFILE | 0.10 | VPOC/VAH/VAL position |
| HTF_TREND | 0.10 | 4H ADX + DI scoring |
| SPOOF | 0.00 | Veto/OBI-flip only; no score in backtest |
| SPREAD | — | Not a score; CE score multiplier (P4) |

---

## Backtest Decision Flow (sim_engine.py)

```
Every tick:
  _advance_candles(timestamp) → feeds MS + Momentum + VolProfile + HTFTrend
  VPIN.process_trade() for each trade
  portfolio.update(mid, ts, atr) → SL/TP/trailing on open positions

Every 900s boundary (15-min candle):
  OBI.update()  VPIN.update()  Spread.update()
  Spoof.update()  Depth.update()  Clearance.update()  Options.update()
  signals = { OBI: obi_ma_float, SR_PROXIMITY: ms.sr_proximity_score, ... }
  decision = CE.evaluate(signals, vpin_val, spread_status, regime)

  if VETO → log, continue (never touch open positions)

  if LONG or SHORT:
    direction = +1 or -1
    if not ms.allows_direction(direction) → log blocked:structure_TREND, continue
    if not portfolio.can_open()           → log blocked:cooldown/limit, continue
    ATR from candle ranges → stop_dist
    tp2 = ms.find_next_sr_level() or fallback 2.5R
    portfolio.open_position(...)
```

---

## Bybit Testnet Mode (Phase 5 addition)
- `USE_TESTNET=true` in .env → Bybit testnet as primary OB source
- `core/engine.py._on_order_book()`: Bybit primary; Binance fallback if Bybit has no data
- Paper trading integrates with Bybit testnet for order book signal generation

---

## File Structure

```
OBI_Pro/
├── config/
│   ├── constants.py        # SignalDirection, SpreadStatus, VetoReason, RegimeType, DecisionAction
│   └── settings.py         # Pydantic settings for all modules (OBI/VPIN/CE/Backtest/etc.)
├── core/
│   ├── engine.py           # OFIEngine — master async loop, live mode
│   ├── events.py           # EventBus — asyncio.Queue fan-out dispatcher
│   └── health.py           # HealthMonitor — component status
├── data/
│   ├── feeds/
│   │   ├── binance_feed.py # depth20@100ms, aggTrade, bookTicker
│   │   ├── bybit_feed.py   # orderbook.200, publicTrade (testnet support)
│   │   └── deribit_feed.py # REST poller — options chain 5min
│   ├── normalizer.py
│   ├── storage.py          # InfluxWriter + SQLiteStore
│   ├── recorder.py         # Live data recorder → daily CSV
│   ├── binance_downloader.py
│   ├── tardis_downloader.py
│   └── converter.py        # trades_to_ticks, tardis_ob_to_ticks, load_klines_as_candles (NEW)
├── signals/
│   ├── market_structure.py # MarketStructureReader — CHoCH/BOS/swing points (FINAL GATE)
│   ├── obi.py              # M1: OBI (continuous float, MA-based consistency)
│   ├── vpin.py             # M2: VPIN (dynamic bucket sizing)
│   ├── spread_monitor.py   # M3: Spread Monitor (multiplier only, not CE score)
│   ├── depth_erosion.py    # M4: Depth Erosion
│   ├── spoofing_detector.py# M5: Spoofing Detector
│   ├── clearance_detector.py# M6: Clearance (all 4 traces, raised thresholds)
│   ├── options_layer.py    # M7: GEX/PCR/regime
│   ├── confirmation.py     # Confirmation Engine — weighted score + veto gates
│   ├── pa_filter.py        # SUSPENDED — not imported/called anywhere
│   ├── htf_trend.py        # 4H ADX + DI scoring
│   ├── momentum.py         # RSI + EMA crossover
│   └── volume_profile.py   # VPOC/VAH/VAL (96 x 15-min rolling)
├── backtest/
│   ├── sim_engine.py       # BacktestEngine — full pipeline with MS final gate
│   ├── data_loader.py      # BacktestTick, DataLoader, SyntheticGenerator
│   ├── portfolio.py        # Position management, TP1/TP2/TP3/SL
│   ├── fee_model.py        # 0.1% taker, ±0.05% slippage
│   ├── metrics.py          # PerformanceMetrics, PRD target checks
│   ├── walk_forward.py     # 70/30 chronological OOS validation
│   ├── monte_carlo.py      # 1000-iteration DD simulation
│   ├── optimizer.py        # GridSearchOptimizer (15k combinations)
│   └── report.py           # Text + JSON report generation
├── risk/
│   ├── risk_manager.py     # 4-tier drawdown scaling, VaR, correlation
│   ├── position_sizer.py   # Half-Kelly sizer
│   └── circuit_breaker.py  # Consecutive loss circuit breaker
├── execution/
│   ├── position_manager.py # Live position lifecycle (TP1/TP2/TP3/SL)
│   ├── hedger.py           # 5-trigger dynamic hedger
│   └── paper_trader.py     # Paper trading mode (live data simulation)
├── scripts/
│   ├── run_backtest.py     # Trades-only backtest CLI (now accepts --klines-dir)
│   ├── run_backtest_ob.py  # Tardis OB backtest CLI
│   ├── download_history.py # Binance historical data download
│   ├── download_tardis_ob.py # Tardis.dev OB download
│   ├── record_data.py      # Live data recording
│   ├── diagnose_signals.py
│   └── diagnose_real_ob.py
├── tests/                  # ~287 tests passing
├── reports/                # Generated backtest reports
│   ├── phase3_ms_backtest_report.{txt,json}
│   ├── phase3_ms_backtest_v2_report.{txt,json}
│   ├── real_ob_v2_report.{txt,json}
│   └── validation_faz4_p15_report.{txt,json}
└── historical_data/
    ├── BTCUSDT/trades/     # aggTrades CSVs (Binance download)
    ├── BTCUSDT/klines/1m/  # 1-min klines (use --klines-dir with run_backtest.py)
    └── tardis/             # Tardis.dev .csv.gz files
```

---

## Key Architectural Decisions (from development history)

1. **NOT an HFT bot.** 15-min to 4-hour analysis. 2-6 trades/day is the target.

2. **VETO = entry gate only (P1).** VETO (VPIN, Spread, Clearance) blocks new entries.
   Open positions are managed ONLY by their stop loss. No force-close from vetoes.

3. **Market Structure > Indicators (P3/P9).** PA Filter (EMA crossover, candlestick patterns)
   is SUSPENDED. MarketStructureReader uses pure price structure: CHoCH/BOS/swing points.
   No lagging indicators.

4. **Spread = confidence multiplier (P4).** Spread cannot predict direction. Tight spread
   = high confidence; wide spread = reduced confidence. Multiplier: 1.0/0.7/0.3/VETO.

5. **OBI continuous gradient (P6).** obi_ma ∈ [0,1] passed to CE, not {-1,0,+1}.
   CE converts: `(obi_ma - 0.5) * 2` for scaling.

6. **Dynamic VPIN bucket sizing (P2).** bucket_size = hourly_vol × 0.0005 ≈ 27-min timescale.
   Adapts to volume regime automatically.

7. **Clearance — all 4 traces (P8).** Genuine MM dumping shows ALL 4 footprints simultaneously.
   3-of-4 was too sensitive; false positives killed 6/8 early trades.

8. **ATR from 15-min candle ranges (P10).** Tick-to-tick |price diff| at 100s spacing is ~$5-50.
   True BTC ATR from candles is $200-800. Stops must use candle ATR.

9. **Spoofing blind in backtest (P11).** Sampled OB at 100s gaps cannot detect sub-800ms cancels.
   Accepted: spoof score = 0, CE weights redistributed. Spoof still active in live mode.

10. **S/R-based TP2 (P12).** `ms.find_next_sr_level()` finds nearest swing point in trade direction.
    Falls back to 2.5R × stop_dist if no S/R level is found above/below price.

11. **Static options regime (P5).** Default `options_regime = "NEUTRAL"` (multiplier 1.0) until
    Deribit historical data is downloaded. Using "LONG_GAMMA" was asymmetrically killing LONG signals.

12. **Auto-scaling windows (P14).** `sample_interval_sec` auto-scales OBI MA, spread baseline,
    and clearance OB depth windows in sim_engine.py so time-based logic stays valid with sampled data.

13. **MS is_ready guard.** When `is_ready=False` (fewer than min_swings swing points),
    `allows_direction()` returns True (pass-through). MS becomes active gradually as candles arrive.
    With klines warm-up (`--klines-dir`), MS can be ready from tick 1.

14. **Candle-based modules warm-up.** `_warm_up_structure()` feeds ALL candles that close before
    the first tick. This seeds Momentum, VolumeProfile, HTFTrend, and MS simultaneously.
    Without warm-up, all 4 modules are inactive for the first ~100 candles.

---

## Remaining Work (NOT STARTED)

### P13 — Historical Deribit Options Data
Download via Deribit public API (no auth required):
```
GET /api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option
GET /api/v2/public/get_historical_volatility?currency=BTC
```
Tools: bottama/Deribit-Option-Data (GitHub), DVOL as proxy (DVOL<50 → LONG_GAMMA, >70 → SHORT_GAMMA)
Impact: Activates real GEX/PCR for all 12 months of Tardis data; removes static assumption.

### P15 — Fee Economics
- Live: Use limit orders (maker fee 0.02% vs 0.10% taker). Round-trip: 0.04% vs 0.25%.
- Backtest: Simulate with 0.02% fee rates for accurate live projection.
- TP1 R:R: Increase from 1.5R to 2.0R. Break-even WR drops from 60% to ~40%.

### Phase 6 — AI & Improvements
- Claude Vision API for chart analysis + historical OB metrics
- Multi-pair expansion (SOL, ETH)
- Advanced dashboard with signal reasoning visualization
- Monthly parameter recalibration

---

## Tech Stack

- Python 3.12, asyncio
- websockets, aiohttp (feeds)
- influxdb-client[async], aiosqlite (storage)
- pydantic, pydantic-settings (config)
- orjson (fast JSON)
- pandas, numpy (analysis)
- pytest, pytest-asyncio (testing)
- Docker + docker-compose (deployment)

---

## Current Git State (2026-03-14)

Branch: `claude/exciting-wilbur`
Recent commits:
- `4edef2c` fix: use Bybit as primary OB source in testnet mode
- `ad6bf36` fix: _flush_paper_log use _trades not _closed_positions
- `bc644a9` fix: add 45s message timeout to WebSocket feed
- `7540a0e` fix: Binance feed URL + Bybit health monitor + CLI encoding
- `e1a71bc` feat: Faz 5 — Bybit testnet support + CLI entry point + paper trade log

Unstaged modifications as of session start:
- `.env` — modified
- `core/engine.py` — modified (Bybit primary OB fix)
- `signals/spoofing_detector.py` — modified
- `data/converter.py` — modified (load_klines_as_candles added)
- `scripts/run_backtest.py` — modified (--klines-dir, candles_15m passed to engine)
