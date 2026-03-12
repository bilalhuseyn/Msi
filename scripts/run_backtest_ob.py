"""
Run backtest on REAL order book depth data from Tardis.dev.

Unlike run_backtest.py (trades-only, synthetic OB), this uses actual
25-level order book snapshots -- no synthetic books needed.

Usage:
    python scripts/run_backtest_ob.py
    python scripts/run_backtest_ob.py --data-dir historical_data/tardis/binance-futures --sample 200
    python scripts/run_backtest_ob.py --limit 50000 --output reports/real_ob_result
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
from data.converter import tardis_ob_to_ticks, _load_tardis_trades
from signals.market_structure import build_candles_from_trades

logger = logging.getLogger(__name__)


def _real_ob_settings():
    """
    Settings calibrated for real order book data (Tardis book_snapshot_25).

    Phase 1 fixes applied:
    - P1: VETO is entry-gate only (handled in CE + sim_engine)
    - P2: VPIN uses dynamic bucket sizing (bucket_size=500 BTC baseline)
    - P4: Spread is confidence multiplier (handled in CE, removed from weights)
    - P5: Options regime = NEUTRAL (default in settings.py)
    - VPIN veto at 0.90 with sustained confirmation (3 consecutive readings)
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
    )
    return obi, vpin, ce, bt


def run(
    data_dir: str,
    sample_every: int,
    limit: int | None,
    output: str | None,
) -> BacktestResult:
    data_path = Path(data_dir)

    logger.info("Building 15-min candles from Tardis trade data...")
    t0 = time.time()
    trade_files = sorted(data_path.glob("*trades*.csv.gz"))
    trades_by_sec = _load_tardis_trades(trade_files)
    candles_15m = build_candles_from_trades(trades_by_sec, interval_sec=900)
    logger.info("Built %d candles (15-min) in %.1fs", len(candles_15m), time.time() - t0)

    logger.info("Loading real OB data from %s (sample_every=%d)", data_dir, sample_every)
    t0 = time.time()
    ticks = tardis_ob_to_ticks(
        data_dir,
        depth=20,
        sample_every=sample_every,
        limit=limit,
    )
    load_time = time.time() - t0
    logger.info("Loaded %d ticks in %.1fs", len(ticks), load_time)

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

    obi_s, vpin_s, ce_s, bt_s = _real_ob_settings()
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    run(args.data_dir, args.sample, args.limit, args.output)


if __name__ == "__main__":
    main()
