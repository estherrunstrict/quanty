"""Tests for RegimeDetector."""
import pytest
import pandas as pd
import numpy as np

from framework.regime.detector import RegimeDetector
from framework.types import MarketRegime
from tests.conftest import make_ohlcv


class TestRegimeDetector:
    @pytest.fixture
    def detector(self):
        return RegimeDetector(market_name="US")

    def test_bull_regime_strong_uptrend(self, detector):
        data = make_ohlcv(300, start_price=100, trend=0.002)
        regime = detector.detect(data)
        assert regime == MarketRegime.BULL

    def test_bear_regime_strong_downtrend(self, detector):
        data = make_ohlcv(300, start_price=400, trend=-0.002)
        regime = detector.detect(data)
        assert regime == MarketRegime.BEAR

    def test_sideways_insufficient_data(self, detector):
        data = make_ohlcv(50)
        regime = detector.detect(data)
        assert regime == MarketRegime.SIDEWAYS

    def test_sideways_empty_data(self, detector):
        regime = detector.detect(pd.DataFrame())
        assert regime == MarketRegime.SIDEWAYS

    def test_sideways_none_data(self, detector):
        regime = detector.detect(None)
        assert regime == MarketRegime.SIDEWAYS

    def test_crisis_detection(self):
        """4+ drops > 3% in 30 days → CRISIS."""
        detector = RegimeDetector(crisis_threshold_pct=-3.0, crisis_count=4, crisis_window_days=30)

        # Build data with 5 large drops in last 30 days
        np.random.seed(123)
        n = 250
        dates = pd.bdate_range("2023-01-01", periods=n)
        prices = np.full(n, 100.0)
        # Normal returns most of the time
        for i in range(1, n):
            prices[i] = prices[i-1] * (1 + np.random.normal(0, 0.005))

        # Insert 5 large drops in the last 30 trading days
        for offset in [5, 10, 15, 20, 25]:
            prices[n - offset] = prices[n - offset - 1] * 0.95  # -5% drop

        df = pd.DataFrame({
            "Open": prices * 1.001,
            "High": prices * 1.01,
            "Low": prices * 0.99,
            "Close": prices,
            "Volume": np.full(n, 1e6),
        }, index=dates)

        regime = detector.detect(df)
        assert regime == MarketRegime.CRISIS

    def test_no_crisis_with_few_drops(self):
        """2 drops < threshold → not CRISIS."""
        detector = RegimeDetector(crisis_threshold_pct=-3.0, crisis_count=4)
        data = make_ohlcv(300, start_price=100, trend=-0.0005, volatility=0.008)
        regime = detector.detect(data)
        assert regime != MarketRegime.CRISIS

    def test_per_market_instance(self):
        """Each market gets its own detector."""
        us = RegimeDetector(market_name="US", crisis_threshold_pct=-3.0)
        kr = RegimeDetector(market_name="KRX", crisis_threshold_pct=-3.0)
        crypto = RegimeDetector(market_name="Crypto", crisis_threshold_pct=-5.0)

        assert us.market_name == "US"
        assert kr.market_name == "KRX"
        assert crypto.crisis_threshold_pct == -5.0
