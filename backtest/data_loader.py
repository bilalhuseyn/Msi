from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OrderBookLevel:
    price: float
    qty: float


@dataclass
class TradeEvent:
    timestamp: float
    price: float
    qty: float
    side: str  # "buy" | "sell"


@dataclass
class BacktestTick:
    """Single point-in-time snapshot used by the simulation engine."""

    timestamp: float
    bids: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    best_bid: float = 0.0
    best_ask: float = 0.0
    options_chain: list[dict] = field(default_factory=list)

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2
        if self.bids and self.asks:
            return (self.bids[0]["price"] + self.asks[0]["price"]) / 2
        return 0.0


class DataLoader:
    """Loads historical market data from CSV files into BacktestTick sequences."""

    @staticmethod
    def load_klines(filepath: str | Path, *, limit: int | None = None) -> list[BacktestTick]:
        """
        Load OHLCV kline data and convert to BacktestTick sequence.
        Expected CSV columns: timestamp, open, high, low, close, volume
        Each kline generates one tick with a synthetic order book.
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Kline file not found: {path}")

        ticks: list[BacktestTick] = []
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if limit is not None and i >= limit:
                    break
                tick = DataLoader._kline_to_tick(row)
                if tick is not None:
                    ticks.append(tick)
        return ticks

    @staticmethod
    def load_trades(filepath: str | Path, *, limit: int | None = None) -> list[TradeEvent]:
        """
        Load raw trade data.
        Expected CSV columns: timestamp, price, qty, side
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Trades file not found: {path}")

        events: list[TradeEvent] = []
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if limit is not None and i >= limit:
                    break
                events.append(TradeEvent(
                    timestamp=float(row["timestamp"]),
                    price=float(row["price"]),
                    qty=float(row["qty"]),
                    side=row["side"].strip().lower(),
                ))
        return events

    @staticmethod
    def load_orderbook_snapshots(
        filepath: str | Path,
        depth: int = 10,
        *,
        limit: int | None = None,
    ) -> list[BacktestTick]:
        """
        Load order book snapshot CSV.
        Expected columns: timestamp, bid1_price, bid1_qty, ..., ask1_price, ask1_qty, ...
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Orderbook file not found: {path}")

        ticks: list[BacktestTick] = []
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if limit is not None and i >= limit:
                    break
                ts = float(row["timestamp"])
                bids, asks = [], []
                for j in range(1, depth + 1):
                    bp_key, bq_key = f"bid{j}_price", f"bid{j}_qty"
                    ap_key, aq_key = f"ask{j}_price", f"ask{j}_qty"
                    if bp_key in row and bq_key in row:
                        bids.append({"price": float(row[bp_key]), "qty": float(row[bq_key])})
                    if ap_key in row and aq_key in row:
                        asks.append({"price": float(row[ap_key]), "qty": float(row[aq_key])})

                best_bid = bids[0]["price"] if bids else 0.0
                best_ask = asks[0]["price"] if asks else 0.0
                ticks.append(BacktestTick(
                    timestamp=ts, bids=bids, asks=asks,
                    best_bid=best_bid, best_ask=best_ask,
                ))
        return ticks

    @staticmethod
    def _kline_to_tick(row: dict) -> BacktestTick | None:
        try:
            ts = float(row["timestamp"])
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            vol = float(row["volume"])
        except (KeyError, ValueError):
            return None

        mid = (o + c) / 2
        spread = max(mid * 0.0002, 0.01)
        best_bid = mid - spread / 2
        best_ask = mid + spread / 2

        bids = SyntheticGenerator.generate_book_side(best_bid, vol / 20, depth=10, ascending=False)
        asks = SyntheticGenerator.generate_book_side(best_ask, vol / 20, depth=10, ascending=True)

        buy_vol = vol * (0.45 + 0.1 * (c - o) / max(h - l, 0.01))
        buy_vol = max(0.01, min(buy_vol, vol))
        sell_vol = vol - buy_vol
        n_trades = max(2, int(vol / max(mid * 0.01, 1)))
        n_trades = min(n_trades, 20)

        trades = SyntheticGenerator.distribute_trades(ts, mid, buy_vol, sell_vol, n_trades)

        return BacktestTick(
            timestamp=ts, bids=bids, asks=asks, trades=trades,
            best_bid=best_bid, best_ask=best_ask,
        )


class SyntheticGenerator:
    """Generates realistic synthetic market data for testing."""

    @staticmethod
    def generate_book_side(
        anchor_price: float,
        base_qty: float,
        depth: int = 10,
        ascending: bool = True,
        noise: float = 0.15,
    ) -> list[dict]:
        levels = []
        step = anchor_price * 0.00005
        for i in range(depth):
            offset = step * (i + 1) if ascending else -step * (i + 1)
            price = anchor_price + offset
            qty = base_qty * (1 + random.uniform(-noise, noise)) / (1 + i * 0.1)
            levels.append({"price": round(price, 2), "qty": round(max(qty, 0.001), 4)})
        return levels

    @staticmethod
    def distribute_trades(
        ts: float, mid: float, buy_vol: float, sell_vol: float, n_trades: int,
    ) -> list[dict]:
        trades = []
        if n_trades <= 0:
            return trades
        n_buys = max(1, int(n_trades * buy_vol / max(buy_vol + sell_vol, 1e-9)))
        n_sells = max(1, n_trades - n_buys)

        for _ in range(n_buys):
            trades.append({
                "price": round(mid + random.uniform(0, mid * 0.0003), 2),
                "qty": round(buy_vol / n_buys * random.uniform(0.5, 1.5), 4),
                "side": "buy",
                "timestamp": ts + random.uniform(0, 0.9),
            })
        for _ in range(n_sells):
            trades.append({
                "price": round(mid - random.uniform(0, mid * 0.0003), 2),
                "qty": round(sell_vol / n_sells * random.uniform(0.5, 1.5), 4),
                "side": "sell",
                "timestamp": ts + random.uniform(0, 0.9),
            })
        trades.sort(key=lambda t: t["timestamp"])
        return trades

    @staticmethod
    def generate_random_walk(
        n_ticks: int = 500,
        start_price: float = 50_000.0,
        volatility: float = 0.002,
        tick_interval: float = 60.0,
        base_volume: float = 10.0,
        trend: float = 0.0,
        seed: int | None = None,
    ) -> list[BacktestTick]:
        """
        Generate a sequence of BacktestTicks following a random walk.
        Useful for integration tests and demo runs.
        """
        if seed is not None:
            random.seed(seed)

        ticks: list[BacktestTick] = []
        price = start_price
        ts = 1_700_000_000.0

        for _ in range(n_ticks):
            ret = random.gauss(trend, volatility)
            price *= (1 + ret)
            price = max(price, 1.0)

            vol = base_volume * random.uniform(0.5, 2.0)
            spread = price * 0.0002
            best_bid = round(price - spread / 2, 2)
            best_ask = round(price + spread / 2, 2)
            mid = (best_bid + best_ask) / 2

            bids = SyntheticGenerator.generate_book_side(best_bid, vol / 20, 10, ascending=False)
            asks = SyntheticGenerator.generate_book_side(best_ask, vol / 20, 10, ascending=True)

            buy_ratio = 0.5 + 0.3 * ret / max(volatility, 1e-9)
            buy_ratio = max(0.2, min(0.8, buy_ratio))
            buy_vol = vol * buy_ratio
            sell_vol = vol - buy_vol
            n_trades = random.randint(3, 12)
            trades = SyntheticGenerator.distribute_trades(ts, mid, buy_vol, sell_vol, n_trades)

            ticks.append(BacktestTick(
                timestamp=ts, bids=bids, asks=asks, trades=trades,
                best_bid=best_bid, best_ask=best_ask,
            ))
            ts += tick_interval

        return ticks
