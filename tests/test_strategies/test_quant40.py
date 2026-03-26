"""Tests for Quant40 strategy."""
import pytest
import pandas as pd
import numpy as np

from strategies.quant40 import Quant40Strategy, compute_trend_signal
from framework.types import SignalDirection, MarketRegime, Portfolio, Position
from tests.conftest import make_ohlcv


class TestTrendSignal:
    def test_strong_uptrend_gives_bullish_signal(self):
        """All 4 MA pairs bullish → score 4 → signal 1.0"""
        # Strong uptrend: monotonically increasing prices
        close = pd.Series(range(100, 400), dtype=float)
        signal, score = compute_trend_signal(close)
        assert score == 4
        assert signal == 1.0

    def test_strong_downtrend_gives_bearish_signal(self):
        """All 4 MA pairs bearish → score 0 → signal -1.0"""
        close = pd.Series(range(400, 100, -1), dtype=float)
        signal, score = compute_trend_signal(close)
        assert score == 0
        assert signal == -1.0

    def test_insufficient_data_returns_neutral(self):
        """Less than 256 data points → neutral signal"""
        close = pd.Series(range(100), dtype=float)
        signal, score = compute_trend_signal(close)
        assert signal == 0.0
        assert score == 2

    def test_flat_prices_return_mixed(self):
        """Flat prices → all MAs equal → score depends on >= comparison"""
        close = pd.Series([100.0] * 300)
        signal, score = compute_trend_signal(close)
        # With flat prices, short MA = long MA, so >= is True → score 4
        assert score == 4

    def test_known_data_reproducible(self):
        """Same input always gives same output."""
        data = make_ohlcv(300, start_price=100, trend=0.001)
        s1, sc1 = compute_trend_signal(data["Close"])
        s2, sc2 = compute_trend_signal(data["Close"])
        assert s1 == s2
        assert sc1 == sc2


class TestQuant40Strategy:
    @pytest.fixture
    def strategy(self):
        config = {
            "SPY_ALLOCATION": 0.60,
            "TREND_ALLOCATION": 0.40,
            "EQUITY_ASSET": "SPY",
            "SAFE_ASSET": "IEF",
            "INVERSE_EQUITY_ASSET": "SH",
            "SIGNAL_ASSET": "SPY",
        }
        return Quant40Strategy(config, state_dir="/tmp")

    def test_asset_list(self, strategy):
        assert strategy.get_asset_list() == ["SPY", "IEF", "SH"]

    def test_signal_with_uptrend_data(self, strategy):
        data = make_ohlcv(300, start_price=100, trend=0.001)
        signals = strategy.generate_signals(data)
        assert "SPY" in signals
        assert signals["SPY"].direction == SignalDirection.BUY

    def test_signal_with_downtrend_data(self, strategy):
        data = make_ohlcv(300, start_price=400, trend=-0.002)
        signals = strategy.generate_signals(data)
        assert "SPY" in signals
        assert signals["SPY"].direction == SignalDirection.SELL

    def test_signal_with_insufficient_data(self, strategy):
        data = make_ohlcv(50)
        signals = strategy.generate_signals(data)
        assert signals["SPY"].direction == SignalDirection.HOLD

    def test_allocation_bullish(self, strategy):
        from framework.types import Signal
        signals = {"SPY": Signal("SPY", SignalDirection.BUY, strength=1.0)}
        portfolio = Portfolio(total_value=10000, cash_balance=10000, budget_ceiling=25000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.BULL, portfolio)
        # 60% static + 40% * 1.0 trend = 100% SPY
        assert alloc["SPY"] == pytest.approx(1.0)
        assert alloc["SH"] == pytest.approx(0.0)
        assert alloc["IEF"] == pytest.approx(0.0)

    def test_allocation_bearish(self, strategy):
        from framework.types import Signal
        signals = {"SPY": Signal("SPY", SignalDirection.SELL, strength=1.0)}
        portfolio = Portfolio(total_value=10000, cash_balance=10000, budget_ceiling=25000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.BEAR, portfolio)
        # 60% static SPY + 40% SH (fully bearish)
        assert alloc["SPY"] == pytest.approx(0.60)
        assert alloc["SH"] == pytest.approx(0.40)
        assert alloc["IEF"] == pytest.approx(0.0)

    def test_allocation_neutral(self, strategy):
        from framework.types import Signal
        signals = {"SPY": Signal("SPY", SignalDirection.BUY, strength=0.0)}
        portfolio = Portfolio(total_value=10000, cash_balance=10000, budget_ceiling=25000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.SIDEWAYS, portfolio)
        # 60% SPY + 0% trend + 40% IEF
        assert alloc["SPY"] == pytest.approx(0.60)
        assert alloc["IEF"] == pytest.approx(0.40)
        assert alloc["SH"] == pytest.approx(0.0)

    def test_allocation_sums_to_one(self, strategy):
        from framework.types import Signal
        for strength in [0.0, 0.25, 0.5, 0.75, 1.0]:
            for direction in [SignalDirection.BUY, SignalDirection.SELL]:
                signals = {"SPY": Signal("SPY", direction, strength=strength)}
                portfolio = Portfolio(total_value=10000, cash_balance=10000)
                alloc = strategy.get_target_allocation(signals, MarketRegime.BULL, portfolio)
                total = sum(alloc.values())
                assert total == pytest.approx(1.0, abs=0.01), (
                    f"Allocation sums to {total} for strength={strength}, dir={direction}"
                )

    def test_crisis_regime_reduces_equity(self, strategy):
        params = strategy.get_params_for_regime(MarketRegime.CRISIS)
        assert params["SPY_ALLOCATION"] < strategy.spy_allocation
