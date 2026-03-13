from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Exponential backoff with jitter for WebSocket reconnect."""

    def __init__(
        self,
        base: float = 1.0,
        factor: float = 2.0,
        max_delay: float = 60.0,
    ):
        self._base = base
        self._factor = factor
        self._max = max_delay
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    @property
    def delay(self) -> float:
        d = min(self._base * (self._factor ** self._attempt), self._max)
        self._attempt += 1
        return d

    async def wait(self) -> float:
        d = self.delay
        logger.info("Backoff wait %.1fs (attempt %d)", d, self._attempt)
        await asyncio.sleep(d)
        return d
