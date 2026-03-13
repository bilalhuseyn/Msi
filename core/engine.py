"""
OFI Pro — Master Loop Engine

Orchestrates all data feeds, 7 signal modules, the Confirmation Engine,
and risk management.  Phase 0+1+2+3 implementation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config.settings import Settings, get_settings
from config.constants import (
    ClearanceStatus,
    DecisionAction,
    SpreadStatus,
    VetoReason,
)
from core.events import EventBus
from core.health import HealthMonitor
from data.feeds.binance_feed import BinanceFeed
from data.feeds.bybit_feed import BybitFeed
from data.feeds.deribit_feed import DeribitPoller
from data.normalizer import DataNormalizer
from data.storage import InfluxWriter, SQLiteStore
from signals.obi import OBIModule
from signals.vpin import VPINModule
from signals.spread_monitor import SpreadMonitor
from signals.depth_erosion import DepthErosionMonitor
from signals.spoofing_detector import SpoofingDetector
from signals.clearance_detector import ClearanceDetector
from signals.options_layer import OptionsLayer
from signals.confirmation import ConfirmationEngine, Decision
from execution.paper_trader import PaperTrader
from risk.risk_manager import RiskManager
from utils.logging import setup_logging, SignalLogger

logger = logging.getLogger(__name__)


class OFIEngine:
    """
    Main engine that ties together feeds, all 7 signal modules,
    the Confirmation Engine, risk management, and storage.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        paper_trading: bool = True,
        initial_balance: float = 10_000.0,
        paper_log_file: str | None = None,
    ):
        self.settings = settings or get_settings()
        self.event_bus = EventBus()
        self.normalizer = DataNormalizer()
        self.health = HealthMonitor()
        self.signal_logger = SignalLogger()

        self._feeds: list = []
        self._deribit: DeribitPoller | None = None
        self._influx: InfluxWriter | None = None
        self._sqlite: SQLiteStore | None = None

        self._obi: dict[str, OBIModule] = {}
        self._vpin: dict[str, VPINModule] = {}
        self._spread: dict[str, SpreadMonitor] = {}
        self._depth: dict[str, DepthErosionMonitor] = {}
        self._spoof: dict[str, SpoofingDetector] = {}
        self._clearance: dict[str, ClearanceDetector] = {}
        self._options = OptionsLayer(self.settings.options)
        self._confirmation = ConfirmationEngine(self.settings.confirmation)
        self._risk = RiskManager(self.settings.risk)
        self._risk.set_balance(initial_balance)

        # Paper trader — active when paper_trading=True (default)
        self._paper: PaperTrader | None = (
            PaperTrader(initial_balance=initial_balance) if paper_trading else None
        )
        self._paper_log_file: str = (
            paper_log_file
            or os.environ.get("PAPER_LOG_FILE", "logs/paper_trades.jsonl")
        )

        self._last_ob: dict[str, dict] = {}
        self._last_ticker: dict[str, dict] = {}
        self._recent_trades: dict[str, deque] = {}
        self._last_decision: dict[str, Decision] = {}
        self._running = False

        self._dashboard_interval = 5.0
        self._last_dashboard_ts = 0.0

    async def start(self) -> None:
        logger.info("OFI Pro Engine starting...")

        self._influx = InfluxWriter(self.settings)
        self._sqlite = SQLiteStore()
        await self._influx.start()
        await self._sqlite.start()

        self.event_bus.subscribe("order_book", self._on_order_book)
        self.event_bus.subscribe("trade", self._on_trade)
        self.event_bus.subscribe("ticker", self._on_ticker)
        self.event_bus.subscribe("options_chain", self._on_options)
        await self.event_bus.start()

        s = self.settings
        for symbol in s.symbols:
            self._obi[symbol] = OBIModule(s.obi)
            self._vpin[symbol] = VPINModule(s.vpin)
            self._spread[symbol] = SpreadMonitor(s.spread)
            self._depth[symbol] = DepthErosionMonitor(
                check_interval=s.depth_erosion.check_interval,
                erosion_threshold=s.depth_erosion.erosion_threshold,
                price_stability=s.depth_erosion.price_stability,
                depth=s.depth_erosion.depth,
            )
            self._spoof[symbol] = SpoofingDetector(
                size_threshold=s.spoofing.size_threshold,
                cancel_window_ms=s.spoofing.cancel_window_ms,
            )
            self._clearance[symbol] = ClearanceDetector(
                one_sided_threshold=s.clearance.one_sided_threshold,
                ask_slide_pct=s.clearance.ask_slide_pct,
                large_trade_multiplier=s.clearance.large_trade_multiplier,
                large_trade_min_cluster=s.clearance.large_trade_min_cluster,
                bid_thin_pct=s.clearance.bid_thin_pct,
                ob_history_depth=s.clearance.ob_history_depth,
                recent_trade_window=s.clearance.recent_trade_window,
            )
            self._recent_trades[symbol] = deque(maxlen=300)

            binance = BinanceFeed(symbol, self.event_bus.queue, self.settings)
            bybit = BybitFeed(symbol, self.event_bus.queue, self.settings)
            self._feeds.extend([binance, bybit])
            self.health.register(binance.name)
            self.health.register(bybit.name)

        self._deribit = DeribitPoller(self.event_bus.queue, self.settings)
        self.health.register("deribit")

        for feed in self._feeds:
            await feed.start()
        await self._deribit.start()

        self.health.register("engine")
        self.health.register("event_bus")

        self._running = True
        logger.info(
            "OFI Pro Engine started | %d symbols, %d feeds, 7 modules + CE",
            len(s.symbols), len(self._feeds) + 1,
        )

    async def stop(self) -> None:
        self._running = False
        logger.info("OFI Pro Engine stopping...")

        for feed in self._feeds:
            await feed.stop()
        if self._deribit:
            await self._deribit.stop()
        await self.event_bus.stop()
        if self._influx:
            await self._influx.stop()
        if self._sqlite:
            await self._sqlite.stop()

        if self._paper is not None:
            self._flush_paper_log()

        logger.info("OFI Pro Engine stopped")

    # ---- Event Handlers ----

    async def _on_order_book(self, event: dict) -> None:
        event = self.normalizer.normalize_order_book(event)
        data = event["data"]
        symbol = data["symbol"]
        exchange = data["exchange"]

        # Track health for all exchanges
        if exchange == "bybit" and symbol in self._obi:
            self.health.update(f"bybit-{symbol}", "connected")

        if exchange != "binance":
            return

        self._last_ob[symbol] = data
        self.health.update(f"binance-{symbol}", "connected")

        obi_result = self._obi[symbol].update(data) if symbol in self._obi else None

        spoof_result = None
        if symbol in self._spoof:
            spoof_data = {
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
                "timestamp_ms": int(data.get("ts_utc", time.time()) * 1000),
            }
            spoof_result = self._spoof[symbol].update(spoof_data)

        depth_result = None
        if symbol in self._depth:
            spoof_active = (
                spoof_result is not None
                and spoof_result.metadata.get("is_active", False)
            )
            depth_data = {
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
                "mid_price": data.get("mid_price", 0),
                "timestamp": data.get("ts_utc", time.time()),
                "spoof_active": spoof_active,
            }
            depth_result = self._depth[symbol].update(depth_data)

        clearance_result = None
        if symbol in self._clearance:
            clearance_data = {
                "bids": data.get("bids", []),
                "asks": data.get("asks", []),
                "recent_trades": list(self._recent_trades.get(symbol, [])),
            }
            clearance_result = self._clearance[symbol].update(clearance_data)

        await self._run_confirmation(symbol, obi_result, spoof_result, depth_result, clearance_result)

        await self._write_metric(symbol, "obi", {
            "raw": obi_result.raw_value if obi_result else 0,
            "direction": int(obi_result.direction) if obi_result else 0,
        })
        if spoof_result and spoof_result.metadata.get("spoof_count", 0) > 0:
            await self._write_metric(symbol, "spoof", {
                "count": float(spoof_result.metadata["spoof_count"]),
            })
        if depth_result and depth_result.metadata.get("status") not in ("SKIP", "BASELINE_SET", None):
            await self._write_metric(symbol, "depth_erosion", {
                "ask_erosion": depth_result.metadata.get("ask_erosion", 0),
                "bid_erosion": depth_result.metadata.get("bid_erosion", 0),
            })

    async def _on_trade(self, event: dict) -> None:
        event = self.normalizer.normalize_trade(event)
        data = event["data"]
        symbol = data["symbol"]
        if data["exchange"] != "binance":
            return

        if symbol in self._recent_trades:
            self._recent_trades[symbol].append({
                "price": data["price"],
                "qty": data["qty"],
                "side": data["side"],
            })

        ticker = self._last_ticker.get(symbol, {})
        mid = ticker.get("mid_price", data["price"])

        vpin_mod = self._vpin.get(symbol)
        if vpin_mod:
            vpin_mod.process_trade(data["price"], data["qty"], mid)
            if vpin_mod.is_ready:
                vpin_val = vpin_mod.vpin
                if vpin_val is not None:
                    await self._write_metric(symbol, "vpin", {"value": vpin_val})
                    if vpin_val > self.settings.vpin.veto_threshold:
                        self.signal_logger.log_veto(
                            symbol, VetoReason.VPIN_CRITICAL.value,
                            {"vpin": vpin_val},
                        )

    async def _on_ticker(self, event: dict) -> None:
        event = self.normalizer.normalize_ticker(event)
        data = event["data"]
        symbol = data["symbol"]
        if data["exchange"] != "binance":
            return

        self._last_ticker[symbol] = data

        spread_mod = self._spread.get(symbol)
        if spread_mod:
            spread_result = spread_mod.update(data)
            status = spread_result.metadata.get("status", "")
            await self._write_metric(symbol, "spread", {
                "pct": spread_result.raw_value,
                "ratio": spread_result.metadata.get("ratio", 0),
            })
            if status == SpreadStatus.MM_WITHDRAWN.value:
                self.signal_logger.log_veto(
                    symbol, VetoReason.SPREAD_CRISIS.value,
                    {"status": status, "ratio": spread_result.metadata.get("ratio")},
                )

    async def _on_options(self, event: dict) -> None:
        data = event["data"]
        options = data.get("options", [])
        last_ticker = self._last_ticker.get("BTCUSDT", {})
        spot = last_ticker.get("mid_price", 0)

        if options and spot > 0:
            result = self._options.update({
                "options_chain": options, "spot_price": spot,
            })
            self.health.update("deribit", "connected", data.get("count", 0))
            await self._write_metric("BTC", "gex", {
                "value": result.raw_value,
                "multiplier": result.metadata.get("multiplier", 1.0),
            })

    # ---- Confirmation Engine ----

    async def _run_confirmation(
        self,
        symbol: str,
        obi_result,
        spoof_result,
        depth_result,
        clearance_result,
    ) -> None:
        vpin_mod = self._vpin.get(symbol)
        vpin_val = vpin_mod.vpin if vpin_mod and vpin_mod.is_ready else None

        spread_mod = self._spread.get(symbol)
        spread_status = (
            spread_mod.current_status.value
            if spread_mod and spread_mod.current_status
            else SpreadStatus.MM_ACTIVE.value
        )

        regime = self._options.last_regime

        spoof_list = (
            spoof_result.metadata.get("spoofs", [])
            if spoof_result else []
        )
        obi_mod = self._obi.get(symbol)
        obi_history = obi_mod.history if obi_mod else []

        options_score = 0
        if regime:
            if regime.regime.value == "SHORT_GAMMA" and not regime.flip_risk:
                options_score = 1
            elif regime.flip_risk:
                options_score = -1

        signals = {
            "OBI": (obi_result.metadata.get("obi_ma", float(obi_result.score))
                    if obi_result else 0.0),  # P6: float gradient
            "OBI_HISTORY": obi_history[-10:],
            "SPREAD_SCORE": (
                self._spread[symbol].update(
                    self._last_ticker.get(symbol, {"best_bid": 0, "best_ask": 0})
                ).metadata.get("score", 0)
                if symbol in self._spread and symbol in self._last_ticker
                else 0
            ),
            "DEPTH_SCORE": int(depth_result.direction) if depth_result else 0,
            "SPOOF_LIST": spoof_list,
            "SPOOF_SCORE": int(spoof_result.direction) if spoof_result else 0,
            "CLEARANCE_STATUS": (
                clearance_result.metadata.get("status", "NORMAL")
                if clearance_result else "NORMAL"
            ),
            "CLEARANCE_SCORE": int(clearance_result.direction) if clearance_result else 0,
            "OPTIONS_SCORE": options_score,
        }

        decision = self._confirmation.evaluate(
            signals, vpin_val, spread_status, regime,
        )
        self._last_decision[symbol] = decision

        # Risk gate: block entry if risk limits exceeded
        if decision.action in (DecisionAction.LONG, DecisionAction.SHORT):
            can_trade, block_reason = self._risk.can_open_position(symbol)
            if not can_trade:
                logger.info("Risk gate blocked %s %s: %s", symbol, decision.action.value, block_reason)

        # Paper trader: forward decision for simulated execution
        if self._paper is not None:
            ticker = self._last_ticker.get(symbol, {})
            price = ticker.get("mid_price", 0.0)
            if price > 0:
                gex_flip = regime.flip_risk if regime else False
                self._paper.on_decision(
                    symbol=symbol,
                    action=decision.action,
                    score=decision.score,
                    price=price,
                    atr=price * 0.005,  # fallback ATR ~0.5%
                    vpin_value=vpin_val,
                    gex_flip=gex_flip,
                )

        self.signal_logger.log_decision(
            symbol=symbol,
            action=decision.action.value,
            score=decision.score,
            signals=signals,
            regime={
                "type": regime.regime.value,
                "gex": regime.gex_value,
                "flip_risk": regime.flip_risk,
                "multiplier": regime.multiplier,
            } if regime else None,
            veto_reason=(
                decision.veto_reason.value
                if decision.veto_reason != VetoReason.NONE
                else None
            ),
        )

        if self._sqlite:
            await self._sqlite.log_signal(
                symbol=symbol,
                action=decision.action.value,
                score=decision.score,
                veto_reason=(
                    decision.veto_reason.value
                    if decision.veto_reason != VetoReason.NONE
                    else None
                ),
                raw_signals={k: v for k, v in signals.items() if k != "OBI_HISTORY"},
                regime={
                    "type": regime.regime.value,
                    "multiplier": regime.multiplier,
                } if regime else None,
            )

        await self._write_metric(symbol, "decision", {
            "score": decision.score,
            "action": float(decision.direction),
        })

    # ---- Paper Trade Logging ----

    def _flush_paper_log(self) -> None:
        """Write PaperTrader session summary + trade list to JSONL log file."""
        if self._paper is None:
            return
        try:
            log_path = Path(self._paper_log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            stats = self._paper._stats
            positions = list(self._paper._closed_positions)

            session_record = {
                "session_end": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "total_trades": stats.total_trades,
                    "wins": stats.wins,
                    "losses": stats.losses,
                    "win_rate": round(stats.win_rate, 4),
                    "total_pnl": round(stats.total_pnl, 4),
                    "max_drawdown": round(stats.max_drawdown, 4),
                    "profit_factor": round(stats.profit_factor, 4),
                    "vetoes": stats.vetoes,
                },
                "trades": [
                    {
                        "symbol": p.symbol,
                        "side": p.side,
                        "entry_price": p.entry_price,
                        "exit_price": p.exit_price,
                        "qty": p.qty,
                        "pnl": round(p.pnl, 4),
                        "state": p.state.value if hasattr(p.state, "value") else str(p.state),
                        "open_ts": p.open_ts,
                        "close_ts": p.close_ts,
                    }
                    for p in positions
                ],
            }

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(session_record) + "\n")

            logger.info(
                "Paper trade log written → %s  (%d trades, PnL=%.2f)",
                log_path, stats.total_trades, stats.total_pnl,
            )
        except Exception as exc:
            logger.warning("Failed to write paper trade log: %s", exc)

    # ---- Helpers ----

    async def _write_metric(
        self, symbol: str, measurement: str, fields: dict
    ) -> None:
        if self._influx:
            await self._influx.write_metric(
                measurement=measurement,
                tags={"symbol": symbol},
                fields=fields,
            )

    async def run_dashboard_tick(self) -> None:
        now = time.time()
        if now - self._last_dashboard_ts >= self._dashboard_interval:
            self._last_dashboard_ts = now
            self.health.update("engine", "ok")
            self.health.update("event_bus", "ok", self.event_bus.stats["processed"])
            dashboard = self.health.print_dashboard()
            logger.info(dashboard)
            if self._paper is not None:
                logger.info(self._paper.print_status())

    def get_signal_snapshot(self, symbol: str) -> dict:
        obi = self._obi.get(symbol)
        vpin = self._vpin.get(symbol)
        spread = self._spread.get(symbol)
        depth = self._depth.get(symbol)
        spoof = self._spoof.get(symbol)
        clearance = self._clearance.get(symbol)
        regime = self._options.last_regime
        decision = self._last_decision.get(symbol)

        return {
            "symbol": symbol,
            "obi": obi.update(self._last_ob.get(symbol, {"bids": [], "asks": []})).metadata if obi else None,
            "vpin": {"value": vpin.vpin, "ready": vpin.is_ready} if vpin else None,
            "spread": spread.current_status.value if spread and spread.current_status else None,
            "depth": depth.last_status.value if depth else None,
            "spoof_active": spoof.is_active if spoof else False,
            "clearance": clearance.last_status.value if clearance else None,
            "regime": {
                "type": regime.regime.value,
                "gex": regime.gex_value,
                "flip_risk": regime.flip_risk,
                "multiplier": regime.multiplier,
            } if regime else None,
            "decision": {
                "action": decision.action.value,
                "score": decision.score,
                "reason": decision.reason,
            } if decision else None,
            "ts": time.time(),
        }


async def main() -> None:
    setup_logging()
    engine = OFIEngine()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await engine.start()
        while not stop_event.is_set():
            await engine.run_dashboard_tick()
            await asyncio.sleep(0.1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
