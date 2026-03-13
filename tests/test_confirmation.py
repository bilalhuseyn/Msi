from __future__ import annotations

import pytest
from config.constants import (
    ClearanceStatus,
    DecisionAction,
    RegimeType,
    SpreadStatus,
    VetoReason,
)
from config.settings import ConfirmationSettings
from signals.confirmation import ConfirmationEngine, Decision
from signals.options_layer import RegimeInfo


def _regime(
    regime: str = "LONG_GAMMA",
    flip_risk: bool = False,
    pcr_signal: str = "NORMAL",
    multiplier: float = 0.85,
) -> RegimeInfo:
    return RegimeInfo(
        regime=RegimeType(regime),
        gex_value=100.0 if regime == "LONG_GAMMA" else -100.0,
        pcr_value=1.0,
        flip_risk=flip_risk,
        pcr_signal=pcr_signal,
        multiplier=multiplier,
        description="",
    )


class TestGlobalVeto:
    def test_vpin_critical_vetoes(self):
        ce = ConfirmationEngine()
        # VPIN > 0.90 requires 3 consecutive evaluations to trigger VETO
        for _ in range(3):
            d = ce.evaluate({}, vpin=0.95, spread_status="MM_ACTIVE", regime=None)
        assert d.action == DecisionAction.VETO
        assert d.veto_reason == VetoReason.VPIN_CRITICAL

    def test_spread_crisis_vetoes(self):
        ce = ConfirmationEngine()
        d = ce.evaluate({}, vpin=0.30, spread_status="MM_WITHDRAWN", regime=None)
        assert d.action == DecisionAction.VETO
        assert d.veto_reason == VetoReason.SPREAD_CRISIS

    def test_clearance_active_vetoes(self):
        ce = ConfirmationEngine()
        signals = {"CLEARANCE_STATUS": ClearanceStatus.CLEARANCE_ACTIVE.value}
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.action == DecisionAction.VETO
        assert d.veto_reason == VetoReason.CLEARANCE_ACTIVE

    def test_no_veto_on_safe_conditions(self):
        ce = ConfirmationEngine()
        d = ce.evaluate({}, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.action != DecisionAction.VETO


class TestSpoofOBIInteraction:
    def test_bid_spoof_reverses_bullish_obi(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": 1,
            "SPOOF_LIST": [{"side": "BID", "implication": "SELL"}],
            "SPREAD_SCORE": 0, "DEPTH_SCORE": 0,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.raw_scores.get("OBI") == -1

    def test_ask_spoof_reverses_bearish_obi(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": -1,
            "SPOOF_LIST": [{"side": "ASK", "implication": "BUY"}],
            "SPREAD_SCORE": 0, "DEPTH_SCORE": 0,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.raw_scores.get("OBI") == 1

    def test_three_spoofs_zero_obi(self):
        ce = ConfirmationEngine()
        spoofs = [
            {"side": "BID"}, {"side": "BID"}, {"side": "ASK"},
        ]
        signals = {
            "OBI": 1, "SPOOF_LIST": spoofs,
            "SPREAD_SCORE": 0, "DEPTH_SCORE": 0,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.raw_scores.get("OBI") == 0


class TestWeightedScoring:
    def test_strong_bullish_gives_long(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": 1, "SPREAD_SCORE": 1, "DEPTH_SCORE": 1,
            "SPOOF_SCORE": 1, "CLEARANCE_SCORE": 1, "OPTIONS_SCORE": 1,
            "OBI_HISTORY": [0.70] * 5,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.action == DecisionAction.LONG

    def test_strong_bearish_gives_short(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": -1, "SPREAD_SCORE": -1, "DEPTH_SCORE": -1,
            "SPOOF_SCORE": -1, "CLEARANCE_SCORE": -1, "OPTIONS_SCORE": -1,
            "OBI_HISTORY": [0.30] * 5,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.action == DecisionAction.SHORT

    def test_mixed_signals_give_neutral(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": 1, "SPREAD_SCORE": -1, "DEPTH_SCORE": 0,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert d.action == DecisionAction.NEUTRAL


class TestRegimeModifier:
    def test_short_gamma_amplifies_score(self):
        ce = ConfirmationEngine()
        regime = _regime("SHORT_GAMMA", multiplier=1.20)
        signals = {
            "OBI": 1, "SPREAD_SCORE": 1, "DEPTH_SCORE": 1,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
            "OBI_HISTORY": [0.70] * 5,
        }
        d_with = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=regime)
        d_without = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=None)
        assert abs(d_with.score) >= abs(d_without.score)

    def test_flip_risk_dampens_score(self):
        ce = ConfirmationEngine()
        regime = _regime("SHORT_GAMMA", flip_risk=True, multiplier=0.60)
        signals = {
            "OBI": 1, "SPREAD_SCORE": 1, "DEPTH_SCORE": 1,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
            "OBI_HISTORY": [0.70] * 5,
        }
        d = ce.evaluate(signals, vpin=0.30, spread_status="MM_ACTIVE", regime=regime)
        assert abs(d.score) < 1.0


class TestVPINWarning:
    def test_vpin_warning_halves_score(self):
        ce = ConfirmationEngine()
        signals = {
            "OBI": 1, "SPREAD_SCORE": 1, "DEPTH_SCORE": 1,
            "SPOOF_SCORE": 0, "CLEARANCE_SCORE": 0, "OPTIONS_SCORE": 0,
            "OBI_HISTORY": [0.70] * 5,
        }
        d_safe = ce.evaluate(signals, vpin=0.40, spread_status="MM_ACTIVE", regime=None)
        d_warn = ce.evaluate(signals, vpin=0.75, spread_status="MM_ACTIVE", regime=None)
        assert abs(d_warn.score) < abs(d_safe.score)


class TestDecisionProperties:
    def test_long_direction(self):
        d = Decision(DecisionAction.LONG, "test", 0.5)
        assert d.direction == 1

    def test_short_direction(self):
        d = Decision(DecisionAction.SHORT, "test", -0.5)
        assert d.direction == -1

    def test_neutral_direction(self):
        d = Decision(DecisionAction.NEUTRAL, "test", 0.0)
        assert d.direction == 0

    def test_veto_direction(self):
        d = Decision(DecisionAction.VETO, "test", 0.0)
        assert d.direction == 0
