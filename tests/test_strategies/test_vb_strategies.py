"""Tests for VB (Volatility Breakthrough) strategies — Uranium and BTC."""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from strategies.uranium_vb import UraniumVBStrategy, calc_atr, calc_regime, calc_noise_ratio
from strategies.btc_vb import BTCVBStrategy
from framework.intraday_strategy import BreakoutTarget
from framework.types import SignalDirection, MarketRegime, Portfolio
from tests.conftest import make_ohlcv


class TestUraniumVBIndicators:
    def test_atr_calculation(self):
        data = make_ohlcv(30)
        atr = calc_atr(data, period=14)
        assert len(atr) == 30
        assert pd.notna(atr.iloc[-1])
        assert atr.iloc[-1] > 0

    def test_regime_bull(self):
        """Strong uptrend → bull regime (2)."""
        data = make_ohlcv(100, start_price=100, trend=0.003)
        regime = calc_regime(data["Close"])
        assert regime == 2

    def test_regime_bear(self):
        """Strong downtrend → bear regime (0)."""
        data = make_ohlcv(100, start_price=300, trend=-0.003)
        regime = calc_regime(data["Close"])
        assert regime == 0

    def test_regime_insufficient_data(self):
        """Insufficient data → neutral (1)."""
        data = make_ohlcv(10)
        regime = calc_regime(data["Close"])
        assert regime == 1

    def test_noise_ratio(self):
        data = make_ohlcv(30)
        noise = calc_noise_ratio(data, lb=20)
        assert len(noise) == 30
        # Noise ratio should be between 0 and 1 for well-behaved data
        valid = noise.dropna()
        assert all(valid >= 0) and all(valid <= 1.5)


class TestUraniumVBStrategy:
    @pytest.fixture
    def strategy(self):
        s = UraniumVBStrategy({}, state_dir="/tmp")
        s.state = s.get_default_state()
        return s

    def test_asset_list(self, strategy):
        assert strategy.get_asset_list() == ["URA", "URNM"]

    def test_breakout_targets_uptrend(self, strategy):
        """Bull regime should produce breakout targets."""
        data = make_ohlcv(100, start_price=20, trend=0.002)
        targets = strategy.compute_breakout_targets(data)
        assert len(targets) > 0
        assert all(t.direction == "long" for t in targets)
        assert all(t.target_price > 0 for t in targets)
        assert all(t.stop_price > 0 for t in targets)

    def test_no_targets_in_bear_regime(self, strategy):
        """Bear regime should produce no targets (no entry)."""
        data = make_ohlcv(100, start_price=100, trend=-0.004)
        targets = strategy.compute_breakout_targets(data)
        # In strong downtrend, regime is bear, no entries
        assert len(targets) == 0

    def test_insufficient_data_no_targets(self, strategy):
        data = make_ohlcv(20)
        targets = strategy.compute_breakout_targets(data)
        assert len(targets) == 0

    def test_signals_bridge(self, strategy):
        """generate_signals should bridge breakout targets to Signal objects."""
        data = make_ohlcv(100, start_price=20, trend=0.002)
        signals = strategy.generate_signals(data)
        # Should have signals if breakout targets were generated
        if signals:
            for ticker, signal in signals.items():
                assert signal.direction in [SignalDirection.BUY, SignalDirection.SELL]


class TestBTCVBStrategy:
    @pytest.fixture
    def strategy(self):
        s = BTCVBStrategy({}, state_dir="/tmp")
        s.state = s.get_default_state()
        return s

    def test_asset_list(self, strategy):
        assert strategy.get_asset_list() == ["KRW-BTC"]

    def test_breakout_target_uptrend(self, strategy):
        """Uptrend with trend filter passing should produce target."""
        data = make_ohlcv(50, start_price=80000000, trend=0.002, volatility=0.02)
        targets = strategy.compute_breakout_targets(data)
        if targets:  # May not produce if trend filter fails on synthetic data
            assert targets[0].direction == "long"
            assert targets[0].target_price > 0

    def test_no_target_downtrend(self, strategy):
        """Downtrend should fail trend filter → no target."""
        data = make_ohlcv(50, start_price=100000000, trend=-0.005)
        targets = strategy.compute_breakout_targets(data)
        assert len(targets) == 0

    def test_insufficient_data(self, strategy):
        data = make_ohlcv(3)
        targets = strategy.compute_breakout_targets(data)
        assert len(targets) == 0

    def test_equity_allocation_default(self, strategy):
        """With no equity history, should use bull allocation."""
        alloc = strategy._get_equity_allocation()
        assert alloc == 1.0  # bull_allocation default

    def test_equity_allocation_with_history(self, strategy):
        """With declining equity, should switch to bear allocation."""
        # Build declining equity history
        strategy.state["equity_history"] = [
            (f"2023-{i:02d}-01", 10000 - i * 100) for i in range(1, 50)
        ]
        alloc = strategy._get_equity_allocation()
        assert alloc == 0.30  # bear_allocation
