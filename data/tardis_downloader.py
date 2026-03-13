"""
Download historical order book depth data from Tardis.dev.

Free data: first day of each month (no API key needed).
Provides book_snapshot_25 (25 levels bid/ask) + trades + options_chain.

Usage:
    python -m data.tardis_downloader --months 6
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import date, timedelta
from pathlib import Path

from tardis_dev import datasets

logger = logging.getLogger(__name__)


def download_ob_snapshots(
    output_dir: str = "historical_data",
    exchange: str = "binance-futures",
    symbol: str = "BTCUSDT",
    months: int = 6,
    api_key: str = "",
) -> Path:
    """
    Download book_snapshot_25 + trades for first day of each month.
    Free tier: no API key needed for first-of-month data.
    """
    out = Path(output_dir) / "tardis" / exchange / symbol
    out.mkdir(parents=True, exist_ok=True)

    today = date.today()
    dates = []
    for i in range(months):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        d = date(y, m, 1)
        if d < today:
            dates.append(d)

    dates.sort()

    for d in dates:
        from_date = d.isoformat()
        to_date = (d + timedelta(days=1)).isoformat()

        logger.info("Downloading %s %s for %s...", exchange, symbol, from_date)
        try:
            datasets.download(
                exchange=exchange,
                data_types=["book_snapshot_25", "trades"],
                from_date=from_date,
                to_date=to_date,
                symbols=[symbol],
                api_key=api_key if api_key else "",
                download_dir=str(out),
            )
            logger.info("OK: %s", from_date)
        except Exception as e:
            logger.error("Failed %s: %s", from_date, e)

    logger.info("Download complete. Files in: %s", out)
    return out


def download_deribit_options(
    output_dir: str = "historical_data",
    months: int = 6,
    api_key: str = "",
) -> Path:
    """Download Deribit options chain data (free first-of-month)."""
    out = Path(output_dir) / "tardis" / "deribit" / "BTC-OPTIONS"
    out.mkdir(parents=True, exist_ok=True)

    today = date.today()
    dates = []
    for i in range(months):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        d = date(y, m, 1)
        if d < today:
            dates.append(d)

    dates.sort()

    for d in dates:
        from_date = d.isoformat()
        to_date = (d + timedelta(days=1)).isoformat()

        logger.info("Downloading Deribit options for %s...", from_date)
        try:
            datasets.download(
                exchange="deribit",
                data_types=["options_chain"],
                from_date=from_date,
                to_date=to_date,
                symbols=["OPTIONS"],
                api_key=api_key if api_key else "",
                download_dir=str(out),
            )
            logger.info("OK: %s", from_date)
        except Exception as e:
            logger.error("Failed %s: %s", from_date, e)

    return out


def tardis_snapshot_to_ticks(
    data_dir: str | Path,
    depth: int = 20,
    *,
    sample_interval: int = 100,
) -> list[dict]:
    """
    Convert Tardis book_snapshot_25 CSV to our BacktestTick format.

    Args:
        data_dir: directory containing .csv.gz files from Tardis
        depth: how many levels to extract (max 25)
        sample_interval: take every Nth snapshot (1=all, 100=every 100th)

    Returns list of dicts ready for BacktestTick construction.
    """
    from backtest.data_loader import BacktestTick

    data_dir = Path(data_dir)
    snapshot_files = sorted(data_dir.rglob("*book_snapshot_25*.csv.gz"))

    if not snapshot_files:
        snapshot_files = sorted(data_dir.rglob("*book_snapshot*.csv.gz"))

    if not snapshot_files:
        logger.warning("No book_snapshot files found in %s", data_dir)
        return []

    trade_files = sorted(data_dir.rglob("*trades*.csv.gz"))
    trades_by_sec: dict[int, list[dict]] = {}

    for tf in trade_files:
        logger.info("Loading trades from %s", tf.name)
        with gzip.open(tf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_us = int(row.get("timestamp", 0))
                    ts_sec = ts_us // 1_000_000
                    trades_by_sec.setdefault(ts_sec, []).append({
                        "price": float(row.get("price", 0)),
                        "qty": float(row.get("amount", 0)),
                        "side": row.get("side", "unknown"),
                        "timestamp": ts_us / 1_000_000,
                    })
                except (ValueError, KeyError):
                    continue

    logger.info("Loaded trades for %d seconds", len(trades_by_sec))

    ticks: list[BacktestTick] = []
    row_count = 0

    for sf in snapshot_files:
        logger.info("Processing %s", sf.name)
        with gzip.open(sf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_count += 1
                if row_count % sample_interval != 0:
                    continue

                try:
                    ts_us = int(row.get("timestamp", 0))
                except (ValueError, KeyError):
                    continue

                ts_sec = ts_us / 1_000_000

                bids = []
                asks = []
                for i in range(min(depth, 25)):
                    bp = row.get(f"bids[{i}].price", "")
                    bq = row.get(f"bids[{i}].amount", "")
                    ap = row.get(f"asks[{i}].price", "")
                    aq = row.get(f"asks[{i}].amount", "")

                    if bp and bq:
                        bids.append({"price": float(bp), "qty": float(bq)})
                    if ap and aq:
                        asks.append({"price": float(ap), "qty": float(aq)})

                if not bids or not asks:
                    continue

                sec_key = int(ts_sec)
                matched_trades = trades_by_sec.get(sec_key, [])

                ticks.append(BacktestTick(
                    timestamp=ts_sec,
                    bids=bids,
                    asks=asks,
                    trades=matched_trades,
                    best_bid=bids[0]["price"],
                    best_ask=asks[0]["price"],
                ))

    logger.info(
        "Created %d ticks from %d snapshots (sample_interval=%d)",
        len(ticks), row_count, sample_interval,
    )
    return ticks


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(description="Download Tardis.dev order book data")
    parser.add_argument("--months", type=int, default=6, help="Months of first-day data")
    parser.add_argument("--exchange", default="binance-futures", help="Exchange ID")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol")
    parser.add_argument("--output", default="historical_data", help="Output dir")
    parser.add_argument("--api-key", default="", help="Tardis API key (optional)")
    parser.add_argument("--deribit", action="store_true", help="Also download Deribit options")
    args = parser.parse_args()

    download_ob_snapshots(
        output_dir=args.output,
        exchange=args.exchange,
        symbol=args.symbol,
        months=args.months,
        api_key=args.api_key,
    )

    if args.deribit:
        download_deribit_options(
            output_dir=args.output,
            months=args.months,
            api_key=args.api_key,
        )
