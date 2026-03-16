"""
SN64 (Chutes) Fiyat Farkı İzleme Botu
========================================
MEXC (CEX) ile TAO DEX (on-chain AMM) arasındaki fiyat farkını
24 saat boyunca izler ve analiz eder.

Kullanım:
    python scripts/dex_price_monitor.py [--interval 60] [--duration 1440] [--alert 5.0]

Çevre Değişkenleri:
    TAOSTATS_API_KEY  : Taostats API anahtarı (dash.taostats.io adresinden alın)
    ALERT_THRESHOLD   : Spread uyarı eşiği % (varsayılan: 5.0)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import aiosqlite
import pandas as pd

# --- Opsiyonel: Rich terminal ekranı ---
try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------
NETUID = 64                           # Bittensor Subnet 64 = Chutes
MEXC_BASE = "https://api.mexc.com"
TAOSTATS_BASE = "https://api.taostats.io"
SN64_SYMBOL = "SN64USDT"             # MEXC çifti
TAO_SYMBOL = "TAOUSDT"               # MEXC TAO/USDT çifti

DB_PATH = Path("data/price_monitor.db")
REPORTS_DIR = Path("reports")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dex_monitor")

# ---------------------------------------------------------------------------
# Veri modeli
# ---------------------------------------------------------------------------
@dataclass
class PriceSnapshot:
    timestamp: str           # ISO-8601 UTC
    mexc_price_usd: float    # MEXC'te SN64/USDT fiyatı
    dex_alpha_tao: float     # DEX'te SN64 fiyatı (TAO cinsinden)
    tao_usdt: float          # TAO/USDT parite
    dex_price_usd: float     # DEX fiyatı USD'ye çevrilmiş
    spread_usd: float        # mexc - dex (mutlak)
    spread_pct: float        # spread / dex * 100
    mexc_volume_24h: float   # MEXC 24s hacim (USDT)
    dex_tao_in_pool: float   # DEX pool'daki TAO miktarı
    dex_alpha_in_pool: float # DEX pool'daki Alpha miktarı
    alert: bool              # Eşik aşıldı mı?


# ---------------------------------------------------------------------------
# Veritabanı
# ---------------------------------------------------------------------------
async def init_db(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp        TEXT NOT NULL,
            mexc_price_usd   REAL,
            dex_alpha_tao    REAL,
            tao_usdt         REAL,
            dex_price_usd    REAL,
            spread_usd       REAL,
            spread_pct       REAL,
            mexc_volume_24h  REAL,
            dex_tao_in_pool  REAL,
            dex_alpha_in_pool REAL,
            alert            INTEGER
        )
    """)
    await conn.commit()


