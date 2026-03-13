"""
OBI Pro — CLI Entry Point

Usage examples:
  python -m obi_pro                          # mainnet, paper trading, BTC+ETH
  python -m obi_pro --testnet                # Bybit testnet WebSocket
  python -m obi_pro --testnet --symbol BTCUSDT --balance 10000
  python -m obi_pro --no-paper               # disable PaperTrader (observe only)
  python -m obi_pro --log-level DEBUG        # verbose logging
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from utils.logging import setup_logging


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="obi_pro",
        description="OBI Pro — Order Book Imbalance algorithmic trading bot",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        default=False,
        help="Connect to Bybit testnet (wss://stream-testnet.bybit.com)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Override trading symbol(s), comma-separated (e.g. BTCUSDT or BTCUSDT,ETHUSDT)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=10_000.0,
        help="Starting paper trading balance in USD (default: 10000)",
    )
    parser.add_argument(
        "--no-paper",
        action="store_true",
        default=False,
        help="Disable PaperTrader — engine runs in observation-only mode",
    )
    parser.add_argument(
        "--paper-log",
        type=str,
        default=None,
        help="Path for JSONL paper trade log (default: logs/paper_trades.jsonl)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    # Lazy import to keep startup fast
    from config.settings import Settings
    from core.engine import OFIEngine

    # --- Apply CLI overrides to environment before loading Settings ---
    if args.testnet:
        os.environ["BYBIT_TESTNET"] = "true"
        logging.getLogger(__name__).info(
            "⚠️  TESTNET MODE — connecting to stream-testnet.bybit.com"
        )

    settings = Settings()

    if args.symbol:
        symbols = [s.strip().upper() for s in args.symbol.split(",")]
        settings = settings.model_copy(update={"symbols": symbols})

    engine = OFIEngine(
        settings=settings,
        paper_trading=not args.no_paper,
        initial_balance=args.balance,
        paper_log_file=args.paper_log,
    )

    loop = asyncio.get_running_loop()

    # Graceful shutdown on SIGINT / SIGTERM
    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logging.getLogger(__name__).info("Shutdown signal received — stopping engine...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows fallback: KeyboardInterrupt handled below
            pass

    await engine.start()

    logger = logging.getLogger(__name__)
    mode = "TESTNET" if args.testnet else "MAINNET"
    paper = "paper-trading" if not args.no_paper else "observe-only"
    logger.info(
        "OBI Pro running | mode=%s | symbols=%s | balance=%.0f | %s",
        mode, settings.symbols, args.balance, paper,
    )
    logger.info("Press Ctrl+C to stop.")

    try:
        while not stop_event.is_set():
            await engine.run_dashboard_tick()
            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — stopping engine...")
    finally:
        await engine.stop()


def main() -> None:
    args = _parse_args()
    setup_logging(level=args.log_level)
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        logging.getLogger(__name__).critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
