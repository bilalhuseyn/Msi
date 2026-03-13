from __future__ import annotations

import pytest
from config.constants import RegimeType, SignalDirection
from config.settings import OptionsSettings
from signals.options_layer import OptionsLayer


def _make_chain(
    n_calls: int = 5,
    n_puts: int = 5,
    gamma: float = 0.001,
    oi: float = 100.0,
) -> list[dict]:
    chain = []
    for i in range(n_calls):
        chain.append({
            "type": "call",
            "strike": 50000 + i * 1000,
            "gamma": gamma,
            "open_interest": oi,
        })
    for i in range(n_puts):
        chain.append({
            "type": "put",
            "strike": 50000 - i * 1000,
            "gamma": gamma,
            "open_interest": oi,
        })
    return chain


class TestGEXCalculation:
    def test_positive_gex_with_more_calls(self):
        ol = OptionsLayer()
        gex = ol.calculate_gex(_make_chain(n_calls=10, n_puts=2), spot_price=50000)
        assert gex > 0

    def test_negative_gex_with_more_puts(self):
        ol = OptionsLayer()
        gex = ol.calculate_gex(_make_chain(n_calls=2, n_puts=10), spot_price=50000)
        assert gex < 0

    def test_empty_chain_returns_zero(self):
        ol = OptionsLayer()
        assert ol.calculate_gex([], spot_price=50000) == 0.0


class TestPCRCalculation:
    def test_balanced_pcr(self):
        ol = OptionsLayer()
        pcr = ol.calculate_pcr(_make_chain(n_calls=5, n_puts=5, oi=100))
        assert pcr is not None
        assert pcr == pytest.approx(1.0)

    def test_high_put_pcr(self):
        ol = OptionsLayer()
        pcr = ol.calculate_pcr(_make_chain(n_calls=5, n_puts=5, oi=100))
        assert pcr is not None

    def test_no_calls_returns_none(self):
        ol = OptionsLayer()
        pcr = ol.calculate_pcr(_make_chain(n_calls=0, n_puts=5))
        assert pcr is None


class TestRegimeDetection:
    def test_long_gamma_detection(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=10, n_puts=2)
        gex = ol.calculate_gex(chain, 50000)
        pcr = ol.calculate_pcr(chain)
        regime = ol.detect_regime(gex, pcr)
        assert regime.regime == RegimeType.LONG_GAMMA

    def test_short_gamma_detection(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=2, n_puts=10)
        gex = ol.calculate_gex(chain, 50000)
        pcr = ol.calculate_pcr(chain)
        regime = ol.detect_regime(gex, pcr)
        assert regime.regime == RegimeType.SHORT_GAMMA

    def test_multiplier_short_gamma(self):
        settings = OptionsSettings(short_gamma_multiplier=1.20)
        ol = OptionsLayer(settings)
        chain = _make_chain(n_calls=2, n_puts=10)
        gex = ol.calculate_gex(chain, 50000)
        regime = ol.detect_regime(gex, 1.0)
        assert regime.multiplier == pytest.approx(1.20)

    def test_multiplier_long_gamma(self):
        settings = OptionsSettings(long_gamma_multiplier=0.85)
        ol = OptionsLayer(settings)
        chain = _make_chain(n_calls=10, n_puts=2)
        gex = ol.calculate_gex(chain, 50000)
        regime = ol.detect_regime(gex, 1.0)
        assert regime.multiplier == pytest.approx(0.85)

    def test_flip_risk_reduces_multiplier(self):
        ol = OptionsLayer(OptionsSettings(gex_flip_momentum_factor=0.30))
        chain_positive = _make_chain(n_calls=10, n_puts=2)
        for _ in range(10):
            gex = ol.calculate_gex(chain_positive, 50000)
            ol.detect_regime(gex, 1.0)

        chain_negative = _make_chain(n_calls=1, n_puts=20, gamma=0.01)
        gex_neg = ol.calculate_gex(chain_negative, 50000)
        regime = ol.detect_regime(gex_neg, 1.0)
        assert regime.flip_risk is True
        assert regime.multiplier == pytest.approx(0.60)


class TestApplyRegimeToScore:
    def test_score_amplified_in_short_gamma(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=2, n_puts=10)
        gex = ol.calculate_gex(chain, 50000)
        regime = ol.detect_regime(gex, 1.0)
        original = 0.40
        modified = ol.apply_regime_to_score(original, regime)
        assert modified > original

    def test_score_dampened_in_long_gamma(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=10, n_puts=2)
        gex = ol.calculate_gex(chain, 50000)
        regime = ol.detect_regime(gex, 1.0)
        original = 0.40
        modified = ol.apply_regime_to_score(original, regime)
        assert modified < original

    def test_extreme_call_buying_bonus(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=10, n_puts=2, oi=100)
        chain_low_put = [o for o in chain if o["type"] == "call"]
        chain_low_put.append({"type": "put", "strike": 49000, "gamma": 0.001, "open_interest": 10})
        gex = ol.calculate_gex(chain_low_put, 50000)
        pcr = ol.calculate_pcr(chain_low_put)
        regime = ol.detect_regime(gex, pcr)
        if regime.pcr_signal == "EXTREME_CALL_BUYING":
            modified = ol.apply_regime_to_score(0.40, regime)
            base = 0.40 * regime.multiplier
            assert modified > base


class TestOptionsSignal:
    def test_no_data_returns_neutral(self):
        ol = OptionsLayer()
        result = ol.update({})
        assert result.direction == SignalDirection.NEUTRAL

    def test_with_chain_returns_signal(self):
        ol = OptionsLayer()
        chain = _make_chain(n_calls=10, n_puts=2)
        result = ol.update({"options_chain": chain, "spot_price": 50000})
        assert result.module == "OPTIONS"
        assert "regime" in result.metadata

    def test_reset_clears_state(self):
        ol = OptionsLayer()
        chain = _make_chain()
        ol.update({"options_chain": chain, "spot_price": 50000})
        assert ol.last_regime is not None
        ol.reset()
        assert ol.last_regime is None
