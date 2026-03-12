from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-parseable logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": time.time(),
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            log_entry["data"] = record.extra_data
        return json.dumps(log_entry)


def setup_logging(
    level: int = logging.INFO,
    json_file: bool = True,
    console: bool = True,
) -> None:
    """Configure root logger with console and optional JSON file handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-25s | %(message)s",
            datefmt="%H:%M:%S",
        )
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    if json_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(LOG_DIR / "ofi_pro.jsonl")
        file_handler.setLevel(level)
        file_handler.setFormatter(JSONFormatter())
        root.addHandler(file_handler)

    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("influxdb_client").setLevel(logging.WARNING)


class SignalLogger:
    """Dedicated logger for signal decisions with structured metadata."""

    def __init__(self):
        self._logger = logging.getLogger("ofi.signals")

    def log_decision(
        self,
        symbol: str,
        action: str,
        score: float,
        signals: dict,
        regime: dict | None = None,
        veto_reason: str | None = None,
    ) -> None:
        msg = f"[{symbol}] {action} score={score:.4f}"
        if veto_reason:
            msg += f" VETO={veto_reason}"

        extra = {
            "symbol": symbol,
            "action": action,
            "score": score,
            "signals": signals,
            "regime": regime,
            "veto_reason": veto_reason,
        }
        record = self._logger.makeRecord(
            self._logger.name, logging.INFO, "", 0, msg, (), None,
        )
        record.extra_data = extra
        self._logger.handle(record)

    def log_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        size: float,
        stop: float,
        tp1: float,
    ) -> None:
        self._logger.info(
            "[%s] TRADE %s entry=%.2f size=%.6f stop=%.2f tp1=%.2f",
            symbol, direction, entry_price, size, stop, tp1,
        )

    def log_veto(self, symbol: str, reason: str, details: dict) -> None:
        self._logger.warning(
            "[%s] GLOBAL VETO — %s | %s", symbol, reason, details,
        )
