"""Tests for JD Strategy state machine and signal generation."""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from strategies.jd_strategy import JDStrategy, JDMarketState
from framework.types import SignalDirection, MarketRegime, Portfolio
from tests.conftest import make_ohlcv


@pytest.fixture
def jd_config():
    return {
        "TOP_STOCK_CANDIDATES": ["AAPL", "MSFT"],
        "MARKET_CAP_THRESHOLD": 0.10,
        "MINUS_3_THRESHOLD": -3.0,
        "PANIC_THRESHOLD": 4,
        "ROLLING_WINDOW_DAYS": 30,
        "REBALANCING_THRESHOLDS": {-2.5: 0.10, -5.0: 0.20, -7.5: 0.30},
        "STAKE_25_INTERVALS": [-5, -10, -15, -20, -25],
        "STAKE_25_TARGETS": [0.05, 0.10, 0.15, 0.20, 0.25],
        "MIN_TRADE_SIZE_USD": 10,
    }


@pytest.fixture
def strategy(jd_config):
    s = JDStrategy(jd_config, state_dir="/tmp")
    s.state = s.get_default_state()
    return s


class TestJDStateTransitions:
    def test_starts_in_normal(self, strategy):
        assert strategy._get_market_state() == JDMarketState.NORMAL

    def test_normal_to_alert_on_crisis(self, strategy):
        """A -3% drop transitions from NORMAL to ALERT."""
        # Create data where the last day has a -4% drop
        data = make_ohlcv(100, start_price=100, trend=0.0005)
        # Override last day to be a crash
        data.iloc[-1, data.columns.get_loc("Close")] = float(data.iloc[-2]["Close"]) * 0.96

        signals = strategy.generate_signals(data)
        assert strategy._get_market_state() == JDMarketState.ALERT

    def test_alert_to_panic_on_multiple_crises(self, strategy):
        """4+ drops in 30 days → ALERT → PANIC."""
        n = 100
        data = make_ohlcv(n, start_price=100)

        # Set state to ALERT first
        strategy.state["current_state"] = "alert"

        # Insert 5 large drops in last 30 days
        for offset in [5, 10, 15, 20, 25]:
            idx = n - offset
            data.iloc[idx, data.columns.get_loc("Close")] = float(data.iloc[idx - 1]["Close"]) * 0.95

        signals = strategy.generate_signals(data)
        assert strategy._get_market_state() == JDMarketState.PANIC

    def test_alert_to_normal_on_recovery(self, strategy):
        """0 crises in rolling window → ALERT → NORMAL."""
        # All gentle positive returns (no crashes)
        data = make_ohlcv(100, start_price=100, trend=0.001, volatility=0.005)
        strategy.state["current_state"] = "alert"

        signals = strategy.generate_signals(data)
        assert strategy._get_market_state() == JDMarketState.NORMAL

    def test_panic_to_normal_on_recovery(self, strategy):
        """0 crises in rolling window → PANIC → NORMAL."""
        data = make_ohlcv(100, start_price=100, trend=0.001, volatility=0.005)
        strategy.state["current_state"] = "panic"

        signals = strategy.generate_signals(data)
        assert strategy._get_market_state() == JDMarketState.NORMAL


class TestJDSignals:
    def test_normal_state_buy_signal(self, strategy):
        """NORMAL state with stock at/near ATH → BUY signal."""
        data = make_ohlcv(100, start_price=100, trend=0.001)
        # Set ATH to current price so drawdown is ~0% (no rebalancing needed)
        strategy.state["world_1_stock_ath"] = float(data["Close"].iloc[-1])

        signals = strategy.generate_signals(data)
        assert "AAPL" in signals
        assert signals["AAPL"].direction == SignalDirection.BUY

    def test_normal_state_rebalance_on_drawdown(self, strategy):
        """NORMAL state with stock -5% from ATH → SELL (rebalance) signal."""
        data = make_ohlcv(100, start_price=100, trend=-0.001)
        current = float(data["Close"].iloc[-1])
        strategy.state["world_1_stock_ath"] = current * 1.06  # stock is ~6% below ATH

        signals = strategy.generate_signals(data)
        assert signals["AAPL"].direction == SignalDirection.SELL
        assert signals["AAPL"].strength > 0  # target cash ratio

    def test_panic_holds_cash(self, strategy):
        """PANIC state → HOLD signal (all cash).

        Must insert enough crisis events to keep panic state from transitioning to normal.
        """
        n = 100
        data = make_ohlcv(n, start_price=100, volatility=0.005)
        strategy.state["current_state"] = "panic"

        # Insert 5 drops to keep crisis_count >= panic_threshold
        for offset in [3, 8, 13, 18, 23]:
            idx = n - offset
            data.iloc[idx, data.columns.get_loc("Close")] = float(data.iloc[idx - 1]["Close"]) * 0.95

        signals = strategy.generate_signals(data)
        assert signals["AAPL"].direction == SignalDirection.HOLD

    def test_insufficient_data_returns_hold(self, strategy):
        data = make_ohlcv(10)
        signals = strategy.generate_signals(data)
        assert signals["AAPL"].direction == SignalDirection.HOLD


class TestJDAllocation:
    def test_normal_full_investment(self, strategy):
        from framework.types import Signal
        signals = {"AAPL": Signal("AAPL", SignalDirection.BUY, strength=1.0)}
        portfolio = Portfolio(total_value=10000, cash_balance=10000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.BULL, portfolio)
        assert alloc["AAPL"] == 1.0

    def test_panic_zero_allocation(self, strategy):
        from framework.types import Signal
        strategy.state["current_state"] = "panic"
        signals = {"AAPL": Signal("AAPL", SignalDirection.HOLD)}
        portfolio = Portfolio(total_value=10000, cash_balance=10000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.BEAR, portfolio)
        assert alloc["AAPL"] == 0.0


class TestJDState:
    def test_default_state_has_required_keys(self, strategy):
        state = strategy.get_default_state()
        assert "current_state" in state
        assert "world_1_stock_ath" in state
        assert "minus_3_dates" in state
        assert "stake_levels_executed" in state

    def test_ath_tracking(self, strategy):
        """ATH should update when price exceeds previous ATH."""
        data = make_ohlcv(100, start_price=100, trend=0.002)
        strategy.state["world_1_stock_ath"] = 95.0  # Below current prices

        strategy.generate_signals(data)
        assert strategy.state["world_1_stock_ath"] > 95.0
