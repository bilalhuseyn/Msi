"""
Download free historical data from Binance Data Vision (data.binance.vision).

Supports:
  - aggTrades (spot + USDT-M futures)
  - klines (any interval)

Downloaded data is saved as CSV files compatible with DataLoader.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL = "https://data.binance.vision/data"

_AGGTRADE_COLUMNS = [
    "agg_trade_id", "price", "qty",
    "first_trade_id", "last_trade_id",
    "timestamp", "is_buyer_maker", "is_best_match",
]

_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trade_count",
    "taker_buy_base", "taker_buy_quote", "ignore",
]

_OUR_TRADE_HEADER = ["timestamp", "price", "qty", "side", "trade_id"]
_OUR_KLINE_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]


class BinanceDownloader:
    """
    Downloads and converts Binance historical data into OFI Pro CSV format.

    >>> dl = BinanceDownloader(output_dir="historical_data")
    >>> await dl.download_aggtrades("BTCUSDT", start=date(2025, 1, 1), end=date(2025, 1, 7))
    >>> await dl.download_klines("BTCUSDT", interval="1m", start=date(2025, 1, 1), end=date(2025, 1, 7))
    """

    def __init__(
        self,
        output_dir: str = "historical_data",
        market: str = "futures/um",
        max_concurrent: int = 3,
    ):
        self._output_dir = Path(output_dir)
        self._market = market
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> BinanceDownloader:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=600),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def download_aggtrades(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        granularity: str = "daily",
    ) -> Path:
        """
        Download aggTrades and convert to our trade CSV format.
        Returns path to the merged output file.
        """
        out_dir = self._output_dir / symbol.upper() / "trades"
        out_dir.mkdir(parents=True, exist_ok=True)

        dates = list(_date_range(start, end))
        if granularity == "monthly":
            months = _unique_months(start, end)
            urls = [
                (m, self._aggtrades_url(symbol, month=m))
                for m in months
            ]
        else:
            urls = [
                (d.isoformat(), self._aggtrades_url(symbol, day=d))
                for d in dates
            ]

        tasks = [
            self._download_and_convert_trades(symbol, label, url, out_dir)
            for label, url in urls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success = sum(1 for r in results if not isinstance(r, Exception))
        failed = sum(1 for r in results if isinstance(r, Exception))
        logger.info(
            "[%s] aggTrades download complete — %d success, %d failed",
            symbol, success, failed,
        )
        return out_dir

    async def download_klines(
        self,
        symbol: str,
        interval: str = "1m",
        start: date | None = None,
        end: date | None = None,
    ) -> Path:
        """
        Download kline data and convert to our kline CSV format.
        Returns path to the merged output file.
        """
        out_dir = self._output_dir / symbol.upper() / "klines" / interval
        out_dir.mkdir(parents=True, exist_ok=True)

        s = start or (date.today() - timedelta(days=30))
        e = end or date.today()

        tasks = [
            self._download_and_convert_klines(
                symbol, d.isoformat(),
                self._klines_url(symbol, interval, d),
                out_dir,
            )
            for d in _date_range(s, e)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success = sum(1 for r in results if not isinstance(r, Exception))
        failed = sum(1 for r in results if isinstance(r, Exception))
        logger.info(
            "[%s] klines (%s) download complete — %d success, %d failed",
            symbol, interval, success, failed,
        )
        return out_dir

    def _aggtrades_url(
        self,
        symbol: str,
        *,
        day: date | None = None,
        month: str | None = None,
    ) -> str:
        sym = symbol.upper()
        if month:
            return f"{_BASE_URL}/{self._market}/monthly/aggTrades/{sym}/{sym}-aggTrades-{month}.zip"
        if day:
            return f"{_BASE_URL}/{self._market}/daily/aggTrades/{sym}/{sym}-aggTrades-{day.isoformat()}.zip"
        raise ValueError("Provide day or month")

    def _klines_url(self, symbol: str, interval: str, day: date) -> str:
        sym = symbol.upper()
        return (
            f"{_BASE_URL}/{self._market}/daily/klines/{sym}/{interval}/"
            f"{sym}-{interval}-{day.isoformat()}.zip"
        )

    async def _download_and_convert_trades(
        self, symbol: str, label: str, url: str, out_dir: Path,
    ) -> Path:
        out_file = out_dir / f"trades_{label}.csv"
        if out_file.exists() and out_file.stat().st_size > 100:
            logger.debug("[%s] Skipping existing %s", symbol, out_file.name)
            return out_file

        raw = await self._fetch_zip_csv(url, label, symbol)
        if raw is None:
            raise FileNotFoundError(f"No data for {label}")

        rows_written = 0
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_OUR_TRADE_HEADER)
            for row in raw:
                if len(row) < 7:
                    continue
                try:
                    ts_ms = int(row[5])
                except ValueError:
                    continue
                side = "sell" if row[6].strip().lower() in ("true", "1") else "buy"
                writer.writerow([
                    ts_ms,
                    row[1],  # price
                    row[2],  # qty
                    side,
                    row[0],  # agg_trade_id
                ])
                rows_written += 1

        if rows_written == 0:
            out_file.unlink(missing_ok=True)
            raise ValueError(f"No valid trade rows parsed for {label}")

        logger.info("[%s] Saved %s", symbol, out_file.name)
        return out_file

    async def _download_and_convert_klines(
        self, symbol: str, label: str, url: str, out_dir: Path,
    ) -> Path:
        out_file = out_dir / f"klines_{label}.csv"
        if out_file.exists() and out_file.stat().st_size > 100:
            logger.debug("[%s] Skipping existing %s", symbol, out_file.name)
            return out_file

        raw = await self._fetch_zip_csv(url, label, symbol)
        if raw is None:
            raise FileNotFoundError(f"No data for {label}")

        rows_written = 0
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_OUR_KLINE_HEADER)
            for row in raw:
                if len(row) < 6:
                    continue
                try:
                    float(row[0])
                except ValueError:
                    continue
                writer.writerow([
                    row[0],  # open_time as timestamp
                    row[1],  # open
                    row[2],  # high
                    row[3],  # low
                    row[4],  # close
                    row[5],  # volume
                ])
                rows_written += 1

        if rows_written == 0:
            out_file.unlink(missing_ok=True)
            raise ValueError(f"No valid kline rows parsed for {label}")

        logger.info("[%s] Saved %s", symbol, out_file.name)
        return out_file

    async def _fetch_zip_csv(
        self, url: str, label: str, symbol: str,
    ) -> list[list[str]] | None:
        async with self._semaphore:
            assert self._session is not None
            try:
                async with self._session.get(url) as resp:
                    if resp.status == 404:
                        logger.warning("[%s] 404 for %s — skipping", symbol, label)
                        return None
                    resp.raise_for_status()
                    data = await resp.read()
            except Exception as e:
                logger.error("[%s] Download failed for %s: %s", symbol, label, e)
                raise

            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                csv_name = zf.namelist()[0]
                with zf.open(csv_name) as f:
                    text = io.TextIOWrapper(f, encoding="utf-8")
                    reader = csv.reader(text)
                    return list(reader)
            except Exception as e:
                logger.error("[%s] Failed to parse ZIP for %s: %s", symbol, label, e)
                raise


def _date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _unique_months(start: date, end: date) -> list[str]:
    months = set()
    d = start
    while d <= end:
        months.add(d.strftime("%Y-%m"))
        d += timedelta(days=1)
    return sorted(months)


async def main():
    """CLI entry point for downloading data."""
    import argparse

    parser = argparse.ArgumentParser(description="Download Binance historical data")
    parser.add_argument("--symbol", default="BTCUSDT", help="Trading pair")
    parser.add_argument("--type", choices=["aggtrades", "klines"], default="aggtrades")
    parser.add_argument("--interval", default="1m", help="Kline interval")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--market", default="futures/um", help="spot or futures/um")
    parser.add_argument("--output", default="historical_data", help="Output directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s = date.fromisoformat(args.start)
    e = date.fromisoformat(args.end)

    async with BinanceDownloader(output_dir=args.output, market=args.market) as dl:
        if args.type == "aggtrades":
            path = await dl.download_aggtrades(args.symbol, s, e)
        else:
            path = await dl.download_klines(args.symbol, args.interval, s, e)
        print(f"Data saved to: {path}")


if __name__ == "__main__":
    asyncio.run(main())
