from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class DataRecorder:
    """
    Records live market data from the event bus to CSV files.

    Captures order book snapshots, trades, tickers, and Deribit options chain
    for later backtesting. Files are rotated daily.

    Usage:
        recorder = DataRecorder(output_dir="recorded_data")
        recorder.subscribe(event_bus)
        await recorder.start()
    """

    def __init__(
        self,
        output_dir: str = "recorded_data",
        flush_interval: float = 5.0,
        ob_depth: int = 20,
    ):
        self._output_dir = Path(output_dir)
        self._flush_interval = flush_interval
        self._ob_depth = ob_depth

        self._writers: dict[str, _CSVWriter] = {}
        self._running = False
        self._flush_task: asyncio.Task | None = None
        self._event_count = 0
        self._start_ts = 0.0

    async def start(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._start_ts = time.time()
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("DataRecorder started — output: %s", self._output_dir)

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        for w in self._writers.values():
            w.close()
        self._writers.clear()
        logger.info("DataRecorder stopped — %d events recorded", self._event_count)

    def subscribe(self, event_bus) -> None:
        """Register handlers on the event bus."""
        event_bus.subscribe("order_book", self.on_order_book)
        event_bus.subscribe("trade", self.on_trade)
        event_bus.subscribe("ticker", self.on_ticker)
        event_bus.subscribe("options_chain", self.on_options)

    async def on_order_book(self, event: dict) -> None:
        data = event.get("data", event)
        symbol = data.get("symbol", "UNKNOWN")
        exchange = data.get("exchange", "unknown")
        ts = data.get("timestamp_ms", int(time.time() * 1000))

        writer = self._get_writer(f"orderbook_{exchange}_{symbol}", self._ob_headers())

        row = [ts, symbol, exchange]
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        for i in range(self._ob_depth):
            if i < len(bids):
                row.extend([bids[i]["price"], bids[i]["qty"]])
            else:
                row.extend([0, 0])
        for i in range(self._ob_depth):
            if i < len(asks):
                row.extend([asks[i]["price"], asks[i]["qty"]])
            else:
                row.extend([0, 0])

        writer.writerow(row)
        self._event_count += 1

    async def on_trade(self, event: dict) -> None:
        data = event.get("data", event)
        symbol = data.get("symbol", "UNKNOWN")
        exchange = data.get("exchange", "unknown")

        writer = self._get_writer(f"trades_{exchange}_{symbol}", [
            "timestamp_ms", "symbol", "exchange", "price", "qty", "side", "trade_id",
        ])
        writer.writerow([
            data.get("timestamp_ms", int(time.time() * 1000)),
            symbol, exchange,
            data.get("price", 0), data.get("qty", 0),
            data.get("side", "unknown"), data.get("trade_id", ""),
        ])
        self._event_count += 1

    async def on_ticker(self, event: dict) -> None:
        data = event.get("data", event)
        symbol = data.get("symbol", "UNKNOWN")
        exchange = data.get("exchange", "unknown")

        writer = self._get_writer(f"ticker_{exchange}_{symbol}", [
            "timestamp_ms", "symbol", "exchange",
            "best_bid", "best_bid_qty", "best_ask", "best_ask_qty",
        ])
        writer.writerow([
            int(time.time() * 1000), symbol, exchange,
            data.get("best_bid", 0), data.get("best_bid_qty", 0),
            data.get("best_ask", 0), data.get("best_ask_qty", 0),
        ])
        self._event_count += 1

    async def on_options(self, event: dict) -> None:
        data = event.get("data", event)
        ts = int(time.time() * 1000)
        currency = data.get("currency", "BTC")
        options = data.get("options", [])

        writer = self._get_writer(f"options_deribit_{currency}", [
            "timestamp_ms", "instrument_name", "type", "strike",
            "open_interest", "gamma", "delta", "iv",
        ])
        for opt in options:
            writer.writerow([
                ts, opt.get("instrument_name", ""),
                opt.get("type", ""), opt.get("strike", 0),
                opt.get("open_interest", 0), opt.get("gamma", 0),
                opt.get("delta", 0), opt.get("iv", 0),
            ])
        self._event_count += 1

    def _ob_headers(self) -> list[str]:
        headers = ["timestamp_ms", "symbol", "exchange"]
        for i in range(1, self._ob_depth + 1):
            headers.extend([f"bid{i}_price", f"bid{i}_qty"])
        for i in range(1, self._ob_depth + 1):
            headers.extend([f"ask{i}_price", f"ask{i}_qty"])
        return headers

    def _get_writer(self, name: str, headers: list[str]) -> _CSVWriter:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"{name}_{date_str}"

        if key not in self._writers:
            day_dir = self._output_dir / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            filepath = day_dir / f"{name}.csv"
            self._writers[key] = _CSVWriter(filepath, headers)
            logger.info("DataRecorder: new file %s", filepath)

        return self._writers[key]

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            for w in self._writers.values():
                w.flush()

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_ts if self._start_ts > 0 else 0.0


class _CSVWriter:
    """Buffered CSV writer with automatic header management."""

    def __init__(self, filepath: Path, headers: list[str]):
        self._filepath = filepath
        file_exists = filepath.exists() and filepath.stat().st_size > 0
        self._file = open(filepath, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if not file_exists:
            self._writer.writerow(headers)
            self._file.flush()

    def writerow(self, row: list) -> None:
        self._writer.writerow(row)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.flush()
        self._file.close()
