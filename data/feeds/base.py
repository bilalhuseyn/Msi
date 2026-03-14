from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field

import websockets
import websockets.exceptions

from config.constants import FeedStatus
from utils.reconnect import ExponentialBackoff

logger = logging.getLogger(__name__)


@dataclass
class FeedHealth:
    status: FeedStatus = FeedStatus.DISCONNECTED
    last_message_ts: float = 0.0
    message_count: int = 0
    reconnect_count: int = 0
    last_error: str | None = None

    @property
    def latency_ms(self) -> float:
        if self.last_message_ts == 0:
            return -1.0
        return (time.time() - self.last_message_ts) * 1000


class BaseFeed(abc.ABC):
    """Abstract WebSocket feed with auto-reconnect and health tracking."""

    def __init__(
        self,
        name: str,
        url: str,
        event_bus: asyncio.Queue,
        *,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
    ):
        self.name = name
        self.url = url
        self.event_bus = event_bus
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._backoff = ExponentialBackoff()
        self.health = FeedHealth()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name=f"feed-{self.name}")
        logger.info("[%s] Feed started", self.name)

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.health.status = FeedStatus.DISCONNECTED
        logger.info("[%s] Feed stopped", self.name)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                self.health.status = FeedStatus.CONNECTING
                async with websockets.connect(
                    self.url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                ) as ws:
                    self._ws = ws
                    self.health.status = FeedStatus.CONNECTED
                    self._backoff.reset()
                    logger.info("[%s] Connected to %s", self.name, self.url)

                    await self._on_connected(ws)

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=45.0)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "[%s] No message in 45s — forcing reconnect", self.name
                            )
                            break
                        self.health.last_message_ts = time.time()
                        self.health.message_count += 1
                        await self._handle_message(raw)

            except websockets.exceptions.ConnectionClosed as e:
                self.health.last_error = f"Connection closed: {e}"
                logger.warning("[%s] %s", self.name, self.health.last_error)
            except Exception as e:
                self.health.status = FeedStatus.ERROR
                self.health.last_error = str(e)
                logger.error("[%s] Feed error: %s", self.name, e, exc_info=True)

            if self._running:
                self.health.status = FeedStatus.RECONNECTING
                self.health.reconnect_count += 1
                await self._backoff.wait()

    @abc.abstractmethod
    async def _on_connected(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send subscription messages after connecting."""

    @abc.abstractmethod
    async def _handle_message(self, raw: str | bytes) -> None:
        """Parse raw message and push normalized data to event_bus."""

    async def _emit(self, event_type: str, data: dict) -> None:
        await self.event_bus.put({
            "source": self.name,
            "type": event_type,
            "ts": time.time(),
            "data": data,
        })
