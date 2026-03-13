from __future__ import annotations

import asyncio
import logging

import orjson
import websockets

from config.settings import Settings
from data.feeds.base import BaseFeed

logger = logging.getLogger(__name__)


class BybitFeed(BaseFeed):
    """
    Bybit V5 public linear feed: orderbook.200 + publicTrade.
    Used for cross-validation with Binance data.
    """

    def __init__(
        self,
        symbol: str,
        event_bus: asyncio.Queue,
        settings: Settings | None = None,
    ):
        cfg = (settings or Settings()).bybit
        super().__init__(
            name=f"bybit-{symbol}",
            url=cfg.ws_base_url,
            event_bus=event_bus,
            ping_interval=20.0,
        )
        self.symbol = symbol.upper()
        self._cfg = cfg
        self._ob_snapshot: dict | None = None

    async def _on_connected(self, ws: websockets.WebSocketClientProtocol) -> None:
        subscribe_msg = orjson.dumps({
            "op": "subscribe",
            "args": [
                f"orderbook.{self._cfg.orderbook_depth}.{self.symbol}",
                f"publicTrade.{self.symbol}",
            ],
        }).decode()
        await ws.send(subscribe_msg)
        logger.info("[%s] Subscribed to orderbook + publicTrade", self.name)

    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = orjson.loads(raw)
        except Exception:
            return

        topic = msg.get("topic", "")
        data = msg.get("data", {})
        msg_type = msg.get("type", "")

        if "orderbook" in topic:
            await self._handle_orderbook(data, msg_type)
        elif "publicTrade" in topic:
            await self._handle_trades(data)
        elif msg.get("op") == "pong" or msg.get("ret_msg") == "pong":
            return

    async def _handle_orderbook(self, data: dict, msg_type: str) -> None:
        if msg_type == "snapshot":
            self._ob_snapshot = data
        elif msg_type == "delta" and self._ob_snapshot:
            for side in ("b", "a"):
                updates = data.get(side, [])
                snap_side = self._ob_snapshot.get(side, [])
                for update in updates:
                    price, qty = update[0], update[1]
                    found = False
                    for i, level in enumerate(snap_side):
                        if level[0] == price:
                            if float(qty) == 0:
                                snap_side.pop(i)
                            else:
                                snap_side[i] = update
                            found = True
                            break
                    if not found and float(qty) > 0:
                        snap_side.append(update)

        if self._ob_snapshot:
            bids_raw = self._ob_snapshot.get("b", [])
            asks_raw = self._ob_snapshot.get("a", [])
            bids = sorted(
                [{"price": float(b[0]), "qty": float(b[1])} for b in bids_raw],
                key=lambda x: -x["price"],
            )
            asks = sorted(
                [{"price": float(a[0]), "qty": float(a[1])} for a in asks_raw],
                key=lambda x: x["price"],
            )
            await self._emit("order_book", {
                "symbol": self.symbol,
                "exchange": "bybit",
                "bids": bids[:20],
                "asks": asks[:20],
                "update_id": data.get("u"),
            })

    async def _handle_trades(self, data: list | dict) -> None:
        trades = data if isinstance(data, list) else [data]
        for t in trades:
            await self._emit("trade", {
                "symbol": self.symbol,
                "exchange": "bybit",
                "price": float(t.get("p", 0)),
                "qty": float(t.get("v", 0)),
                "side": t.get("S", "Buy").lower(),
                "timestamp_ms": t.get("T", 0),
                "trade_id": t.get("i"),
            })
