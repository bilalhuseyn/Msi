"""
Download free historical order book data from Tardis.dev.

Downloads book_snapshot_25 (25-level order book) + trades
for the first day of each month (free, no API key needed).

Usage:
    python scripts/download_tardis_ob.py --months 12
    python scripts/download_tardis_ob.py --months 6 --exchange binance-futures --symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

TARDIS_BASE = "https://datasets.tardis.dev/v1"

CONFIGS = [
    {
        "exchange": "binance-futures",
        "symbol": "BTCUSDT",
        "data_types": ["book_snapshot_25", "trades"],
    },
]


def _first_of_months(months: int) -> list[date]:
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
    return dates


async def download_file(
    session: aiohttp.ClientSession,
    exchange: str,
    data_type: str,
    d: date,
    symbol: str,
    output_dir: Path,
) -> Path | None:
    url = (
        f"{TARDIS_BASE}/{exchange}/{data_type}"
        f"/{d.year}/{d.month:02d}/{d.day:02d}/{symbol}.csv.gz"
    )

    out_file = output_dir / f"{exchange}_{data_type}_{d.isoformat()}_{symbol}.csv.gz"

    if out_file.exists() and out_file.stat().st_size > 500:
        logger.info("SKIP (exists): %s", out_file.name)
        return out_file

    logger.info("GET  %s", url)
    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status == 451:
                logger.warning("BLOCKED (geo-restricted): %s", url)
                return None
            if resp.status != 200:
                logger.warning("HTTP %d for %s", resp.status, url)
                return None

            data = await resp.read()
            out_file.write_bytes(data)
            size_mb = len(data) / (1024 * 1024)
            logger.info("OK   %s (%.1f MB)", out_file.name, size_mb)
            return out_file

    except asyncio.TimeoutError:
        logger.error("TIMEOUT: %s", url)
        return None
    except Exception as e:
        logger.error("ERROR: %s -- %s", url, e)
        return None


async def run(
    months: int,
    exchange: str,
    symbol: str,
    output: str,
):
    output_dir = Path(output) / "tardis" / exchange
    output_dir.mkdir(parents=True, exist_ok=True)

    dates = _first_of_months(months)
    logger.info(
        "Downloading %d months of %s %s from Tardis.dev (FREE first-of-month)",
        len(dates), exchange, symbol,
    )
    logger.info("Dates: %s", [d.isoformat() for d in dates])

    data_types = ["book_snapshot_25", "trades"]

    async with aiohttp.ClientSession() as session:
        tasks = []
        for d in dates:
            for dt in data_types:
                tasks.append(download_file(session, exchange, dt, d, symbol, output_dir))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = sum(1 for r in results if isinstance(r, Path))
        fail = len(results) - ok
        logger.info("Done. %d/%d files downloaded (%d failed)", ok, len(results), fail)

    files = sorted(output_dir.glob("*book_snapshot_25*"))
    total_rows = 0
    for f in files:
        try:
            with gzip.open(f, "rt", encoding="utf-8") as gz:
                lines = sum(1 for _ in gz) - 1
                total_rows += lines
                logger.info("  %s: %d snapshots", f.name, lines)
        except Exception:
            pass

    logger.info("Total OB snapshots available: %d", total_rows)
    logger.info("Files saved to: %s", output_dir.resolve())


def main():
    parser = argparse.ArgumentParser(description="Download Tardis.dev OB data (free)")
    parser.add_argument("--months", type=int, default=6, help="Months of data")
    parser.add_argument("--exchange", default="binance-futures", help="Exchange")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol")
    parser.add_argument("--output", default="historical_data", help="Output dir")
    args = parser.parse_args()

    asyncio.run(run(args.months, args.exchange, args.symbol, args.output))


if __name__ == "__main__":
    main()
