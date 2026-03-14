"""
Run backtest on REAL order book depth data from Tardis.dev or live recorder.

Unlike run_backtest.py (trades-only, synthetic OB), this uses actual
25-level order book snapshots -- no synthetic books needed.

Usage:
    python scripts/run_backtest_ob.py
    python scripts/run_backtest_ob.py --data-dir historical_data/tardis/binance-futures --sample 200
    python scripts/run_backtest_ob.py --limit 50000 --output reports/real_ob_result
    python scripts/run_backtest_ob.py --klines-dir historical_data/BTCUSDT/klines/15m --output reports/real_ob_klines

    # Using live-recorded OB data (after running record_data.py for 1+ week):
    python scripts/run_backtest_ob.py --recorded-dir recorded_data --klines-dir historical_data/BTCUSDT/klines/15m
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from backtest.sim_engine import BacktestEngine, BacktestResult
from backtest.report import BacktestReport
from config.settings import (
    BacktestSettings, VPINSettings, ConfirmationSettings, OBISettings,
)
from data.converter import tardis_ob_to_ticks, _load_tardis_trades, load_klines_as_candles, recorded_ob_to_ticks
from signals.market_structure import build_candles_from_trades

logger = logging.getLogger(__name__)


def _real_ob_settings(sample_every: int = 100):
    """
    Settings calibrated for real order book data (Tardis book_snapshot_25).

    Phase 1-3 fixes applied:
    - P1: VETO is entry-gate only (handled in CE + sim_engine)
    - P2: VPIN uses dynamic bucket sizing (bucket_size=500 BTC baseline)
    - P4: Spread is confidence multiplier (handled in CE, removed from weights)
    - P5: Options regime = NEUTRAL (default in settings.py)
    - VPIN veto at 0.90 with sustained confirmation (3 consecutive readings)

    Phase 4 / P15 fixes:
    - taker_fee_pct=0.0002: maker orders (0.02%) vs taker (0.1%)
    - slippage_pct=0.0001: limit orders have near-zero slippage
    - tp1_rr=2.0: first target at 2R (was 1.5R) to improve avg win/loss

    P14: sample_interval_sec passed so BacktestEngine auto-scales time windows.
    """
    vpin = VPINSettings(
        bucket_size=500.0,
        window=50,
        veto_threshold=0.90,
        warning_threshold=0.70,
        min_buckets=20,
    )
    obi = OBISettings(
        depth=20,
        bullish_threshold=0.55,
        bearish_threshold=0.45,
        consistency_window=2,
    )
    ce = ConfirmationSettings(
        long_threshold=0.20,
        short_threshold=-0.20,
    )
    bt = BacktestSettings(
        cooldown_seconds=600,
        taker_fee_pct=0.0002,        # P15: maker orders (0.02% vs 0.1% taker)
        slippage_pct=0.0001,         # P15: limit order slippage near-zero
        tp1_rr=2.0,                  # P15: 2R first target (was 1.5R)
        sample_interval_sec=sample_every,  # P14: enables window auto-scaling
    )
    return obi, vpin, ce, bt


def run(
    data_dir: str,
    sample_every: int,
    limit: int | None,
    output: str | None,
    klines_dir: str | None = None,
    recorded_dir: str | None = None,
) -> BacktestResult:
    t0 = time.time()

    # ── Candles (always from Binance klines when provided) ──────────────────
    if klines_dir:
        logger.info("Building 15-min candles from Binance klines: %s", klines_dir)
        candles_15m = load_klines_as_candles(klines_dir, target_interval_sec=900)
        logger.info("Loaded %d continuous candles in %.1fs", len(candles_15m), time.time() - t0)
    else:
        logger.info("No --klines-dir provided — building candles from Tardis trades (12 isolated days)...")
        data_path = Path(data_dir)
        trade_files = sorted(data_path.glob("*trades*.csv.gz"))
        _trades_for_candles = _load_tardis_trades(trade_files)
        candles_15m = build_candles_from_trades(_trades_for_candles, interval_sec=900)
        logger.info("Built %d candles in %.1fs", len(candles_15m), time.time() - t0)

    # ── OB ticks: recorded data OR Tardis ───────────────────────────────────
    if recorded_dir:
        logger.info("Loading LIVE-RECORDED OB data from %s (sample_every=%d)", recorded_dir, sample_every)
        t1 = time.time()
        ticks = recorded_ob_to_ticks(
            recorded_dir,
            depth=20,
            sample_every=sample_every,
            limit=limit,
        )
        logger.info("Loaded %d recorded ticks in %.1fs", len(ticks), time.time() - t1)
    else:
        logger.info("Loading Tardis OB data from %s (sample_every=%d)", data_dir, sample_every)
        data_path = Path(data_dir)
        t1 = time.time()
        trade_files = sorted(data_path.glob("*trades*.csv.gz"))
        trades_by_sec = _load_tardis_trades(trade_files)
        ticks = tardis_ob_to_ticks(
            data_dir,
            depth=20,
            sample_every=sample_every,
            limit=limit,
            trades_by_sec=trades_by_sec,  # no double load
        )
        logger.info("Loaded %d Tardis ticks in %.1fs", len(ticks), time.time() - t1)

    if not ticks:
        logger.error("No ticks generated -- check data directory: %s", data_dir)
        return BacktestResult()

    logger.info(
        "Price range: %.2f - %.2f | Time span: %.1f hours | Ticks: %d",
        min(t.mid_price for t in ticks),
        max(t.mid_price for t in ticks),
        (ticks[-1].timestamp - ticks[0].timestamp) / 3600 if len(ticks) > 1 else 0,
        len(ticks),
    )

    avg_depth = sum(len(t.bids) for t in ticks[:1000]) / min(len(ticks), 1000)
    avg_trades = sum(len(t.trades) for t in ticks[:1000]) / min(len(ticks), 1000)
    logger.info("Avg OB depth: %.1f levels | Avg trades/tick: %.1f", avg_depth, avg_trades)

    obi_s, vpin_s, ce_s, bt_s = _real_ob_settings(sample_every)
    engine = BacktestEngine(
        obi_settings=obi_s,
        vpin_settings=vpin_s,
        confirmation_settings=ce_s,
        backtest_settings=bt_s,
        candles_15m=candles_15m,
    )

    logger.info("Running backtest with REAL order book data + Market Structure (15m)...")
    t1 = time.time()
    result = engine.run(ticks)
    run_time = time.time() - t1
    logger.info("Backtest complete in %.1fs", run_time)

    report = BacktestReport(result)
    text = report.generate_text()
    print("\n" + text)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report.save(out_path.parent, out_path.stem)
        logger.info("Reports saved to %s", out_path.parent)

    return result


def main():
    parser = argparse.ArgumentParser(description="Run OFI Pro backtest on real OB data")
    parser.add_argument(
        "--data-dir",
        default="historical_data/tardis/binance-futures",
        help="Directory with Tardis .csv.gz files",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=100,
        help="Sample every Nth snapshot (100=~864 ticks/day, 10=~8640/day)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max ticks to load")
    parser.add_argument("--output", default="reports/real_ob_backtest", help="Output path")
    parser.add_argument(
        "--klines-dir",
        default=None,
        help="Directory with 15m kline CSVs for continuous candles (recommended). "
             "If omitted, candles are built from Tardis trade data (12 isolated days only).",
    )
    parser.add_argument(
        "--recorded-dir",
        default=None,
        help="Directory of live-recorded OB data from record_data.py. "
             "When provided, --data-dir (Tardis) is ignored for OB ticks. "
             "Use --klines-dir together with this for proper candle warm-up.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    run(args.data_dir, args.sample, args.limit, args.output, args.klines_dir, args.recorded_dir)


if __name__ == "__main__":
    main()
