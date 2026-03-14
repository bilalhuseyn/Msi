"""
Converts raw downloaded/recorded CSV data into BacktestTick sequences
that the sim engine can consume directly.

Handles four merge scenarios:
  1. aggTrades only            -> synthetic OB from trades
  2. OB snapshots only         -> no trade-level signals
  3. Full merge                -> recorded OB + recorded/downloaded trades
  4. Tardis book_snapshot_25   -> real 25-level OB depth from Tardis.dev
"""

from __future__ import annotations

import csv
import gzip
import logging
from pathlib import Path

from backtest.data_loader import BacktestTick, SyntheticGenerator

logger = logging.getLogger(__name__)


def trades_to_ticks(
    trades_dir: str | Path,
    *,
    bucket_ms: int = 60_000,
    ob_depth: int = 10,
    limit: int | None = None,
) -> list[BacktestTick]:
    """
    Convert a directory of downloaded aggTrade CSVs into BacktestTick sequence.

    Groups trades into time buckets (default 1 min), then builds a synthetic
    order book around the VWAP of each bucket.
    """
    trades_dir = Path(trades_dir)
    all_trades: list[dict] = []

    csv_files = sorted(trades_dir.glob("trades_*.csv"))
    if not csv_files:
        csv_files = sorted(trades_dir.glob("*.csv"))

    for f in csv_files:
        with open(f, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                all_trades.append({
                    "timestamp": float(row["timestamp"]),
                    "price": float(row["price"]),
                    "qty": float(row["qty"]),
                    "side": row["side"].strip().lower(),
                })
        if limit and len(all_trades) >= limit:
            all_trades = all_trades[:limit]
            break

    if not all_trades:
        logger.warning("No trades found in %s", trades_dir)
        return []

    all_trades.sort(key=lambda t: t["timestamp"])
    logger.info("Loaded %d trades from %d files", len(all_trades), len(csv_files))

    ts_scale = 1.0
    if all_trades[0]["timestamp"] > 1e12:
        ts_scale = 0.001
        logger.info("Detected millisecond timestamps — converting to seconds")

    ticks: list[BacktestTick] = []
    bucket_start = all_trades[0]["timestamp"]
    bucket_trades: list[dict] = []

    for trade in all_trades:
        if trade["timestamp"] - bucket_start >= bucket_ms:
            tick = _bucket_to_tick(bucket_start * ts_scale, bucket_trades, ob_depth, ts_scale)
            if tick:
                ticks.append(tick)
            bucket_start = trade["timestamp"]
            bucket_trades = []
        bucket_trades.append(trade)

    if bucket_trades:
        tick = _bucket_to_tick(bucket_start * ts_scale, bucket_trades, ob_depth, ts_scale)
        if tick:
            ticks.append(tick)

    logger.info("Created %d ticks from trade data", len(ticks))
    return ticks


def merge_ob_and_trades(
    ob_dir: str | Path,
    trades_dir: str | Path,
    *,
    ob_depth: int = 20,
    snap_interval_ms: int = 100,
) -> list[BacktestTick]:
    """
    Merge recorded order book snapshots with recorded/downloaded trades.

    Aligns each OB snapshot with trades that occurred in its time window.
    This gives the backtest engine the highest fidelity data possible.
    """
    ob_dir = Path(ob_dir)
    trades_dir = Path(trades_dir)

    ob_ticks = _load_recorded_ob(ob_dir, ob_depth)
    trades = _load_all_trades(trades_dir)

    if not ob_ticks:
        logger.warning("No OB data — falling back to trades_to_ticks")
        return trades_to_ticks(trades_dir, ob_depth=ob_depth)

    trades.sort(key=lambda t: t["timestamp"])
    trade_idx = 0

    merged: list[BacktestTick] = []
    for tick in ob_ticks:
        tick_trades = []
        while trade_idx < len(trades):
            t = trades[trade_idx]
            if t["timestamp"] < tick.timestamp:
                trade_idx += 1
                continue
            if t["timestamp"] > tick.timestamp + snap_interval_ms:
                break
            tick_trades.append(t)
            trade_idx += 1

        tick.trades = tick_trades
        merged.append(tick)

    logger.info(
        "Merged %d OB snapshots with trades (%d trades matched)",
        len(merged),
        sum(len(t.trades) for t in merged),
    )
    return merged


def _bucket_to_tick(
    ts: float,
    trades: list[dict],
    ob_depth: int,
    ts_scale: float = 1.0,
) -> BacktestTick | None:
    if not trades:
        return None

    total_value = sum(t["price"] * t["qty"] for t in trades)
    total_qty = sum(t["qty"] for t in trades)
    vwap = total_value / total_qty if total_qty > 0 else trades[0]["price"]

    spread = vwap * 0.0002
    best_bid = round(vwap - spread / 2, 2)
    best_ask = round(vwap + spread / 2, 2)

    buy_vol = sum(t["qty"] for t in trades if t["side"] == "buy")
    sell_vol = sum(t["qty"] for t in trades if t["side"] == "sell")
    base_qty = total_qty / 20

    total_vol = buy_vol + sell_vol
    if total_vol > 0:
        imbalance = (buy_vol - sell_vol) / total_vol
    else:
        imbalance = 0.0

    bid_base = base_qty * (1.0 + imbalance * 2.0)
    ask_base = base_qty * (1.0 - imbalance * 2.0)
    bid_base = max(bid_base, base_qty * 0.2)
    ask_base = max(ask_base, base_qty * 0.2)

    bids = SyntheticGenerator.generate_book_side(best_bid, bid_base, ob_depth, ascending=False)
    asks = SyntheticGenerator.generate_book_side(best_ask, ask_base, ob_depth, ascending=True)

    tick_trades = [
        {"price": t["price"], "qty": t["qty"], "side": t["side"],
         "timestamp": t["timestamp"] * ts_scale}
        for t in trades
    ]

    return BacktestTick(
        timestamp=ts, bids=bids, asks=asks, trades=tick_trades,
        best_bid=best_bid, best_ask=best_ask,
    )


def _load_recorded_ob(ob_dir: Path, depth: int) -> list[BacktestTick]:
    ticks: list[BacktestTick] = []
    csv_files = sorted(ob_dir.glob("orderbook_*.csv"))
    if not csv_files:
        csv_files = sorted(ob_dir.glob("*.csv"))

    for f in csv_files:
        with open(f, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ts = float(row.get("timestamp_ms", 0))
                bids, asks = [], []
                for j in range(1, depth + 1):
                    bp, bq = row.get(f"bid{j}_price"), row.get(f"bid{j}_qty")
                    ap, aq = row.get(f"ask{j}_price"), row.get(f"ask{j}_qty")
                    if bp and bq:
                        bids.append({"price": float(bp), "qty": float(bq)})
                    if ap and aq:
                        asks.append({"price": float(ap), "qty": float(aq)})

                best_bid = bids[0]["price"] if bids else 0.0
                best_ask = asks[0]["price"] if asks else 0.0
                ticks.append(BacktestTick(
                    timestamp=ts, bids=bids, asks=asks,
                    best_bid=best_bid, best_ask=best_ask,
                ))

    ticks.sort(key=lambda t: t.timestamp)
    return ticks


def _load_all_trades(trades_dir: Path) -> list[dict]:
    trades: list[dict] = []
    csv_files = sorted(trades_dir.glob("trades_*.csv"))
    if not csv_files:
        csv_files = sorted(trades_dir.glob("*.csv"))

    for f in csv_files:
        with open(f, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                trades.append({
                    "timestamp": float(row["timestamp"]),
                    "price": float(row["price"]),
                    "qty": float(row["qty"]),
                    "side": row["side"].strip().lower(),
                })
    return trades


# ---------------------------------------------------------------------------
#  Candle builders for MarketStructureReader
# ---------------------------------------------------------------------------

def load_klines_as_candles(
    klines_dir: str | Path,
    target_interval_sec: int = 900,
) -> list:
    """
    Load downloaded kline CSV files and aggregate into target-interval candles
    for the MarketStructureReader.

    Expects CSV columns: timestamp, open, high, low, close, volume
    Timestamps may be milliseconds (Binance download) or seconds.

    Args:
        klines_dir: directory containing kline_*.csv or *.csv files
        target_interval_sec: target candle interval (900 = 15 minutes)

    Returns:
        list[Candle] sorted by timestamp ascending
    """
    from signals.market_structure import Candle

    klines_dir = Path(klines_dir)
    raw: list[dict] = []

    csv_files = sorted(klines_dir.glob("klines_*.csv"))
    if not csv_files:
        csv_files = sorted(klines_dir.glob("*.csv"))

    for f in csv_files:
        with open(f, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    ts = float(row["timestamp"])
                    if ts > 1e12:
                        ts /= 1000.0  # ms -> seconds
                    raw.append({
                        "ts": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume", 0.0)),
                    })
                except (ValueError, KeyError):
                    continue

    if not raw:
        logger.warning("No kline data found in %s", klines_dir)
        return []

    raw.sort(key=lambda r: r["ts"])
    logger.info("Loaded %d raw klines from %s", len(raw), klines_dir)

    # Aggregate source klines into target_interval_sec windows
    candles: list = []
    bucket_start = (int(raw[0]["ts"]) // target_interval_sec) * target_interval_sec
    bucket: list[dict] = []

    def _flush(start_ts: int, rows: list[dict]) -> None:
        if not rows:
            return
        candles.append(Candle(
            timestamp=float(start_ts),
            open=rows[0]["open"],
            high=max(r["high"] for r in rows),
            low=min(r["low"] for r in rows),
            close=rows[-1]["close"],
            volume=sum(r["volume"] for r in rows),
        ))

    for row in raw:
        candle_start = (int(row["ts"]) // target_interval_sec) * target_interval_sec
        if candle_start != bucket_start:
            _flush(bucket_start, bucket)
            bucket_start = candle_start
            bucket = []
        bucket.append(row)

    _flush(bucket_start, bucket)

    logger.info(
        "Aggregated %d klines -> %d x %ds candles",
        len(raw), len(candles), target_interval_sec,
    )
    return candles


# ---------------------------------------------------------------------------
#  Tardis.dev book_snapshot_25 converter
# ---------------------------------------------------------------------------

def tardis_ob_to_ticks(
    data_dir: str | Path,
    *,
    depth: int = 20,
    sample_every: int = 100,
    limit: int | None = None,
) -> list[BacktestTick]:
    """
    Convert Tardis book_snapshot_25 + trades .csv.gz files to BacktestTick list.

    Args:
        data_dir: directory containing .csv.gz files from Tardis download
        depth: number of OB levels to keep (max 25)
        sample_every: take every Nth OB snapshot (1=all ticks, 100=~1 per 100)
        limit: total ticks cap (None = unlimited)

    Returns:
        Sorted list of BacktestTick with real OB depth + matched trades.
    """
    data_dir = Path(data_dir)

    snapshot_files = sorted(data_dir.glob("*book_snapshot_25*.csv.gz"))
    trade_files = sorted(data_dir.glob("*trades*.csv.gz"))

    if not snapshot_files:
        logger.error("No book_snapshot_25 files found in %s", data_dir)
        return []

    logger.info(
        "Found %d snapshot files, %d trade files in %s",
        len(snapshot_files), len(trade_files), data_dir,
    )

    trades_by_sec = _load_tardis_trades(trade_files)

    ticks: list[BacktestTick] = []
    total_rows = 0
    skipped = 0

    for sf in snapshot_files:
        logger.info("Processing %s ...", sf.name)
        with gzip.open(sf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_rows += 1

                if total_rows % sample_every != 0:
                    continue

                tick = _parse_tardis_snapshot(row, depth, trades_by_sec)
                if tick is None:
                    skipped += 1
                    continue

                ticks.append(tick)

                if limit and len(ticks) >= limit:
                    break

        if limit and len(ticks) >= limit:
            break

    ticks.sort(key=lambda t: t.timestamp)

    logger.info(
        "Tardis OB -> %d ticks (from %d rows, sample_every=%d, skipped=%d)",
        len(ticks), total_rows, sample_every, skipped,
    )

    if ticks:
        span_h = (ticks[-1].timestamp - ticks[0].timestamp) / 3600
        logger.info(
            "Time span: %.1f hours | Price range: %.2f - %.2f",
            span_h,
            min(t.mid_price for t in ticks),
            max(t.mid_price for t in ticks),
        )

    return ticks


def _load_tardis_trades(trade_files: list[Path]) -> dict[int, list[dict]]:
    """Load Tardis trade CSVs and index by second for fast lookup."""
    trades_by_sec: dict[int, list[dict]] = {}
    total = 0

    for tf in trade_files:
        logger.info("Loading trades: %s", tf.name)
        with gzip.open(tf, "rt", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_us = int(row["timestamp"])
                    ts_sec = ts_us // 1_000_000
                    sec_key = ts_sec

                    trades_by_sec.setdefault(sec_key, []).append({
                        "price": float(row["price"]),
                        "qty": float(row["amount"]),
                        "side": row["side"].strip().lower(),
                        "timestamp": ts_us / 1_000_000,
                    })
                    total += 1
                except (ValueError, KeyError):
                    continue

    logger.info("Indexed %d trades across %d seconds", total, len(trades_by_sec))
    return trades_by_sec


def _parse_tardis_snapshot(
    row: dict,
    depth: int,
    trades_by_sec: dict[int, list[dict]],
) -> BacktestTick | None:
    """Parse one Tardis book_snapshot_25 CSV row into a BacktestTick."""
    try:
        ts_us = int(row.get("timestamp", 0))
    except (ValueError, TypeError):
        return None

    ts_sec = ts_us / 1_000_000
    depth = min(depth, 25)

    bids = []
    asks = []
    for i in range(depth):
        bp = row.get(f"bids[{i}].price", "")
        bq = row.get(f"bids[{i}].amount", "")
        ap = row.get(f"asks[{i}].price", "")
        aq = row.get(f"asks[{i}].amount", "")

        if bp and bq:
            try:
                bids.append({"price": float(bp), "qty": float(bq)})
            except ValueError:
                pass
        if ap and aq:
            try:
                asks.append({"price": float(ap), "qty": float(aq)})
            except ValueError:
                pass

    if len(bids) < 3 or len(asks) < 3:
        return None

    sec_key = int(ts_sec)
    matched_trades = trades_by_sec.get(sec_key, [])

    return BacktestTick(
        timestamp=ts_sec,
        bids=bids,
        asks=asks,
        trades=matched_trades,
        best_bid=bids[0]["price"],
        best_ask=asks[0]["price"],
    )
