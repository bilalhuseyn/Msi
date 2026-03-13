from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from config.constants import FeedStatus

logger = logging.getLogger(__name__)


@dataclass
class ComponentHealth:
    name: str
    status: str = "unknown"
    last_update: float = 0.0
    message_count: int = 0
    error: str | None = None

    @property
    def age_sec(self) -> float:
        if self.last_update == 0:
            return -1.0
        return time.time() - self.last_update

    @property
    def is_healthy(self) -> bool:
        return self.status in ("connected", "ok", FeedStatus.CONNECTED.value)


class HealthMonitor:
    """
    Central health tracker for all system components.
    Aggregates feed statuses, module health, and system metrics.
    """

    def __init__(self, stale_threshold_sec: float = 30.0):
        self._components: dict[str, ComponentHealth] = {}
        self._stale_threshold = stale_threshold_sec
        self._start_time = time.time()

    def register(self, name: str) -> None:
        self._components[name] = ComponentHealth(name=name)

    def update(
        self,
        name: str,
        status: str,
        message_count: int = 0,
        error: str | None = None,
    ) -> None:
        if name not in self._components:
            self.register(name)
        comp = self._components[name]
        comp.status = status
        comp.last_update = time.time()
        comp.message_count = message_count
        comp.error = error

    def get_status(self) -> dict:
        now = time.time()
        components = {}
        all_healthy = True

        for name, comp in self._components.items():
            is_stale = comp.age_sec > self._stale_threshold if comp.last_update > 0 else True
            healthy = comp.is_healthy and not is_stale
            if not healthy:
                all_healthy = False

            components[name] = {
                "status": comp.status,
                "healthy": healthy,
                "age_sec": round(comp.age_sec, 1),
                "messages": comp.message_count,
                "error": comp.error,
            }

        return {
            "healthy": all_healthy,
            "uptime_sec": round(now - self._start_time, 1),
            "components": components,
        }

    def print_dashboard(self) -> str:
        """Generate a compact console dashboard string."""
        status = self.get_status()
        lines = [
            "",
            "=" * 60,
            f"  OFI Pro Health | Uptime: {status['uptime_sec']:.0f}s | "
            f"{'HEALTHY' if status['healthy'] else 'DEGRADED'}",
            "=" * 60,
        ]

        for name, info in status["components"].items():
            icon = "+" if info["healthy"] else "!"
            line = (
                f"  [{icon}] {name:<25s} "
                f"{info['status']:<15s} "
                f"msgs={info['messages']:<8d} "
                f"age={info['age_sec']:.1f}s"
            )
            if info["error"]:
                line += f"  ERR: {info['error'][:40]}"
            lines.append(line)

        lines.append("=" * 60)
        return "\n".join(lines)
