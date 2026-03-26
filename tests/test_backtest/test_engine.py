"""Tests for BacktestEngine — including the MOST CRITICAL look-ahead bias test."""
import pytest
import pandas as pd
import numpy as np
from datetime import date, timedelta

from framework.backtest.engine import BacktestEngine, BacktestBroker
from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio
from tests.conftest import MockDataProvider, make_ohlcv


class SpyStrategy(BaseStrategy):
    """Trivial strategy that records what data it sees (for look-ahead test)."""

    def __init__(self):
        super().__init__("test_spy", {"SPY_ALLOCATION": 0.6}, state_dir="/tmp")
        self.data_seen = []  # record the last date visible at each call

    def get_asset_list(self):
        return ["SPY"]

    def generate_signals(self, data):
        if not data.empty:
            self.data_seen.append(data.index[-1])
        return {"SPY": Signal("SPY", SignalDirection.BUY, strength=1.0)}

    def get_target_allocation(self, signals, regime, portfolio):
        return {"SPY": 1.0}


class TestBacktestBroker:
    def test_buy_order(self):
        broker = BacktestBroker(initial_cash=10000)
        from framework.types import Order
        result = broker.execute_at_price(Order("SPY", "buy", 10), 100.0)
        assert result.success
        assert broker.cash == 9000.0
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].ticker == "SPY"
        assert positions[0].quantity == 10

    def test_sell_order(self):
        broker = BacktestBroker(initial_cash=10000)
        from framework.types import Order
        broker.execute_at_price(Order("SPY", "buy", 10), 100.0)
        result = broker.execute_at_price(Order("SPY", "sell", 5), 110.0)
        assert result.success
        assert broker.cash == pytest.approx(9550.0)  # 9000 + 5*110

    def test_insufficient_cash(self):
        broker = BacktestBroker(initial_cash=100)
        from framework.types import Order
        result = broker.execute_at_price(Order("SPY", "buy", 10), 500.0)
        assert not result.success

    def test_sell_nonexistent_position(self):
        broker = BacktestBroker(initial_cash=10000)
        from framework.types import Order
        result = broker.execute_at_price(Order("SPY", "sell", 10), 100.0)
        assert not result.success


class TestBacktestEngineLookAhead:
    """MOST CRITICAL TEST: Verify BacktestEngine never leaks future data."""

    def test_no_future_data_visible(self):
        """Strategy must never see data from today or later."""
        # Create data with known dates
        spy_data = make_ohlcv(300, start_date="2023-01-01")
        provider = MockDataProvider({"SPY": spy_data})

        strategy = SpyStrategy()
        engine = BacktestEngine(strategy, provider, initial_cash=10000)

        bt_start = date(2023, 6, 1)
        bt_end = date(2023, 12, 29)
        engine.run(bt_start, bt_end)

        # For each simulated day, the strategy should only see data BEFORE that day
        trading_days = spy_data.index[
            (spy_data.index >= pd.Timestamp(bt_start)) &
            (spy_data.index <= pd.Timestamp(bt_end))
        ]

        for i, day in enumerate(trading_days):
            if i >= len(strategy.data_seen):
                break
            last_visible = strategy.data_seen[i]
            assert last_visible < day, (
                f"LOOK-AHEAD BIAS: On day {day}, strategy saw data from {last_visible}"
            )

    def test_empty_date_range(self):
        """Backtest with dates outside data range should return empty metrics."""
        spy_data = make_ohlcv(100, start_date="2023-01-01")
        provider = MockDataProvider({"SPY": spy_data})
        strategy = SpyStrategy()
        engine = BacktestEngine(strategy, provider)
        # Dates far outside the data range — no trading days found
        metrics = engine.run(date(2025, 1, 1), date(2025, 6, 1))
        assert metrics.num_trades == 0

    def test_single_day_backtest(self):
        """Backtest over a single day should work without error."""
        spy_data = make_ohlcv(300, start_date="2023-01-01")
        provider = MockDataProvider({"SPY": spy_data})
        strategy = SpyStrategy()
        engine = BacktestEngine(strategy, provider)
        metrics = engine.run(date(2023, 10, 1), date(2023, 10, 2))
        # Should complete without error
        assert metrics.start_value > 0


class TestBacktestMetrics:
    def test_metrics_from_equity_curve(self):
        from framework.backtest.metrics import compute_metrics
        # Simple doubling over 252 days = ~100% CAGR
        equity = [{"date": i, "value": 10000 + i * 39.68} for i in range(252)]
        returns = [0.003] * 252
        metrics = compute_metrics(equity, returns, 10000, [])
        assert metrics.cagr > 0
        assert metrics.total_return > 0
        assert metrics.max_drawdown <= 0  # MDD is negative or zero
