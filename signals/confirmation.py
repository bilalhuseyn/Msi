from __future__ import annotations

from dataclasses import dataclass, field

from config.constants import (
    ClearanceStatus,
    DecisionAction,
    SpreadStatus,
    VetoReason,
)
from config.settings import ConfirmationSettings
from signals.options_layer import RegimeInfo


@dataclass
class Decision:
    action: DecisionAction
    reason: str
    score: float
    raw_scores: dict = field(default_factory=dict)
    veto_reason: VetoReason = VetoReason.NONE

    @property
    def direction(self) -> int:
        if self.action == DecisionAction.LONG:
            return 1
        if self.action == DecisionAction.SHORT:
            return -1
        return 0


class ConfirmationEngine:
    """
    Combines signal modules into a single LONG/SHORT/NEUTRAL decision via
    weighted scoring, entry-blocking veto gates, spoof-OBI interaction,
    spread confidence multiplier, and MM regime modifiers.

    VETO gates block NEW entries only — they never force-close open positions.
    Open positions are managed by stop loss, take profit, and signal flips.

    Spread is a confidence multiplier (not a weighted directional score).
    """

    # Spread status → confidence multiplier mapping (P4 fix)
    _SPREAD_MULTIPLIERS: dict[str, float] = {
        SpreadStatus.MM_ACTIVE.value: 1.0,
        SpreadStatus.MM_CAUTIOUS.value: 0.7,
        SpreadStatus.MM_THINNING.value: 0.3,
    }

    def __init__(self, settings: ConfirmationSettings | None = None):
        cfg = settings or ConfirmationSettings()
        self._weights = cfg.weights
        self._long_thresh = cfg.long_threshold
        self._short_thresh = cfg.short_threshold
        self._vpin_veto_count = 0
        self._vpin_veto_required = 3

    def evaluate(
        self,
        signals: dict,
        vpin: float | None,
        spread_status: str,
        regime: RegimeInfo | None,
    ) -> Decision:
        # -- 1. Entry-blocking Veto Gates (P1: never force-close positions) --
        if vpin is not None and vpin > 0.90:
            self._vpin_veto_count += 1
            if self._vpin_veto_count >= self._vpin_veto_required:
                return Decision(
                    DecisionAction.VETO, "VPIN_CRITICAL", 0.0,
                    veto_reason=VetoReason.VPIN_CRITICAL,
                )
        else:
            self._vpin_veto_count = 0

        if spread_status == SpreadStatus.MM_WITHDRAWN.value:
            return Decision(
                DecisionAction.VETO, "SPREAD_CRISIS", 0.0,
                veto_reason=VetoReason.SPREAD_CRISIS,
            )
        clearance_st = signals.get("CLEARANCE_STATUS", "")
        if clearance_st == ClearanceStatus.CLEARANCE_ACTIVE.value:
            return Decision(
                DecisionAction.VETO, "CLEARANCE_ACTIVE", 0.0,
                veto_reason=VetoReason.CLEARANCE_ACTIVE,
            )

        # -- 2. Spoof → OBI Interaction (P6: float-aware) --
        obi_score = float(signals.get("OBI", 0.0))
        spoof_list = signals.get("SPOOF_LIST", [])

        if spoof_list:
            latest = spoof_list[-1]
            if latest.get("side") == "BID" and obi_score > 0:
                obi_score = -obi_score  # fake bid pressure → flip bullish OBI
            elif latest.get("side") == "ASK" and obi_score < 0:
                obi_score = -obi_score  # fake ask pressure → flip bearish OBI
            else:
                obi_score = 0.0
            if len(spoof_list) >= 3:
                obi_score = 0.0

        # -- 3. Weighted Score (OB + price-action modules) --
        raw = {
            "OBI": obi_score,
            "DEPTH": signals.get("DEPTH_SCORE", 0),
            "SPOOF": signals.get("SPOOF_SCORE", 0),
            "CLEARANCE": signals.get("CLEARANCE_SCORE", 0),
            "OPTIONS": signals.get("OPTIONS_SCORE", 0),
            "SR_PROXIMITY": signals.get("SR_PROXIMITY", 0),
            "MOMENTUM": signals.get("MOMENTUM", 0),
            "VOLUME_PROFILE": signals.get("VOLUME_PROFILE", 0),
            "HTF_TREND": signals.get("HTF_TREND", 0),
        }

        ready_flags = signals.get("_READY", {})
        active_weights: dict[str, float] = {}
        for k in raw:
            w = self._weights.get(k, 0)
            if w == 0:
                continue
            if k in ready_flags and not ready_flags[k]:
                continue
            active_weights[k] = w

        w_sum = sum(active_weights.values()) or 1.0
        score = sum(raw[k] * (active_weights[k] / w_sum) for k in active_weights)

        # -- 4. Spread Confidence Multiplier (P4: replaces directional score) --
        spread_mult = self._SPREAD_MULTIPLIERS.get(spread_status, 1.0)
        score *= spread_mult
        raw["SPREAD_MULT"] = spread_mult

        # -- 5. MM Regime Modifier --
        if regime is not None:
            score = self._apply_regime(score, regime)

        # -- 6. VPIN Warning Band (0.70–0.90) --
        if vpin is not None and 0.70 < vpin <= 0.90:
            score *= 0.50

        # -- 7. Final Decision --
        if score >= self._long_thresh:
            return Decision(DecisionAction.LONG, "CONFIRMED", score, raw)
        if score <= self._short_thresh:
            return Decision(DecisionAction.SHORT, "CONFIRMED", score, raw)
        return Decision(DecisionAction.NEUTRAL, "INSUFFICIENT", score, raw)

    def _apply_regime(self, score: float, regime: RegimeInfo) -> float:
        result = score * regime.multiplier

        if regime.pcr_signal == "EXTREME_CALL_BUYING" and result > 0:
            result += 0.10
        elif regime.pcr_signal == "EXTREME_PUT_BUYING" and result < 0:
            result -= 0.10

        return result
