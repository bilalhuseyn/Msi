"""
Live data recorder — captures Binance OB + trades and Deribit options to CSV.

Usage:
    python -m scripts.record_data --symbol BTCUSDT --output recorded_data

Press Ctrl+C to stop recording.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from config.settings import Settings
from core.events import EventBus
from data.feeds.binance_feed import BinanceFeed
from data.feeds.deribit_feed import DeribitPoller
from data.recorder import DataRecorder

logger = logging.getLogger(__name__)


async def run_recorder(
    symbol: str,
    output_dir: str,
    record_options: bool = True,
) -> None:
    settings = Settings()
    event_bus = EventBus()
    recorder = DataRecorder(output_dir=output_dir)

    feed = BinanceFeed(
        symbol=symbol,
        event_bus=event_bus.queue,
        settings=settings,
    )

    deribit: DeribitPoller | None = None
    if record_options:
        deribit = DeribitPoller(
            event_bus=event_bus.queue,
            settings=settings,
        )

    recorder.subscribe(event_bus)

    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    await event_bus.start()
    await recorder.start()
    await feed.start()
    if deribit:
        await deribit.start()

    logger.info("Recording started for %s — press Ctrl+C to stop", symbol)

    status_interval = 60
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=status_interval)
        except asyncio.TimeoutError:
            uptime = recorder.uptime_seconds
            logger.info(
                "Status: %d events | uptime %.0fs | bus %s",
                recorder.event_count, uptime, event_bus.stats,
            )

    logger.info("Shutting down...")
    if deribit:
        await deribit.stop()
    await feed.stop()
    await recorder.stop()
    await event_bus.stop()
    logger.info("Recording complete — %d events saved", recorder.event_count)


def main():
    parser = argparse.ArgumentParser(description="Record live market data")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--output", default="recorded_data")
    parser.add_argument("--no-options", action="store_true", help="Skip Deribit options")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    try:
        asyncio.run(run_recorder(args.symbol, args.output, not args.no_options))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
