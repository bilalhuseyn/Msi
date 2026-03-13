from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import aiosqlite
from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from config.settings import Settings

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parent.parent / "data"
SQLITE_PATH = DB_DIR / "ofi_pro.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    entry_price REAL,
    exit_price  REAL,
    size        REAL,
    pnl         REAL,
    score       REAL,
    status      TEXT    DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS signal_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    symbol      TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    score       REAL,
    veto_reason TEXT,
    raw_signals TEXT,
    regime      TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date        TEXT    PRIMARY KEY,
    total_pnl   REAL    DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    win_count   INTEGER DEFAULT 0,
    loss_count  INTEGER DEFAULT 0,
    max_drawdown REAL   DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_signal_logs_ts ON signal_logs(ts);
"""


class InfluxWriter:
    """Async batched writer for InfluxDB time-series data."""

    def __init__(self, settings: Settings | None = None):
        cfg = (settings or Settings()).influxdb
        self._url = cfg.url
        self._token = cfg.token
        self._org = cfg.org
        self._bucket = cfg.bucket
        self._client: InfluxDBClientAsync | None = None
        self._batch: list[Point] = []
        self._batch_size = 100
        self._flush_interval = 5.0
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._client = InfluxDBClientAsync(
            url=self._url, token=self._token, org=self._org,
        )
        self._running = True
        self._task = asyncio.create_task(self._flush_loop(), name="influx-flusher")
        logger.info("InfluxWriter started (url=%s)", self._url)

    async def stop(self) -> None:
        self._running = False
        await self._flush()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
        logger.info("InfluxWriter stopped")

    async def write_metric(
        self,
        measurement: str,
        tags: dict[str, str],
        fields: dict[str, float],
        ts: float | None = None,
    ) -> None:
        point = Point(measurement)
        for k, v in tags.items():
            point.tag(k, v)
        for k, v in fields.items():
            point.field(k, float(v))
        if ts:
            point.time(int(ts * 1e9))

        async with self._lock:
            self._batch.append(point)
            if len(self._batch) >= self._batch_size:
                await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._batch or not self._client:
                return
            points = self._batch[:]
            self._batch.clear()

        try:
            write_api = self._client.write_api()
            await write_api.write(bucket=self._bucket, record=points)
            logger.debug("Flushed %d points to InfluxDB", len(points))
        except Exception:
            logger.error("InfluxDB write failed", exc_info=True)
            async with self._lock:
                self._batch = points + self._batch

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            await self._flush()


class SQLiteStore:
    """Async SQLite store for trade records, signal logs, and daily stats."""

    def __init__(self, db_path: Path | None = None):
        self._path = str(db_path or SQLITE_PATH)
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("SQLiteStore started (path=%s)", self._path)

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
        logger.info("SQLiteStore stopped")

    async def log_signal(
        self,
        symbol: str,
        action: str,
        score: float,
        veto_reason: str | None,
        raw_signals: dict,
        regime: dict | None,
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO signal_logs (ts, symbol, action, score, veto_reason, raw_signals, regime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), symbol, action, score,
                veto_reason,
                json.dumps(raw_signals),
                json.dumps(regime) if regime else None,
            ),
        )
        await self._db.commit()

    async def log_trade(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        size: float,
        score: float,
    ) -> int:
        assert self._db is not None
        cursor = await self._db.execute(
            "INSERT INTO trades (ts, symbol, direction, entry_price, size, score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), symbol, direction, entry_price, size, score),
        )
        await self._db.commit()
        return cursor.lastrowid or 0

    async def close_trade(
        self, trade_id: int, exit_price: float, pnl: float
    ) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE trades SET exit_price=?, pnl=?, status='closed' WHERE id=?",
            (exit_price, pnl, trade_id),
        )
        await self._db.commit()

    async def get_daily_pnl(self, date_str: str) -> float:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE date(ts, 'unixepoch') = ? AND status='closed'",
            (date_str,),
        )
        row = await cursor.fetchone()
        return float(row[0]) if row else 0.0

    async def get_trade_count_today(self, date_str: str) -> int:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM trades WHERE date(ts, 'unixepoch') = ?",
            (date_str,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def get_recent_trades(self, limit: int = 50) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, ts, symbol, direction, entry_price, exit_price, pnl, score, status "
            "FROM trades ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]
