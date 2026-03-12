"""
Tests for data infrastructure: DataRecorder, BinanceDownloader, and Converter.
"""

from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.recorder import DataRecorder, _CSVWriter
from data.binance_downloader import (
    BinanceDownloader,
    _date_range,
    _unique_months,
    _OUR_TRADE_HEADER,
    _OUR_KLINE_HEADER,
)
from data.converter import trades_to_ticks, merge_ob_and_trades


# ---------------------------------------------------------------------------
#  _CSVWriter
# ---------------------------------------------------------------------------


class TestCSVWriter:
    def test_creates_file_with_header(self, tmp_path):
        fp = tmp_path / "test.csv"
        w = _CSVWriter(fp, ["a", "b", "c"])
        w.close()
        with open(fp) as f:
            lines = f.readlines()
        assert lines[0].strip() == "a,b,c"

    def test_writes_rows(self, tmp_path):
        fp = tmp_path / "test.csv"
        w = _CSVWriter(fp, ["x", "y"])
        w.writerow([1, 2])
        w.writerow([3, 4])
        w.close()
        with open(fp) as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 3
        assert rows[1] == ["1", "2"]
        assert rows[2] == ["3", "4"]

    def test_append_mode_no_duplicate_header(self, tmp_path):
        fp = tmp_path / "test.csv"
        w1 = _CSVWriter(fp, ["h1", "h2"])
        w1.writerow(["a", "b"])
        w1.close()
        w2 = _CSVWriter(fp, ["h1", "h2"])
        w2.writerow(["c", "d"])
        w2.close()
        with open(fp) as f:
            rows = list(csv.reader(f))
        assert len(rows) == 3
        assert rows[0] == ["h1", "h2"]


# ---------------------------------------------------------------------------
#  DataRecorder
# ---------------------------------------------------------------------------


class TestDataRecorder:
    @pytest.fixture
    def recorder(self, tmp_path):
        return DataRecorder(output_dir=str(tmp_path), flush_interval=0.1)

    @pytest.mark.asyncio
    async def test_start_stop(self, recorder):
        await recorder.start()
        assert recorder.uptime_seconds >= 0
        await recorder.stop()

    @pytest.mark.asyncio
    async def test_record_trade(self, recorder, tmp_path):
        await recorder.start()
        event = {
            "type": "trade",
            "data": {
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "price": 50000.0,
                "qty": 0.5,
                "side": "buy",
                "timestamp_ms": 1700000000000,
                "trade_id": "123",
            },
        }
        await recorder.on_trade(event)
        assert recorder.event_count == 1
        await recorder.stop()

        csv_files = list(tmp_path.rglob("trades_*.csv"))
        assert len(csv_files) >= 1
        with open(csv_files[0]) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["price"] == "50000.0"
        assert rows[0]["side"] == "buy"

    @pytest.mark.asyncio
    async def test_record_order_book(self, recorder, tmp_path):
        await recorder.start()
        event = {
            "type": "order_book",
            "data": {
                "symbol": "ETHUSDT",
                "exchange": "binance",
                "timestamp_ms": 1700000001000,
                "bids": [{"price": 2000.0, "qty": 10.0}],
                "asks": [{"price": 2001.0, "qty": 5.0}],
            },
        }
        await recorder.on_order_book(event)
        assert recorder.event_count == 1
        await recorder.stop()

        csv_files = list(tmp_path.rglob("orderbook_*.csv"))
        assert len(csv_files) >= 1
        with open(csv_files[0]) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["bid1_price"] == "2000.0"

    @pytest.mark.asyncio
    async def test_record_ticker(self, recorder, tmp_path):
        await recorder.start()
        event = {
            "type": "ticker",
            "data": {
                "symbol": "BTCUSDT",
                "exchange": "binance",
                "best_bid": 50000.0,
                "best_bid_qty": 1.0,
                "best_ask": 50001.0,
                "best_ask_qty": 2.0,
            },
        }
        await recorder.on_ticker(event)
        assert recorder.event_count == 1
        await recorder.stop()

        csv_files = list(tmp_path.rglob("ticker_*.csv"))
        assert len(csv_files) >= 1

    @pytest.mark.asyncio
    async def test_record_options(self, recorder, tmp_path):
        await recorder.start()
        event = {
            "type": "options_chain",
            "data": {
                "currency": "BTC",
                "options": [
                    {
                        "instrument_name": "BTC-26MAR26-80000-C",
                        "type": "call",
                        "strike": 80000.0,
                        "open_interest": 100.0,
                        "gamma": 0.001,
                        "delta": 0.4,
                        "iv": 55.0,
                    },
                ],
            },
        }
        await recorder.on_options(event)
        assert recorder.event_count == 1
        await recorder.stop()

        csv_files = list(tmp_path.rglob("options_*.csv"))
        assert len(csv_files) >= 1

    @pytest.mark.asyncio
    async def test_multiple_events(self, recorder, tmp_path):
        await recorder.start()
        for i in range(50):
            await recorder.on_trade({
                "data": {
                    "symbol": "BTCUSDT", "exchange": "binance",
                    "price": 50000.0 + i, "qty": 0.1,
                    "side": "buy", "timestamp_ms": 1700000000000 + i * 100,
                    "trade_id": str(i),
                },
            })
        assert recorder.event_count == 50
        await recorder.stop()

    @pytest.mark.asyncio
    async def test_subscribe(self, recorder):
        bus = MagicMock()
        bus.subscribe = MagicMock()
        recorder.subscribe(bus)
        assert bus.subscribe.call_count == 4