async def save_snapshot(conn: aiosqlite.Connection, snap: PriceSnapshot) -> None:
    d = asdict(snap)
    d["alert"] = int(d["alert"])
    await conn.execute(
        """INSERT INTO price_snapshots
           (timestamp, mexc_price_usd, dex_alpha_tao, tao_usdt,
            dex_price_usd, spread_usd, spread_pct,
            mexc_volume_24h, dex_tao_in_pool, dex_alpha_in_pool, alert)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (d["timestamp"], d["mexc_price_usd"], d["dex_alpha_tao"], d["tao_usdt"],
         d["dex_price_usd"], d["spread_usd"], d["spread_pct"],
         d["mexc_volume_24h"], d["dex_tao_in_pool"], d["dex_alpha_in_pool"], d["alert"]),
    )
    await conn.commit()


# ---------------------------------------------------------------------------
# MEXC Fiyat Çekme
# ---------------------------------------------------------------------------
async def fetch_mexc_price(session: aiohttp.ClientSession) -> tuple[float, float, float]:
    """SN64/USDT ve TAO/USDT fiyatlarını MEXC'ten çeker.

    Returns:
        (sn64_price, tao_price, sn64_volume_24h)
    """
    async with session.get(
        f"{MEXC_BASE}/api/v3/ticker/24hr",
        params={"symbol": SN64_SYMBOL},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        sn64_price = float(data["lastPrice"])
        sn64_volume = float(data.get("quoteVolume", 0.0))

    async with session.get(
        f"{MEXC_BASE}/api/v3/ticker/price",
        params={"symbol": TAO_SYMBOL},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        tao_price = float(data["price"])

    return sn64_price, tao_price, sn64_volume


# ---------------------------------------------------------------------------
# TAO DEX (Taostats API) Fiyat Çekme
# ---------------------------------------------------------------------------
async def fetch_dex_price(
    session: aiohttp.ClientSession, api_key: Optional[str]
) -> tuple[float, float, float]:
    """TAO DEX'ten SN64 alpha fiyatını çeker.

    Strateji:
      1. Taostats API: /api/subnet/latest/v1  (API key gerekli)
      2. Fallback: /api/v1/subnet?netuid=64   (public)
      3. Fallback: pool rezervlerinden hesapla (/api/dtao/liquidity/position/v1)

    Returns:
        (alpha_price_in_tao, tao_in_pool, alpha_in_pool)
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # --- Deneme 1: Taostats subnet latest endpoint ---
    if api_key:
        try:
            async with session.get(
                f"{TAOSTATS_BASE}/api/subnet/latest/v1",
                params={"netuid": NETUID, "limit": 1},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Taostats yanıt formatı: {"data": [...]}
                    items = data.get("data") or data
                    if isinstance(items, list):
                        for item in items:
                            if item.get("netuid") == NETUID:
                                alpha_price = float(item.get("alpha_price", 0))
                                tao_in = float(item.get("tao_in", 0))
                                alpha_in = float(item.get("alpha_in", 0))
                                if alpha_price > 0:
                                    return alpha_price, tao_in, alpha_in
        except Exception as e:
            log.debug(f"Taostats subnet endpoint hatası: {e}")

    # --- Deneme 2: Taostats liquidity positions (pool rezervlerinden hesapla) ---
    try:
        params: dict = {"netuid": NETUID, "limit": 1, "order": "block_number:desc"}
        async with session.get(
            f"{TAOSTATS_BASE}/api/dtao/liquidity/position/v1",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data") or data
                if isinstance(items, list) and items:
                    item = items[0]
                    tao_in = float(item.get("tao", item.get("tao_in", 0)))
                    alpha_in = float(item.get("alpha", item.get("alpha_in", 0)))
                    if tao_in > 0 and alpha_in > 0:
                        # k = tao_in * alpha_in sabit; alpha_price = tao_in / alpha_in
                        alpha_price = tao_in / alpha_in
                        return alpha_price, tao_in, alpha_in
    except Exception as e:
        log.debug(f"Taostats liquidity endpoint hatası: {e}")

    # --- Deneme 3: taostats.io subnets HTML'den veri çekme (son çare) ---
    try:
        async with session.get(
            f"https://taostats.io/api/subnets",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if isinstance(data, list):
                    for item in data:
                        if item.get("netuid") == NETUID:
                            alpha_price = float(item.get("alpha_price", 0))
                            tao_in = float(item.get("tao_in", 0))
                            alpha_in = float(item.get("alpha_in", 0))
                            if alpha_price > 0:
                                return alpha_price, tao_in, alpha_in
    except Exception as e:
        log.debug(f"Taostats subnets API hatası: {e}")

    raise RuntimeError(
        "TAO DEX fiyatı alınamadı. TAOSTATS_API_KEY ortam değişkenini ayarlayın "
        "(dash.taostats.io adresinden ücretsiz API anahtarı alın)."
    )


# ---------------------------------------------------------------------------
# Analiz ve Rapor
# ---------------------------------------------------------------------------
def generate_24h_report(snapshots: list[PriceSnapshot]) -> pd.DataFrame:
    """24 saatlik snapshot listesinden istatistik raporu üretir."""
    if not snapshots:
        return pd.DataFrame()

    df = pd.DataFrame([asdict(s) for s in snapshots])
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    report = {
        "Başlangıç Zamanı": [df["timestamp"].min()],
        "Bitiş Zamanı": [df["timestamp"].max()],
        "Toplam Ölçüm": [len(df)],
        "MEXC Ort. Fiyat": [df["mexc_price_usd"].mean()],
        "MEXC Min Fiyat": [df["mexc_price_usd"].min()],
        "MEXC Max Fiyat": [df["mexc_price_usd"].max()],
        "DEX Ort. Fiyat (USD)": [df["dex_price_usd"].mean()],
        "DEX Min Fiyat (USD)": [df["dex_price_usd"].min()],
        "DEX Max Fiyat (USD)": [df["dex_price_usd"].max()],
        "Ort. Spread USD": [df["spread_usd"].mean()],
        "Max Spread USD": [df["spread_usd"].max()],
        "Min Spread USD": [df["spread_usd"].min()],
        "Ort. Spread %": [df["spread_pct"].mean()],
        "Max Spread %": [df["spread_pct"].max()],
        "Min Spread %": [df["spread_pct"].min()],
        "Uyarı Sayısı": [df["alert"].sum()],
        "MEXC 24s Ort. Hacim (USDT)": [df["mexc_volume_24h"].mean()],
    }

    return pd.DataFrame(report).T.rename(columns={0: "Değer"})


def export_to_csv(snapshots: list[PriceSnapshot], path: Path) -> None:
    """Snapshot listesini CSV'ye aktarır."""
    if not snapshots:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(snapshots[0]).keys()))
        writer.writeheader()
        writer.writerows([asdict(s) for s in snapshots])


# ---------------------------------------------------------------------------
# Terminal Ekranı (Rich)
# ---------------------------------------------------------------------------
class TerminalDisplay:
    def __init__(self, alert_threshold: float) -> None:
        self.console = Console() if RICH_AVAILABLE else None
        self.alert_threshold = alert_threshold
        self.snapshots: list[PriceSnapshot] = []
        self._live: Optional[Live] = None

    def _make_table(self) -> Table:
        if not RICH_AVAILABLE:
            return None  # type: ignore

        table = Table(
            title=f"SN64 (Chutes) — MEXC vs TAO DEX Fiyat Farkı İzleme",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Zaman (UTC)", style="dim", width=20)
        table.add_column("MEXC (USDT)", justify="right", style="green")
        table.add_column("TAO DEX (USDT)", justify="right", style="blue")
        table.add_column("DEX (TAO)", justify="right", style="blue")
        table.add_column("Spread USD", justify="right")
        table.add_column("Spread %", justify="right")
        table.add_column("Uyarı", justify="center", width=6)

        recent = self.snapshots[-20:]  # Son 20 kayıt
        for s in recent:
            spread_color = "red" if s.alert else ("yellow" if abs(s.spread_pct) > 2 else "white")
            alert_icon = "[red]⚠[/red]" if s.alert else ""
            table.add_row(
                s.timestamp[11:19],
                f"${s.mexc_price_usd:.4f}",
                f"${s.dex_price_usd:.4f}",
                f"τ{s.dex_alpha_tao:.6f}",
                f"[{spread_color}]{s.spread_usd:+.4f}[/{spread_color}]",
                f"[{spread_color}]{s.spread_pct:+.2f}%[/{spread_color}]",
                alert_icon,
            )

        return table

    def _make_stats_panel(self) -> Panel:
        if not self.snapshots or not RICH_AVAILABLE:
            return None  # type: ignore

        last = self.snapshots[-1]
        spreads = [s.spread_pct for s in self.snapshots]
        alert_count = sum(1 for s in self.snapshots if s.alert)

        text = Text()
        text.append(f"Toplam Ölçüm: {len(self.snapshots)}\n", style="white")
        text.append(f"Son MEXC: ${last.mexc_price_usd:.4f}\n", style="green")
        text.append(f"Son DEX:  ${last.dex_price_usd:.4f}  (τ{last.dex_alpha_tao:.6f})\n", style="blue")
        text.append(f"Son TAO:  ${last.tao_usdt:.2f}\n", style="blue")
        text.append(f"Son Spread: {last.spread_pct:+.2f}%\n",
                    style="red" if last.alert else "yellow")
        text.append(f"Ort. Spread: {sum(spreads)/len(spreads):+.2f}%\n", style="white")
        text.append(f"Max Spread: {max(spreads):+.2f}%\n", style="white")
        text.append(f"Min Spread: {min(spreads):+.2f}%\n", style="white")
        text.append(f"Uyarı Sayısı: {alert_count}\n", style="red" if alert_count > 0 else "white")
        text.append(f"Pool: τ{last.dex_tao_in_pool:.2f} TAO | {last.dex_alpha_in_pool:.2f} α\n",
                    style="dim")

        return Panel(text, title="[bold]İstatistikler[/bold]", border_style="cyan")

    def update(self, snap: PriceSnapshot) -> None:
        self.snapshots.append(snap)
        if not RICH_AVAILABLE:
            # Basit terminal çıktısı
            alert_tag = " ⚠ UYARI!" if snap.alert else ""
            print(
                f"[{snap.timestamp[11:19]}] "
                f"MEXC: ${snap.mexc_price_usd:.4f} | "
                f"DEX: ${snap.dex_price_usd:.4f} (τ{snap.dex_alpha_tao:.6f}) | "
                f"Spread: {snap.spread_pct:+.2f}%{alert_tag}"
            )

    def print_static(self) -> None:
        """Rich yoksa veya live mode dışında sade çıktı."""
        if not RICH_AVAILABLE or not self.snapshots:
            return
        self.console.print(self._make_table())
        self.console.print(self._make_stats_panel())


# ---------------------------------------------------------------------------
# Ana Bot
# ---------------------------------------------------------------------------
class PriceMonitorBot:
    def __init__(
        self,
        interval_sec: int = 60,
        duration_min: int = 1440,
        alert_threshold_pct: float = 5.0,
        api_key: Optional[str] = None,
    ) -> None:
        self.interval_sec = interval_sec
        self.duration_sec = duration_min * 60
        self.alert_threshold = alert_threshold_pct
        self.api_key = api_key
        self.snapshots: list[PriceSnapshot] = []
        self.display = TerminalDisplay(alert_threshold_pct)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def _fetch_once(self, session: aiohttp.ClientSession) -> PriceSnapshot:
        mexc_price, tao_price, mexc_vol = await fetch_mexc_price(session)
        alpha_tao, tao_in_pool, alpha_in_pool = await fetch_dex_price(session, self.api_key)

        dex_price_usd = alpha_tao * tao_price
        spread_usd = mexc_price - dex_price_usd
        spread_pct = (spread_usd / dex_price_usd * 100) if dex_price_usd else 0.0
        alert = abs(spread_pct) >= self.alert_threshold

        snap = PriceSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            mexc_price_usd=mexc_price,
            dex_alpha_tao=alpha_tao,
            tao_usdt=tao_price,
            dex_price_usd=dex_price_usd,
            spread_usd=spread_usd,
            spread_pct=spread_pct,
            mexc_volume_24h=mexc_vol,
            dex_tao_in_pool=tao_in_pool,
            dex_alpha_in_pool=alpha_in_pool,
            alert=alert,
        )
        return snap

    async def run(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

        elapsed = 0
        iteration = 0
        start_time = datetime.now(timezone.utc)

        log.info("=" * 60)
        log.info("SN64 (Chutes) Fiyat Farkı İzleme Botu Başlatıldı")
        log.info(f"Süre: {self.duration_sec // 60} dakika | "
                 f"Interval: {self.interval_sec}s | "
                 f"Uyarı eşiği: %{self.alert_threshold}")
        log.info("=" * 60)

        if not RICH_AVAILABLE:
            log.warning("'rich' paketi bulunamadı. Sade terminal çıktısı kullanılacak.")
            log.warning("Kurmak için: pip install rich")

        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with aiosqlite.connect(DB_PATH) as db:
                await init_db(db)

                while not self._stop and elapsed < self.duration_sec:
                    try:
                        snap = await self._fetch_once(session)
                        await save_snapshot(db, snap)
                        self.snapshots.append(snap)
                        self.display.update(snap)

                        if snap.alert:
                            log.warning(
                                f"SPREAD UYARISI! {snap.spread_pct:+.2f}% | "
                                f"MEXC: ${snap.mexc_price_usd:.4f} | "
                                f"DEX: ${snap.dex_price_usd:.4f}"
                            )

                        iteration += 1

                        # Her saatte bir özet
                        if iteration % (3600 // self.interval_sec) == 0:
                            self._print_hourly_summary()

                    except RuntimeError as e:
                        log.error(str(e))
                        log.info("Bot durduruluyor...")
                        break
                    except Exception as e:
                        log.error(f"Veri çekme hatası: {e}")

                    await asyncio.sleep(self.interval_sec)
                    elapsed += self.interval_sec

        self._finalize(start_time)

    def _print_hourly_summary(self) -> None:
        if not self.snapshots:
            return
        recent = self.snapshots[-max(1, 3600 // self.interval_sec):]
        spreads = [s.spread_pct for s in recent]
        log.info(
            f"[SAATLIK ÖZET] Ort. Spread: {sum(spreads)/len(spreads):.2f}% | "
            f"Max: {max(spreads):.2f}% | Min: {min(spreads):.2f}% | "
            f"Örnekler: {len(recent)}"
        )

    def _finalize(self, start_time: datetime) -> None:
        log.info("=" * 60)
        log.info("Bot tamamlandı. Rapor oluşturuluyor...")

        # CSV dışa aktar
        ts = start_time.strftime("%Y%m%d_%H%M%S")
        csv_path = REPORTS_DIR / f"sn64_price_monitor_{ts}.csv"
        export_to_csv(self.snapshots, csv_path)
        log.info(f"CSV kaydedildi: {csv_path}")

        # 24s raporu
        report_df = generate_24h_report(self.snapshots)
        if not report_df.empty:
            report_path = REPORTS_DIR / f"sn64_24h_report_{ts}.txt"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("SN64 (Chutes) 24 Saatlik Fiyat Farkı Raporu\n")
                f.write("=" * 50 + "\n")
                f.write(report_df.to_string())
                f.write("\n")
            log.info(f"24h raporu kaydedildi: {report_path}")
            print("\n" + "=" * 60)
            print("SN64 (Chutes) 24 Saatlik Raporu")
            print("=" * 60)
            print(report_df.to_string())
            print("=" * 60)

        if RICH_AVAILABLE and self.snapshots:
            self.display.print_static()

        log.info(f"SQLite DB: {DB_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SN64 (Chutes) MEXC vs TAO DEX fiyat farkı izleme botu"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling aralığı (saniye, varsayılan: 60)"
    )
    parser.add_argument(
        "--duration", type=int, default=1440,
        help="İzleme süresi (dakika, varsayılan: 1440 = 24 saat)"
    )
    parser.add_argument(
        "--alert", type=float,
        default=float(os.getenv("ALERT_THRESHOLD", "5.0")),
        help="Spread uyarı eşiği %% (varsayılan: 5.0)"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    api_key = os.getenv("TAOSTATS_API_KEY")

    if not api_key:
        log.warning(
            "TAOSTATS_API_KEY ortam değişkeni ayarlanmamış. "
            "DEX fiyatı için public endpoint kullanılacak (daha yavaş/kısıtlı). "
            "Ücretsiz API anahtarı için: https://dash.taostats.io"
        )

    bot = PriceMonitorBot(
        interval_sec=args.interval,
        duration_min=args.duration,
        alert_threshold_pct=args.alert,
        api_key=api_key,
    )

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stop)
        except NotImplementedError:
            pass  # Windows

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
