from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from config.constants import RegimeType, SignalDirection
from config.settings import OptionsSettings
from signals.base import BaseSignalModule, SignalOutput


@dataclass
class RegimeInfo:
    regime: RegimeType
    gex_value: float
    pcr_value: float | None
    flip_risk: bool
    pcr_signal: str
    multiplier: float
    description: str


class OptionsLayer(BaseSignalModule):
    """
    M7: MM Regime Detector — Options Layer.

    Calculates GEX and PCR from Deribit options chain data.
    Determines whether MMs are in Long Gamma (stabilising) or
    Short Gamma (amplifying) mode.

    This layer modifies ALL other signal weights via its multiplier.
    """

    @property
    def name(self) -> str:
        return "OPTIONS"

    def __init__(self, settings: OptionsSettings | None = None):
        cfg = settings or OptionsSettings()
        self._cfg = cfg
        self._gex_history: deque[float] = deque(maxlen=cfg.gex_history_window * 4)
        self._last_regime: RegimeInfo | None = None

    def reset(self) -> None:
        self._gex_history.clear()
        self._last_regime = None

    def calculate_gex(
        self, options_chain: list[dict], spot_price: float
    ) -> float:
        total_gex = 0.0
        for opt in options_chain:
            gamma = opt.get("gamma", 0.0)
            oi = opt.get("open_interest", 0.0)
            sign = 1 if opt.get("type") == "call" else -1
            gex = (
                sign * gamma * oi * self._cfg.btc_contract_size
                * spot_price ** 2 / 100
            )
            total_gex += gex
        return total_gex

    def calculate_pcr(self, options_chain: list[dict]) -> float | None:
        put_oi = sum(
            o.get("open_interest", 0) for o in options_chain if o.get("type") == "put"
        )
        call_oi = sum(
            o.get("open_interest", 0) for o in options_chain if o.get("type") == "call"
        )
        if call_oi <= 0:
            return None
        return put_oi / call_oi

    def detect_regime(
        self, gex: float, pcr: float | None
    ) -> RegimeInfo:
        self._gex_history.append(gex)

        hist = list(self._gex_history)
        window = hist[-self._cfg.gex_history_window :]
        avg_gex = sum(window) / len(window) if window else 0.0
        gex_momentum = gex - avg_gex

        regime = RegimeType.LONG_GAMMA if gex > 0 else RegimeType.SHORT_GAMMA

        flip_risk = False
        if avg_gex != 0:
            flip_risk = gex_momentum < -self._cfg.gex_flip_momentum_factor * abs(avg_gex)

        if pcr is not None and pcr < self._cfg.extreme_call_pcr:
            pcr_signal = "EXTREME_CALL_BUYING"
        elif pcr is not None and pcr > self._cfg.extreme_put_pcr:
            pcr_signal = "EXTREME_PUT_BUYING"
        else:
            pcr_signal = "NORMAL"

        multiplier = self._compute_multiplier(regime, flip_risk)
        description = self._describe(regime, flip_risk, pcr_signal)

        info = RegimeInfo(
            regime=regime,
            gex_value=gex,
            pcr_value=pcr,
            flip_risk=flip_risk,
            pcr_signal=pcr_signal,
            multiplier=multiplier,
            description=description,
        )
        self._last_regime = info
        return info

    def _compute_multiplier(self, regime: RegimeType, flip_risk: bool) -> float:
        if flip_risk:
            return self._cfg.flip_risk_multiplier
        if regime == RegimeType.SHORT_GAMMA:
            return self._cfg.short_gamma_multiplier
        return self._cfg.long_gamma_multiplier

    def apply_regime_to_score(self, score: float, regime: RegimeInfo) -> float:
        """Apply regime modifier and PCR bonus to a weighted score."""
        result = score * regime.multiplier

        if regime.pcr_signal == "EXTREME_CALL_BUYING" and result > 0:
            result += self._cfg.pcr_score_bonus
        elif regime.pcr_signal == "EXTREME_PUT_BUYING" and result < 0:
            result -= self._cfg.pcr_score_bonus

        return result

    def _describe(
        self, regime: RegimeType, flip_risk: bool, pcr_signal: str
    ) -> str:
        parts = [f"MM {regime.value}"]
        if flip_risk:
            parts.append("GEX FLIP RISK — volatility spike imminent")
        if pcr_signal == "EXTREME_CALL_BUYING":
            parts.append("Extreme call buying — gamma squeeze possible")
        elif pcr_signal == "EXTREME_PUT_BUYING":
            parts.append("Extreme put buying — bearish hedge pressure")
        return " | ".join(parts)

    def update(self, data: dict) -> SignalOutput:
        """
        Expected data: {options_chain: [...], spot_price: float}
        """
        options = data.get("options_chain", [])
        spot = data.get("spot_price", 0.0)

        if not options or spot <= 0:
            return SignalOutput(
                module=self.name,
                direction=SignalDirection.NEUTRAL,
                metadata={"reason": "no_options_data"},
            )

        gex = self.calculate_gex(options, spot)
        pcr = self.calculate_pcr(options)
        regime = self.detect_regime(gex, pcr)

        direction = SignalDirection.NEUTRAL
        if regime.regime == RegimeType.SHORT_GAMMA and not regime.flip_risk:
            direction = SignalDirection.BULLISH
        elif regime.flip_risk:
            direction = SignalDirection.BEARISH

        return SignalOutput(
            module=self.name,
            direction=direction,
            raw_value=gex,
            confidence=abs(regime.multiplier - 1.0),
            metadata={
                "regime": regime.regime.value,
                "gex": gex,
                "pcr": pcr,
                "flip_risk": regime.flip_risk,
                "pcr_signal": regime.pcr_signal,
                "multiplier": regime.multiplier,
            },
        )

    @property
    def last_regime(self) -> RegimeInfo | None:
        return self._last_regime
