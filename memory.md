# OFI Pro — Project Memory

> Bookmark file for AI context continuity across sessions.
> Last updated: 2026-03-08 (Real OB data acquired — Tardis.dev integration)

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

## Source Documents

| Document | Location | Purpose |
|----------|----------|---------|
| OFI_Pro_OnePager | Attached PDF | High-level architecture, 7 modules, weight matrix, development timeline |
| OFI_Pro_PRD_v1.0 | Attached PDF (33 pages) | Full technical specification — algorithms, code, thresholds, backtest targets |

---

## Architecture — 3 Layers

```
Layer 1: MM Regime Detector
  └── Deribit REST API (5min) → GEX/PCR/IV → Long/Short Gamma regime
  └── Modifies ALL signal weights via multiplier

Layer 2: Order Flow Engine (7 modules → Confirmation Engine)
  └── Binance/Bybit WebSocket (100ms) feeds into:
      M1: OBI          — Order Book Imbalance
      M2: VPIN         — Flow Toxicity (informed trading probability)
      M3: Spread Mon   — MM activity via bid-ask spread
      M4: Depth Erosion — Hidden institutional pressure
      M5: Spoof Det    — Manipulation detection
      M6: Clearance    — MM inventory dumping
      M7: Options      — GEX/PCR regime (Layer 1)
  └── Confirmation Engine aggregates all 7 → LONG/SHORT/NEUTRAL/VETO

Layer 3: Execution
  └── Position Manager, Dynamic Hedger, AI Trader Assistance (manual)
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
- **M1: OBI** — bid_vol/(bid_vol+ask_vol), 10-period MA, 3-period consistency, +1/-1/0
- **M2: VPIN** — 500 BTC buckets, 50-bucket rolling window, spike filter, GLOBAL VETO >0.65
- **M3: Spread Monitor** — 24h baseline, MM_ACTIVE/CAUTIOUS/THINNING/WITHDRAWN, VETO on withdrawal
- **M7: Options Layer** (early inclusion) — GEX calc, PCR calc, regime detection, flip risk,
  multipliers: SHORT_GAMMA x1.20, LONG_GAMMA x0.85, GEX_FLIP x0.60

### Phase 2 — Advanced Signal Modules (DONE)
- **M4: Depth Erosion** — 60s periodic OB depth comparison, HIDDEN_BUY/HIDDEN_SELL when
  price stays stable but one side erodes >35%. Suppressed when spoof is active.
- **M5: Spoofing Detector** — Tracks large orders (>50 BTC), flags cancellation within
  800ms as spoof. BID spoof → SELL intent, ASK spoof → BUY intent. Consecutive tracking.
- **M6: Clearance Detector** — 4-trace algorithm:
  (1) One-sided trade flow >72%
  (2) Best ask sliding down
  (3) Large sell cluster (>4x avg)
  (4) Bid-side thinning (<55%)
  Score >=3 → CLEARANCE_ACTIVE → GLOBAL VETO

### Phase 3 — Confirmation Engine (DONE)
- Weight matrix: OBI(0.18) + Spread(0.20) + Depth(0.18) + Spoof(0.16) + Clearance(0.14) + Options(0.14) = 1.0
- 3 global veto gates: VPIN >0.65, MM_WITHDRAWN, CLEARANCE_ACTIVE
- Spoof→OBI interaction: BID spoof reverses BULLISH OBI, ASK reverses BEARISH, 3+ spoofs zeros OBI
- Regime modifier applied to weighted score
- VPIN warning band (0.55–0.65) halves signal strength
- OBI consistency penalty for mixed readings
- Decision thresholds: score >=+0.35 → LONG, <=-0.35 → SHORT, else NEUTRAL

### Risk Management Enhancements (DONE, beyond PRD)
- Drawdown-responsive scaling: 4 progressive tiers (1%→0.3% risk) instead of binary halt
- Circuit breaker: 3 losses → 50% size for 2 trades; 5 losses → 1h cooldown + 25% size
- Cross-asset correlation tracker: rolling Pearson, cap at 1.5x combined when r>0.85
- Funding rate awareness: annualized rate tracking, expensive hedge flagging
- Portfolio VaR: ATR-based 95% confidence, blocks new positions if VaR >2%
- Half-Kelly position sizer: caps size at 50% Kelly fraction

---

### Phase 4 — Backtest Framework (DONE)
- **BacktestEngine** — replays historical data through all 6 active signal modules
  (OBI, VPIN, Spread, Depth Erosion, Spoofing, Clearance) + Confirmation Engine.
  Options Layer skipped in backtest (no historical Deribit data).
- **DataLoader** — loads from CSV (klines, trades, orderbook snapshots).
  SyntheticGenerator creates random-walk market data for testing.
- **FeeModel** — 0.1% taker fee per trade, ±0.05% slippage (configurable, randomizable).
- **Portfolio** — tracks balance, up to 2 concurrent positions, enforces cooldown,
  daily trade limits, max hold time (4h). Partial exits: TP1 (50% at 1.5R),
  TP2 (30% at 2.5R), TP3 (trailing ATR×1.5). Break-even at 1R. Stop management.
- **Metrics** — calculates Win Rate, Gross Profit/Loss, Net PnL, Profit Factor,
  Avg R:R, Max Drawdown (USD + %), Sharpe Ratio, Sortino Ratio, Calmar Ratio,
  avg hold time, daily trades. Auto-checks against PRD targets.
- **WalkForwardSplitter** — chronological 70/30 train/OOS split. Supports rolling
  windows. Validate method runs both splits and calculates degradation. Robustness
  check: WR change <-30%, Sharpe <-50%, PF <-40% = not robust.
- **MonteCarloSimulator** — 1,000 iterations (configurable), shuffles trade PnL order.
  Reports median/mean/95th/99th percentile max drawdown, ruin probability (50% threshold).
  Confidence interval method for custom confidence levels.
- **GridSearchOptimizer** — full grid or random search. ParameterSpace defines PRD ranges:
  OBI bullish [0.60–0.70], consistency [2–5], VPIN veto [0.60–0.70],
  depth erosion [0.25–0.45], spoof cancel [400–1200ms], CE threshold [0.25–0.45].
  Objective functions: sharpe, profit_factor, or composite (multi-metric).
- **BacktestReport** — generates text and JSON reports. Includes PRD target pass/fail,
  Monte Carlo section, walk-forward comparison table. Save to file.
- **BacktestSettings** added to settings.py — all configurable from .env.

---

### Phase 4.5 — Data Infrastructure & Real-Data Backtest (DONE)
- **DataRecorder** (`data/recorder.py`) — hooks into EventBus, writes live OB snapshots,
  trades, tickers, and Deribit options chain to daily-rotated CSV files.
  Buffered writes with configurable flush interval. Records all 4 event types.
- **BinanceDownloader** (`data/binance_downloader.py`) — downloads free historical data
  from data.binance.vision (aggTrades + klines). Supports daily/monthly granularity,
  spot and USDT-M futures. ZIP → CSV conversion with automatic format detection.
  Skips existing files >100 bytes, handles 404s gracefully.
- **Data Converter** (`data/converter.py`) — transforms downloaded/recorded data into
  BacktestTick sequences:
  - `trades_to_ticks()` — groups trades into time buckets, builds synthetic OB from
    VWAP + trade flow imbalance. Auto-detects ms vs seconds timestamps.
  - `merge_ob_and_trades()` — aligns recorded OB snapshots with trade data for
    highest-fidelity backtest input.
- **Scripts:**
  - `scripts/download_history.py` — CLI for downloading Binance history
  - `scripts/record_data.py` — CLI for live data recording (Binance WS + Deribit REST)
  - `scripts/run_backtest.py` — CLI for running backtest with tuned settings
  - `scripts/diagnose_signals.py` — Signal diagnostics tool
- **First Real-Data Backtest Results (BTCUSDT futures, Feb 28 - Mar 1, 2025):**
  - 5.7M aggTrades downloaded (2 days, ~250MB)
  - 2,874 ticks generated (1-min buckets)
  - Price range: $78,355 - $86,494
  - 16 trades executed, max drawdown 0.52%, -$36.23 PnL
  - 0% win rate (expected with synthetic OB — real OB data needed for alpha)
  - **Key finding:** synthetic order books from trades are too balanced for OBI/Spoof
    signals. The system works end-to-end but needs recorded OB data for meaningful results.

#### Data Source Strategy
| Data Type | Source | Status | Notes |
|-----------|--------|--------|-------|
| aggTrades (hist) | data.binance.vision | AVAILABLE FREE | Daily/monthly ZIP downloads |
| klines (hist) | data.binance.vision | AVAILABLE FREE | Any interval |
| **OB Depth (hist)** | **Tardis.dev** | **ACQUIRED FREE** | **17.3M snapshots, 25-level, 12 months** |
| Order Book (live) | Binance WS depth20@100ms | FREE | Via existing BinanceFeed |
| Options (hist) | Self-recorded + crypto-data.io | NEEDS RECORDING | Deribit public API |
| Options (live) | Deribit REST API | FREE | Via existing DeribitPoller |

### Real OB Data — Tardis.dev Integration (DONE)
- **Source:** Tardis.dev free tier — first day of each month, no API key required
- **Downloaded:** 12 months (Apr 2025 - Mar 2026) `book_snapshot_25` + `trades`
- **Format:** 25-level bid/ask depth, tick-level (every OB change), gzip CSV
- **Volume:** 17,261,267 OB snapshots + 43,508,888 matched trades (~1GB total)
- **File sizes:** 32-88 MB per month (OB), 9-57 MB per month (trades)
- **Downloader:** `scripts/download_tardis_ob.py` — async parallel downloads via aiohttp
- **Converter:** `data/converter.tardis_ob_to_ticks()` — Tardis CSV.gz -> BacktestTick
  Loads gzip trades into per-second index, then streams OB snapshots with sampling.
- **Sampling:** `sample_every=N` reduces ~1.5M snapshots/day to manageable tick count
- **Free data strategy:** First-of-month gives diverse market conditions across seasons
- **Backtest script:** `scripts/run_backtest_ob.py` — calibrated settings for real OB

#### First Real-OB Backtest Results
- **Data:** 50,000 ticks from 20-level real depth (Apr-Oct 2025)
- **Price range:** $82,385 - $117,155 | Time span: 4,406 hours (183 days)
- **Avg OB depth:** 20.0 levels | Avg trades/tick: 25.5
- **Results:** 56 trades, 3.6% win rate, -$136 PnL, max DD 1.92%
- **Signal diagnostics (Feb 2026 single-month run):**
  - OBI fires on ~10% of ticks — real imbalances detected correctly
  - VPIN bucket_size=50 too large — only 81 buckets from 1.5M trades (not ready)
  - CE scores range [-0.32, +0.31] — barely crossing ±0.30 thresholds
  - Spoofing detection silent (expected with sampled snapshots)
  - 567 VETO events from spread/clearance signals
- **Key finding:** System detects genuine OB imbalances but trades counter-trend
  without PA Filter integration. 15/16 shorts in one period hit SL because BTC
  was trending up. Needs PA Filter trend confirmation in backtest loop.

#### Calibration for Real OB Data
| Parameter | Trades-Only | Real OB | Notes |
|-----------|-------------|---------|-------|
| VPIN bucket_size | 50.0 | 5.0 | Must fill quickly with 1.5M+ trades/day |
| VPIN veto_threshold | 0.75 | 0.85 | Less aggressive with real flow data |
| OBI depth | 10 | 20 | Use full depth from Tardis |
| OBI bull/bear | 0.55/0.45 | 0.55/0.45 | Works well with real depth |
| CE long/short | ±0.20 | ±0.20 | Lower bar since OBI is genuine |
| Cooldown | 300s | 600s | Reduce overtrading |

---

### Phase 5 — Live Execution Layer (DONE)

- **Price Action Filter** (`signals/pa_filter.py`) — Final confirmation gate after CE signal.
  - Multi-timeframe trend analysis (1m/5m/15m) using EMA(8)/EMA(21) crossover
  - 6 candlestick patterns: Bullish/Bearish Engulfing, Bullish/Bearish Pin Bar,
    Inside Bar, Doji. Detection uses range-relative wick ratios for robustness.
  - Volume confirmation: current bar > 1.5x rolling 20-bar average
  - Support/Resistance detection: swing high/low pivot points with proximity
    merging (0.2% threshold). Top 20 levels ranked by touch strength.
  - `find_tp2_level()` replaces the 2.5R TP2 placeholder — finds next S/R level
    in the direction of the trade. Falls back to 2.5R if no S/R detected.
  - Confidence scoring: pattern(0.30) + trend_aligned(0.35) + volume(0.20) + sr_clear(0.15)
  - Trade blocked if price is within 0.3% of opposing S/R level

- **Position Manager** (`execution/position_manager.py`) — Live position lifecycle.
  - Per-tick update cycle: price extremes → stop loss → time exit → TP1 → TP2 → trailing → BE
  - TP1: 50% exit at 1.5R, stop moves to break-even
  - TP2: 30% exit at next S/R level (via PA Filter), stop tightens to +0.5R
  - TP3: Remaining 20% trails with ATR × 1.5 stop
  - Break-even at 1R if TP1 hasn't triggered yet
  - Max hold 4 hours → forced time exit
  - Enforces max 2 positions, 8 trades/day, 15-min cooldown
  - `update_tp2()` allows PA Filter to dynamically adjust TP2 based on new S/R

- **Dynamic Hedger** (`execution/hedger.py`) — PRD 5-trigger hedge system.
  - Trigger 1: SIZE — position notional > $5,000
  - Trigger 2: VPIN — VPIN reading > 0.55
  - Trigger 3: TIME — holding > 120 minutes
  - Trigger 4: ADVERSE — unrealized PnL < -1.2%
  - Trigger 5: GEX_FLIP — Options Layer detects gamma regime flip
  - Hedge levels: 2 triggers → 40% close, 3 → 65%, 4 → 85%, 5 → 100% (full close)
  - 0-1 triggers → no hedge action

- **Paper Trading Mode** (`execution/paper_trader.py`) — Full simulation on live data.
  - Integrates PositionManager + PriceActionFilter + DynamicHedger
  - Processes CE decisions in real-time with virtual balance
  - PA Filter acts as final gate — blocks signals without confirming price action
  - Tracks running stats: W/L, PnL, max DD, PA blocks, vetoes, hedges
  - Session save/load to JSON for post-analysis
  - Status dashboard: `print_status()` for CLI monitoring
  - PRD requirement: minimum 2 weeks paper trading before live execution

- **Settings added:**
  - `PAFilterSettings` — EMA periods, volume multiplier, S/R lookback, min confidence
  - `HedgerSettings` — all 5 trigger thresholds

---

## Remaining Phases (NOT STARTED)

### Phase 6 — AI & Improvements
- AI Trader Assistance: Claude Vision API for chart analysis + historical OB metrics
- Multi-pair expansion (SOL etc.)
- Advanced dashboard with signal reasoning visualization
- Monthly parameter recalibration

---

## File Structure

```
OBI_Pro/
├── config/
│   ├── __init__.py
│   ├── constants.py        # SignalDirection, SpreadStatus, VetoReason, RegimeType, etc.
│   └── settings.py         # Pydantic settings for all modules + exchanges + InfluxDB
├── core/
│   ├── engine.py           # OFIEngine — master async loop, 7 modules + CE
│   ├── events.py           # EventBus — asyncio.Queue fan-out dispatcher
│   └── health.py           # HealthMonitor — component status tracking
├── data/
│   ├── feeds/
│   │   ├── base.py         # BaseFeed — abstract WS with reconnect
│   │   ├── binance_feed.py # depth20@100ms, aggTrade, bookTicker
│   │   ├── bybit_feed.py   # orderbook.200, publicTrade
│   │   └── deribit_feed.py # REST poller — options chain (5min)
│   ├── normalizer.py       # UTC sync, outlier filter, baseline tracker
│   └── storage.py          # InfluxWriter + SQLiteStore
├── signals/
│   ├── base.py             # BaseSignalModule, SignalOutput
│   ├── obi.py              # M1: Order Book Imbalance
│   ├── vpin.py             # M2: VPIN (flow toxicity)
│   ├── spread_monitor.py   # M3: Spread Monitor (MM activity)
│   ├── depth_erosion.py    # M4: Depth Erosion (hidden pressure)
│   ├── spoofing_detector.py# M5: Spoofing Detector
│   ├── clearance_detector.py# M6: Inventory Clearance
│   ├── options_layer.py    # M7: GEX/PCR/regime
│   └── confirmation.py     # Confirmation Engine — final LONG/SHORT/VETO
├── risk/
│   ├── risk_manager.py     # RiskManager, CorrelationTracker, FundingRate, VaR
│   ├── position_sizer.py   # Dynamic sizing with half-Kelly overlay
│   └── circuit_breaker.py  # CircuitBreaker + DrawdownScaler
├── utils/
│   ├── logging.py          # JSON logging + SignalLogger
│   └── reconnect.py        # ExponentialBackoff
├── backtest/
│   ├── __init__.py
│   ├── data_loader.py      # BacktestTick, DataLoader, SyntheticGenerator
│   ├── fee_model.py        # FeeModel (0.1% fee, ±0.05% slippage)
│   ├── portfolio.py        # Portfolio, Position, TradeRecord
│   ├── metrics.py          # PerformanceMetrics, calculate_metrics
│   ├── sim_engine.py       # BacktestEngine — replay through all modules
│   ├── walk_forward.py     # WalkForwardSplitter (70/30 chronological)
│   ├── monte_carlo.py      # MonteCarloSimulator (1000 iter, 95% CI)
│   ├── optimizer.py        # GridSearchOptimizer, ParameterSpace
│   └── report.py           # BacktestReport (text + JSON)
├── data/
│   ├── feeds/              # (as before)
│   ├── normalizer.py
│   ├── storage.py
│   ├── recorder.py         # Live data recorder (OB/trades/options -> CSV)
│   ├── binance_downloader.py # data.binance.vision downloader
│   ├── converter.py        # Raw CSV -> BacktestTick (trades-only + Tardis OB)
│   └── tardis_downloader.py # NEW: Tardis.dev OB depth downloader
├── scripts/
│   ├── download_history.py # CLI — download Binance historical data
│   ├── download_tardis_ob.py # NEW: CLI — download Tardis.dev OB snapshots (free)
│   ├── record_data.py      # CLI — record live market data
│   ├── run_backtest.py     # CLI — run trades-only backtest
│   ├── run_backtest_ob.py  # NEW: CLI — run real-OB-data backtest
│   ├── diagnose_signals.py # Signal diagnostics tool
│   └── diagnose_real_ob.py # NEW: Real OB signal diagnostics
├── reports/                # Generated backtest reports (.txt + .json)
├── historical_data/        # Downloaded Binance data (gitignored)
├── recorded_data/          # Live-recorded data (gitignored)
├── execution/
│   ├── __init__.py
│   ├── position_manager.py # NEW: Live position lifecycle management
│   ├── hedger.py          # NEW: 5-trigger dynamic hedger
│   └── paper_trader.py    # NEW: Paper trading mode (simulated live)
├── tests/                  # 287 tests (all passing)
│   ├── conftest.py
│   ├── test_obi.py
│   ├── test_vpin.py
│   ├── test_spread.py
│   ├── test_depth_erosion.py
│   ├── test_spoofing.py
│   ├── test_clearance.py
│   ├── test_options.py
│   ├── test_confirmation.py
│   ├── test_risk.py
│   ├── test_integration.py
│   ├── test_backtest.py
│   └── test_data_infra.py  # NEW: 27 tests for recorder/downloader/converter
├── requirements.txt
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
└── memory.md               # This file
```

---

## Key Thresholds & Parameters

| Parameter | Value | Context |
|-----------|-------|---------|
| OBI bullish threshold | 0.65 | OBI MA above this → BULLISH |
| OBI bearish threshold | 0.35 | OBI MA below this → BEARISH |
| OBI consistency window | 3 periods | Must align 3x before signal emits |
| VPIN veto threshold | 0.65 | GLOBAL VETO — system halts |
| VPIN warning band | 0.55–0.65 | Signal strength halved |
| VPIN bucket size | 500 BTC | Volume-sync bucket |
| Spread MM_ACTIVE | ratio < 1.20x | Normal MM activity |
| Spread MM_CAUTIOUS | 1.20–1.80x | Size reduced 30% |
| Spread MM_THINNING | 1.80–3.00x | No new positions |
| Spread MM_WITHDRAWN | > 3.00x | GLOBAL VETO |
| Depth erosion threshold | 35% | OB side erosion to trigger |
| Depth price stability | 0.3% | Max price move for erosion to count |
| Spoof size threshold | 50 BTC | Minimum order size tracked |
| Spoof cancel window | 800ms | Max time for cancel to count as spoof |
| Clearance score for veto | >= 3 of 4 traces | CLEARANCE_ACTIVE |
| CE LONG threshold | +0.35 | Score above → LONG signal |
| CE SHORT threshold | -0.35 | Score below → SHORT signal |
| SHORT_GAMMA multiplier | x1.20 | Amplifies breakout signals |
| LONG_GAMMA multiplier | x0.85 | Dampens breakout signals |
| GEX_FLIP multiplier | x0.60 | Conservative — volatility incoming |
| Risk per trade | 1% of account | Base risk |
| Max daily loss | 3% | System halts |
| Max weekly loss | 7% | Manual approval needed |
| Max daily trades | 8 | Anti-overtrading |
| Cooldown between trades | 15 minutes | Anti-impulse |
| Max open positions | 2 | Concentration limit |

---

## Weight Matrix (Confirmation Engine)

| Module | Weight | Max Contribution | Veto Condition |
|--------|--------|-----------------|----------------|
| OBI | 0.18 | ±0.18 | Spoof overrides |
| Spread | 0.20 | ±0.20 | MM_WITHDRAWN → veto |
| Depth Erosion | 0.18 | ±0.18 | Spoof suppresses |
| Spoofing | 0.16 | ±0.16 | Reverses OBI |
| Clearance | 0.14 | ±0.14 | ACTIVE → veto |
| Options/GEX | 0.14 | ±0.14 | Flip risk weakens |

---

## Tech Stack

- Python 3.12, asyncio
- websockets, aiohttp (feeds)
- influxdb-client[async], aiosqlite (storage)
- pydantic, pydantic-settings (config)
- orjson (fast JSON)
- pandas, numpy, pandas_ta (analysis)
- pytest, pytest-asyncio (testing)
- Docker + docker-compose (deployment)

---

## Test Status

- **287 tests passing** (3.17s runtime)
- Phase 0-3: 129 tests — signal modules, confirmation engine, risk, events, normalizer
- Phase 4: 78 tests — data loader, fee model, portfolio, metrics, sim engine,
  walk-forward, Monte Carlo, optimizer, report, options regime modes,
  daily limit compliance, full pipeline integration
- Phase 4.5: 27 tests — CSVWriter, DataRecorder (4 event types), BinanceDownloader
  (URL generation, ZIP parsing, skip logic, 404 handling), data converter
  (trades_to_ticks, merge_ob_and_trades, empty dirs, tick structure)
- Phase 5: 53 tests — PriceActionFilter (trend detection, 6 candle patterns,
  volume confirmation, S/R detection, TP2 level finding, multi-TF storage),
  PositionManager (open/close, stop loss, TP1/TP2/TP3, break-even, trailing,
  time exit, short positions, force close, daily limits),
  DynamicHedger (0-5 trigger levels, all threshold combinations),
  PaperTrader (veto/neutral/signal handling, PA blocking, session save, drawdown),
  Candle dataclass, EMA helper

---

## Development Decisions & Notes

1. Options Layer (M7) was pulled into Phase 1 instead of Phase 2 because it
   modifies ALL signal weights — testing the multiplier pipeline early matters.

2. Settings use Pydantic BaseSettings but without ge/le validation constraints
   so unit tests can use small values for fast iteration.

3. Bybit data is collected but currently only Binance data flows through signal
   modules (engine filters on `exchange == "binance"`). Bybit is for future
   cross-validation.

4. The Confirmation Engine does NOT call modules directly — it receives pre-computed
   signal scores and metadata via a `signals` dict. This keeps it pure/testable.

5. Risk enhancements beyond PRD: drawdown-responsive scaling (4 tiers), consecutive
   loss circuit breaker, cross-asset correlation cap, funding rate tracking,
   portfolio VaR. These are not in the original PRD but address real production gaps.

6. VPIN is NOT part of the weight matrix. It acts only as a global veto gate
   and a signal-strength reducer (warning band). This matches the PRD design.

7. Spoof→OBI interaction is critical: a BID-side spoof with OBI BULLISH means
   the "bullish" book is fake — the engine flips OBI to BEARISH. 3+ consecutive
   spoofs suspend OBI entirely (set to 0).

8. Backtest engine runs ALL 7 modules. M7 (Options Layer) uses a configurable
   static regime assumption via `BacktestSettings.options_regime`:
   - `"LONG_GAMMA"` (default) — multiplier x0.85, conservative, dampens signals
   - `"SHORT_GAMMA"` — multiplier x1.20, amplifies breakout signals
   - `"NEUTRAL"` — no regime modifier (multiplier x1.0)
   When real historical Deribit options data is provided in BacktestTick.options_chain,
   the OptionsLayer computes live GEX/PCR and overrides the static assumption.
   This ensures backtest results include the regime modifier effect that the
   live system will apply, preventing behavior divergence.

9. GridSearchOptimizer supports 3 objective functions: sharpe (default),
   profit_factor, and composite (multi-metric blend of Sharpe×0.35 + WinRate×0.20
   + PF×0.25 + inverse-DD×0.20). Random search is recommended for initial
   exploration before full grid.

10. WalkForward robustness criteria: if OOS win rate degrades >30% vs train,
    or Sharpe degrades >50%, or Profit Factor degrades >40%, the parameter set
    is NOT considered robust. This prevents overfitting.

11. TP2 at 2.5R is a backtest placeholder. PRD defines TP2 as "next S/R level"
    which requires the Price Action Filter (Phase 5) for detection. The fixed
    R-multiple is acceptable for parameter optimization and Monte Carlo analysis.
    Phase 5 will replace it with PA Filter-based S/R detection.

12. TP3 trailing stop uses ATR × 1.5 per PRD Section 7.2 spec. Confirmed in
    BacktestSettings.tp3_trailing_atr and Portfolio trailing stop logic.

13. Daily 8-trade hard limit is tracked in metrics.py as `max_trades_in_a_day`
    and `days_exceeding_limit`. The PRD compliance check `daily_limit_respected`
    is included in `meets_targets` and the backtest report.

14. **Data timestamps:** Binance aggTrades are in milliseconds. The converter
    auto-detects this (timestamp > 1e12) and converts to seconds for the
    backtest engine, which expects UNIX seconds.

15. **Synthetic OB limitations:** When backtesting with trades-only data,
    the converter generates synthetic order books from trade VWAP + flow
    imbalance. This is sufficient for VPIN and basic OBI, but Spoof (always 0),
    Depth Erosion (limited), and OBI (often near-balanced) produce weak signals.
    Real OB data from the recorder is needed for meaningful alpha testing.

16. **Tuned backtest settings for trades-only data:**
    - VPIN bucket_size: 50 (vs 500 default) — more granular for smaller buckets
    - VPIN veto_threshold: 0.75 (vs 0.65) — less aggressive veto
    - OBI thresholds: 0.55/0.45 (vs 0.65/0.35) — more sensitive
    - CE thresholds: ±0.20 (vs ±0.35) — lower bar for signal generation
    These are exploration parameters, not production settings.

17. **Tardis.dev as OB data source:** Instead of waiting 2-3 weeks to record
    live OB data, we found Tardis.dev provides free historical `book_snapshot_25`
    data (25-level depth, tick-level) for the first day of each month.
    Downloaded 12 months of data (17.3M snapshots, 43.5M trades, ~1GB).
    This eliminates the need to self-record OB data for initial backtesting.

18. **Real OB vs Synthetic OB performance gap:** With synthetic OB (from trades),
    OBI fires weakly and is often near-balanced. With real OB (Tardis), OBI
    correctly identifies imbalances on ~10% of ticks. However, OBI imbalance
    alone predicts short-term direction poorly without trend confirmation.

19. **Counter-trend trading problem:** First real-OB backtest showed 15/16
    short signals during a bullish BTC period. The CE correctly detects
    ask-side pressure but doesn't know the macro trend. Integration of
    Price Action Filter into the backtest loop is the critical next step.

20. **VPIN scaling issue with real data:** At 43.5M trades/day, VPIN
    bucket_size must be 5.0 (not 50.0 or 500.0) to fill buckets at a
    meaningful rate and activate within the first few hundred ticks.
