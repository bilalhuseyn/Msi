from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """
    Async event bus bridging data feeds and signal modules.

    Feeds push raw events into the shared asyncio.Queue.
    The dispatcher reads them and fans out to registered handlers by event type.
    """

    def __init__(self, maxsize: int = 10_000):
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._running = False
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._dropped = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)
        logger.debug("Handler registered for '%s'", event_type)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._dispatch_loop(), name="event-bus")
        logger.info("EventBus started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            "EventBus stopped — processed=%d dropped=%d",
            self._processed, self._dropped,
        )

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            event_type = event.get("type", "")
            handlers = self._handlers.get(event_type, [])

            if not handlers:
                self._dropped += 1
                continue

            self._processed += 1
            for handler in handlers:
                try:
                    await handler(event)
                except Exception:
                    logger.error(
                        "Handler error for event '%s'", event_type, exc_info=True
                    )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "processed": self._processed,
            "dropped": self._dropped,
            "queue_size": self.queue.qsize(),
        }
