"""
Download historical aggTrades from Binance Data Vision.

Usage:
    python -m scripts.download_history --symbol BTCUSDT --start 2025-02-25 --end 2025-03-04
"""

import asyncio
import argparse
import logging
from datetime import date

from data.binance_downloader import BinanceDownloader


async def run(symbol: str, start: date, end: date, market: str, output: str):
    async with BinanceDownloader(output_dir=output, market=market) as dl:
        path = await dl.download_aggtrades(symbol, start, end)
        print(f"\naggTrades saved to: {path}")

        path = await dl.download_klines(symbol, "1m", start, end)
        print(f"Klines saved to: {path}")


def main():
    parser = argparse.ArgumentParser(description="Download Binance historical data")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--market", default="futures/um")
    parser.add_argument("--output", default="historical_data")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end)
    asyncio.run(run(args.symbol, s, e, args.market, args.output))


if __name__ == "__main__":
    main()