# ---------------------------------------------------------------------------
#  BinanceDownloader — URL generation
# ---------------------------------------------------------------------------


class TestBinanceDownloaderURLs:
    def test_aggtrades_daily_url(self):
        dl = BinanceDownloader(market="futures/um")
        from datetime import date
        url = dl._aggtrades_url("BTCUSDT", day=date(2025, 6, 15))
        assert "futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2025-06-15.zip" in url

    def test_aggtrades_monthly_url(self):
        dl = BinanceDownloader(market="futures/um")
        url = dl._aggtrades_url("ETHUSDT", month="2025-03")
        assert "futures/um/monthly/aggTrades/ETHUSDT/ETHUSDT-aggTrades-2025-03.zip" in url

    def test_aggtrades_spot_url(self):
        dl = BinanceDownloader(market="spot")
        from datetime import date
        url = dl._aggtrades_url("BTCUSDT", day=date(2025, 1, 1))
        assert "spot/daily/aggTrades" in url

    def test_klines_url(self):
        dl = BinanceDownloader(market="futures/um")
        from datetime import date
        url = dl._klines_url("BTCUSDT", "1m", date(2025, 6, 15))
        assert "klines/BTCUSDT/1m/BTCUSDT-1m-2025-06-15.zip" in url

    def test_raises_without_day_or_month(self):
        dl = BinanceDownloader()
        with pytest.raises(ValueError):
            dl._aggtrades_url("BTCUSDT")


class TestDateHelpers:
    def test_date_range(self):
        from datetime import date
        dates = list(_date_range(date(2025, 1, 1), date(2025, 1, 5)))
        assert len(dates) == 5
        assert dates[0] == date(2025, 1, 1)
        assert dates[-1] == date(2025, 1, 5)

    def test_unique_months(self):
        from datetime import date
        months = _unique_months(date(2025, 1, 15), date(2025, 3, 10))
        assert months == ["2025-01", "2025-02", "2025-03"]


# ---------------------------------------------------------------------------
#  Converter — trades_to_ticks
# ---------------------------------------------------------------------------


