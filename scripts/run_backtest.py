"""
Run backtest on real downloaded data.

Usage:
    python -m scripts.run_backtest --trades historical_data/BTCUSDT/trades --bucket-ms 60000

Reads downloaded aggTrades, converts to BacktestTick sequence, and runs
all 7 signal modules through the Confirmation Engine.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from backtest.sim_engine import BacktestEngine, BacktestResult
from backtest.report import BacktestReport
from config.settings import (
    BacktestSettings, VPINSettings, ConfirmationSettings, OBISettings,
)
from data.converter import trades_to_ticks

logger = logging.getLogger(__name__)


def _tuned_settings():
    """Settings tuned for trades-only backtesting (no real OB data)."""
    vpin = VPINSettings(
        bucket_size=50.0,
        window=50,
        veto_threshold=0.75,
        warning_threshold=0.60,
        min_buckets=50,
    )
    obi = OBISettings(
        depth=10,
        bullish_threshold=0.55,
        bearish_threshold=0.45,
        consistency_window=2,
    )
    ce = ConfirmationSettings(
        long_threshold=0.20,
        short_threshold=-0.20,
    )
    bt = BacktestSettings(
        cooldown_seconds=300,
    )
    return obi, vpin, ce, bt


def run(
    trades_dir: str,
    bucket_ms: int,
    limit: int | None,
    output: str | None,
) -> BacktestResult:
    logger.info("Loading trades from %s (bucket=%dms)", trades_dir, bucket_ms)
    t0 = time.time()
    ticks = trades_to_ticks(trades_dir, bucket_ms=bucket_ms, limit=limit)
    load_time = time.time() - t0
    logger.info("Loaded %d ticks in %.1fs", len(ticks), load_time)

    if not ticks:
        logger.error("No ticks generated -- check your data directory")
        return BacktestResult()

    logger.info(
        "Price range: %.2f - %.2f | Time span: %.0fs",
        min(t.mid_price for t in ticks),
        max(t.mid_price for t in ticks),
        ticks[-1].timestamp - ticks[0].timestamp if len(ticks) > 1 else 0,
    )

    obi_s, vpin_s, ce_s, bt_s = _tuned_settings()
    engine = BacktestEngine(
        obi_settings=obi_s,
        vpin_settings=vpin_s,
        confirmation_settings=ce_s,
        backtest_settings=bt_s,
    )
    logger.info("Running backtest (tuned for trades-only data)...")
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
    parser = argparse.ArgumentParser(description="Run OFI Pro backtest on real data")
    parser.add_argument("--trades", required=True, help="Directory with trade CSVs")
    parser.add_argument("--bucket-ms", type=int, default=60000, help="Tick bucket size in ms")
    parser.add_argument("--limit", type=int, default=None, help="Max trades to load")
    parser.add_argument("--output", default="reports/backtest_result", help="Output report path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    run(args.trades, args.bucket_ms, args.limit, args.output)


if __name__ == "__main__":
    main()
