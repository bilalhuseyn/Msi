from __future__ import annotations

import asyncio
import logging

import orjson
import websockets

from config.settings import Settings
from data.feeds.base import BaseFeed

logger = logging.getLogger(__name__)


class BinanceFeed(BaseFeed):
    """
    Binance combined stream: depth20@100ms, aggTrade, bookTicker.
    Produces normalised order_book, trade, and ticker events.
    """

    def __init__(
        self,
        symbol: str,
        event_bus: asyncio.Queue,
        settings: Settings | None = None,
    ):
        cfg = (settings or Settings()).binance
        sym = symbol.lower()
        streams = [
            f"{sym}@depth{cfg.depth_levels}@{cfg.depth_update_ms}ms",
            f"{sym}@aggTrade",
            f"{sym}@bookTicker",
        ]
        # Binance combined stream endpoint — returns {"stream": "...", "data": {...}}
        # Must use /stream?streams= (not /ws/s1/s2) for multi-stream wrapper format
        base = cfg.ws_base_url.replace("/ws", "")
        url = f"{base}/stream?streams={'/'.join(streams)}"
        super().__init__(name=f"binance-{symbol}", url=url, event_bus=event_bus)
        self.symbol = symbol.upper()

    async def _on_connected(self, ws: websockets.WebSocketClientProtocol) -> None:
        logger.info("[%s] Subscribed via combined stream URL", self.name)

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = orjson.loads(raw)
        except Exception:
            logger.warning("[%s] Failed to parse message", self.name)
            return

        if "stream" not in msg:
            return
        stream: str = msg["stream"]
        data = msg.get("data", {})

        if "@depth" in stream:
            await self._handle_depth(data)
        elif "@aggTrade" in stream:
            await self._handle_agg_trade(data)
        elif "@bookTicker" in stream:
            await self._handle_book_ticker(data)

    async def _handle_depth(self, data: dict) -> None:
        bids = [{"price": float(p), "qty": float(q)} for p, q in data.get("bids", [])]
        asks = [{"price": float(p), "qty": float(q)} for p, q in data.get("asks", [])]
        await self._emit("order_book", {
            "symbol": self.symbol,
            "exchange": "binance",
            "bids": bids,
            "asks": asks,
            "update_id": data.get("lastUpdateId"),
        })

    async def _handle_agg_trade(self, data: dict) -> None:
        await self._emit("trade", {
            "symbol": self.symbol,
            "exchange": "binance",
            "price": float(data["p"]),
            "qty": float(data["q"]),
            "side": "sell" if data.get("m") else "buy",
            "timestamp_ms": data.get("T", 0),
            "trade_id": data.get("a"),
        })

    async def _handle_book_ticker(self, data: dict) -> None:
        await self._emit("ticker", {
            "symbol": self.symbol,
            "exchange": "binance",
            "best_bid": float(data["b"]),
            "best_bid_qty": float(data["B"]),
            "best_ask": float(data["a"]),
            "best_ask_qty": float(data["A"]),
        })