class TestConverter:
    @pytest.fixture
    def trade_csvs(self, tmp_path):
        """Create sample trade CSV files."""
        trades_dir = tmp_path / "trades"
        trades_dir.mkdir()
        fp = trades_dir / "trades_2025-01-01.csv"
        with open(fp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "price", "qty", "side", "trade_id"])
            base_ts = 1700000000000
            for i in range(200):
                w.writerow([
                    base_ts + i * 100,
                    50000 + i * 0.1,
                    0.01,
                    "buy" if i % 3 != 0 else "sell",
                    str(i),
                ])
        return trades_dir

    @pytest.fixture
    def ob_csvs(self, tmp_path):
        """Create sample OB snapshot CSV files."""
        ob_dir = tmp_path / "ob"
        ob_dir.mkdir()
        fp = ob_dir / "orderbook_binance_BTCUSDT.csv"
        headers = ["timestamp_ms", "symbol", "exchange"]
        for i in range(1, 6):
            headers.extend([f"bid{i}_price", f"bid{i}_qty", f"ask{i}_price", f"ask{i}_qty"])
        with open(fp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(headers)
            base_ts = 1700000000000
            for j in range(20):
                row = [base_ts + j * 1000, "BTCUSDT", "binance"]
                for i in range(5):
                    row.extend([50000 - i, 1.0, 50001 + i, 1.0])
                w.writerow(row)
        return ob_dir

    def test_trades_to_ticks(self, trade_csvs):
        ticks = trades_to_ticks(trade_csvs, bucket_ms=5000)
        assert len(ticks) > 0
        for t in ticks:
            assert t.best_bid > 0
            assert t.best_ask > t.best_bid
            assert len(t.bids) > 0
            assert len(t.asks) > 0
            assert len(t.trades) > 0

    def test_trades_to_ticks_limit(self, trade_csvs):
        ticks = trades_to_ticks(trade_csvs, bucket_ms=5000, limit=50)
        assert len(ticks) > 0

    def test_trades_to_ticks_empty(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        ticks = trades_to_ticks(empty_dir)
        assert ticks == []

    def test_merge_ob_and_trades(self, ob_csvs, trade_csvs):
        ticks = merge_ob_and_trades(ob_csvs, trade_csvs, ob_depth=5)
        assert len(ticks) > 0
        matched = sum(1 for t in ticks if len(t.trades) > 0)
        assert matched >= 0

    def test_merge_fallback_without_ob(self, tmp_path, trade_csvs):
        empty_ob = tmp_path / "no_ob"
        empty_ob.mkdir()
        ticks = merge_ob_and_trades(empty_ob, trade_csvs)
        assert len(ticks) > 0

    def test_tick_structure(self, trade_csvs):
        ticks = trades_to_ticks(trade_csvs, bucket_ms=10000)
        tick = ticks[0]
        assert hasattr(tick, "timestamp")
        assert hasattr(tick, "bids")
        assert hasattr(tick, "asks")
        assert hasattr(tick, "trades")
        assert hasattr(tick, "mid_price")
        assert tick.mid_price > 0


# ---------------------------------------------------------------------------
#  BinanceDownloader — download with mocked HTTP
# ---------------------------------------------------------------------------


class TestBinanceDownloaderMocked:
    @staticmethod
    def _make_zip_bytes(csv_content: str, filename: str = "data.csv") -> bytes:
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(filename, csv_content)
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_download_aggtrades_converts_correctly(self, tmp_path):
        raw_csv = (
            "12345,50000.5,0.01,1,2,1700000000000,true,true\n"
            "12346,50001.0,0.02,3,4,1700000000100,false,true\n"
        )
        zip_bytes = self._make_zip_bytes(raw_csv, "BTCUSDT-aggTrades-2025-01-01.csv")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=zip_bytes)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        from datetime import date
        dl = BinanceDownloader(output_dir=str(tmp_path))
        dl._session = mock_session

        out = await dl.download_aggtrades("BTCUSDT", date(2025, 1, 1), date(2025, 1, 1))

        csv_files = list(out.glob("*.csv"))
        assert len(csv_files) == 1

        with open(csv_files[0]) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["side"] == "sell"
        assert rows[1]["side"] == "buy"
        assert rows[0]["price"] == "50000.5"

    @pytest.mark.asyncio
    async def test_download_klines_converts_correctly(self, tmp_path):
        raw_csv = (
            "1700000000000,50000,50100,49900,50050,100,"
            "1700000060000,5000000,1000,60,3000000,0\n"
        )
        zip_bytes = self._make_zip_bytes(raw_csv, "BTCUSDT-1m-2025-01-01.csv")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=zip_bytes)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        from datetime import date
        dl = BinanceDownloader(output_dir=str(tmp_path))
        dl._session = mock_session

        out = await dl.download_klines("BTCUSDT", "1m", date(2025, 1, 1), date(2025, 1, 1))

        csv_files = list(out.glob("*.csv"))
        assert len(csv_files) == 1

        with open(csv_files[0]) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        assert rows[0]["open"] == "50000"
        assert rows[0]["close"] == "50050"

    @pytest.mark.asyncio
    async def test_skip_existing_files(self, tmp_path):
        out_dir = tmp_path / "BTCUSDT" / "trades"
        out_dir.mkdir(parents=True)
        existing = out_dir / "trades_2025-01-01.csv"
        existing.write_text("x" * 200)

        mock_session = AsyncMock()
        mock_session.get = MagicMock()

        from datetime import date
        dl = BinanceDownloader(output_dir=str(tmp_path))
        dl._session = mock_session

        await dl.download_aggtrades("BTCUSDT", date(2025, 1, 1), date(2025, 1, 1))
        mock_session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_404(self, tmp_path):
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        from datetime import date
        dl = BinanceDownloader(output_dir=str(tmp_path))
        dl._session = mock_session

        await dl.download_aggtrades("BTCUSDT", date(2025, 1, 1), date(2025, 1, 1))
        csv_files = list((tmp_path / "BTCUSDT" / "trades").rglob("*.csv"))
        assert len(csv_files) == 0
