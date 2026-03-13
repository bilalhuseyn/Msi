from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from config.constants import FeedStatus
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class OptionData:
    instrument_name: str
    option_type: str
    strike: float
    open_interest: float
    gamma: float
    delta: float
    iv: float
    best_bid: float
    best_ask: float


@dataclass
class DeribitHealth:
    status: FeedStatus = FeedStatus.DISCONNECTED
    last_fetch_ts: float = 0.0
    fetch_count: int = 0
    last_error: str | None = None


class DeribitPoller:
    """
    REST poller for Deribit public options data.
    Fetches options chain every `interval_sec` seconds (default 300 = 5 min).
    """

    def __init__(
        self,
        event_bus: asyncio.Queue,
        settings: Settings | None = None,
    ):
        s = settings or Settings()
        self._cfg = s.deribit
        self._interval = s.options.update_interval_sec
        self.event_bus = event_bus
        self.health = DeribitHealth()
        self._running = False
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._cache: list[OptionData] = []
        self._cache_ts: float = 0.0

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._poll_loop(), name="deribit-poller")
        logger.info("[deribit] Poller started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        self.health.status = FeedStatus.DISCONNECTED
        logger.info("[deribit] Poller stopped")

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_options_chain()
                self.health.status = FeedStatus.CONNECTED
                self.health.last_fetch_ts = time.time()
                self.health.fetch_count += 1
            except Exception as e:
                self.health.status = FeedStatus.ERROR
                self.health.last_error = str(e)
                logger.error("[deribit] Fetch error: %s", e, exc_info=True)
            await asyncio.sleep(self._interval)

    async def _fetch_options_chain(self) -> None:
        assert self._session is not None
        url = (
            f"{self._cfg.rest_base_url}/api/v2/public/"
            f"get_book_summary_by_currency"
        )
        params = {"currency": self._cfg.currency, "kind": "option"}

        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            body = await resp.json()

        raw_options = body.get("result", [])
        options: list[OptionData] = []

        for opt in raw_options:
            instrument = opt.get("instrument_name", "")
            parts = instrument.split("-")
            if len(parts) < 4:
                continue

            option_type = "call" if parts[-1] == "C" else "put"
            try:
                strike = float(parts[-2])
            except ValueError:
                continue

            greeks = opt.get("greeks", {})
            options.append(OptionData(
                instrument_name=instrument,
                option_type=option_type,
                strike=strike,
                open_interest=float(opt.get("open_interest", 0)),
                gamma=float(greeks.get("gamma", 0)),
                delta=float(greeks.get("delta", 0)),
                iv=float(opt.get("mark_iv", 0)),
                best_bid=float(opt.get("bid_price", 0) or 0),
                best_ask=float(opt.get("ask_price", 0) or 0),
            ))

        self._cache = options
        self._cache_ts = time.time()

        await self.event_bus.put({
            "source": "deribit",
            "type": "options_chain",
            "ts": time.time(),
            "data": {
                "currency": self._cfg.currency,
                "options": [
                    {
                        "instrument_name": o.instrument_name,
                        "type": o.option_type,
                        "strike": o.strike,
                        "open_interest": o.open_interest,
                        "gamma": o.gamma,
                        "delta": o.delta,
                        "iv": o.iv,
                    }
                    for o in options
                ],
                "count": len(options),
            },
        })
        logger.info("[deribit] Fetched %d options", len(options))

    @property
    def cached_options(self) -> list[OptionData]:
        return self._cache

    @property
    def cache_age_sec(self) -> float:
        if self._cache_ts == 0:
            return float("inf")
        return time.time() - self._cache_ts
