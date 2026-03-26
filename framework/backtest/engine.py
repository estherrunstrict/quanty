"""BacktestEngine — the mechanism for "backtest = live" parity.

The central insight: strategies NEVER fetch their own data. The engine provides it.

LIVE MODE:
  Engine calls provider.get_historical_data(ticker, start, today-1)
  Engine calls strategy.generate_signals(data)  # data ends at yesterday
  Engine calls broker.place_order(...)           # real KIS/Upbit broker

BACKTEST MODE:
  For each simulated day D in [start_date..end_date]:
    Engine slices historical_data[:D-1]          # STRICT: no future data
    Engine calls strategy.generate_signals(sliced_data)
    Engine calls backtest_broker.place_order(...) # simulated execution

Look-ahead bias is IMPOSSIBLE because:
  1. Strategies never call data providers directly
  2. BacktestEngine slices data BEFORE passing it to the strategy
  3. BacktestBroker executes at D's open price (not close)
"""
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

from framework.strategy import BaseStrategy
from framework.data.provider import BaseDataProvider
from framework.types import (
    MarketRegime, Order, OrderResult, Portfolio, Position,
    Signal, SignalDirection,
)
from framework.backtest.metrics import BacktestMetrics, compute_metrics

logger = logging.getLogger(__name__)


class BacktestBroker:
    """Simulates order execution against historical prices.

    Fills orders at the NEXT day's open price (no look-ahead on close).
    """

    def __init__(self, initial_cash: float = 10000.0):
        self._cash = initial_cash
        self._positions: Dict[str, Position] = {}
        self._trade_log: List[dict] = []

    @property
    def cash(self) -> float:
        return self._cash

    def get_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_cash_balance(self) -> float:
        return self._cash

    def get_portfolio_value(self, prices: Dict[str, float]) -> float:
        total = self._cash
        for ticker, pos in self._positions.items():
            price = prices.get(ticker, pos.avg_price)
            total += pos.quantity * price
        return total

    def execute_at_price(self, order: Order, fill_price: float) -> OrderResult:
        """Execute an order at a specific price (used by backtest engine)."""
        if fill_price <= 0:
            return OrderResult(False, order.ticker, order.side, 0, message="Invalid price")

        if order.side == "buy":
            cost = order.quantity * fill_price
            if cost > self._cash:
                # Buy what we can afford
                affordable_qty = int(self._cash / fill_price)
                if affordable_qty <= 0:
                    return OrderResult(False, order.ticker, "buy", 0, message="Insufficient cash")
                order = Order(order.ticker, "buy", affordable_qty, exchange=order.exchange)
                cost = affordable_qty * fill_price

            self._cash -= cost
            if order.ticker in self._positions:
                old = self._positions[order.ticker]
                total_qty = old.quantity + order.quantity
                new_avg = (old.avg_price * old.quantity + fill_price * order.quantity) / total_qty
                self._positions[order.ticker] = Position(
                    order.ticker, total_qty, new_avg, total_qty * fill_price
                )
            else:
                self._positions[order.ticker] = Position(
                    order.ticker, order.quantity, fill_price, order.quantity * fill_price
                )

        elif order.side == "sell":
            if order.ticker not in self._positions:
                return OrderResult(False, order.ticker, "sell", 0, message="No position")
            pos = self._positions[order.ticker]
            sell_qty = min(order.quantity, pos.quantity)
            proceeds = sell_qty * fill_price
            self._cash += proceeds

            remaining = pos.quantity - sell_qty
            if remaining > 0:
                self._positions[order.ticker] = Position(
                    order.ticker, remaining, pos.avg_price, remaining * fill_price
                )
            else:
                del self._positions[order.ticker]

        self._trade_log.append({
            "ticker": order.ticker,
            "side": order.side,
            "quantity": order.quantity,
            "price": fill_price,
        })
        return OrderResult(True, order.ticker, order.side, order.quantity, fill_price)


class BacktestEngine:
    """Runs a strategy over historical data with look-ahead prevention."""

    def __init__(
        self,
        strategy: BaseStrategy,
        data_provider: BaseDataProvider,
        initial_cash: float = 10000.0,
    ):
        self.strategy = strategy
        self.data_provider = data_provider
        self.initial_cash = initial_cash

    def run(
        self,
        start_date: date,
        end_date: date,
        regime_detector=None,
    ) -> BacktestMetrics:
        """Run backtest over [start_date, end_date].

        For each trading day D:
          1. Slice data up to D-1 (STRICT: no future data)
          2. Detect regime from sliced data
          3. Generate signals from sliced data
          4. Compute target allocation
          5. Execute orders at D's open price
        """
        assets = self.strategy.get_asset_list()
        broker = BacktestBroker(self.initial_cash)

        # Pre-fetch all data (provider enforces end bound)
        # We fetch extra history before start_date for indicator warmup
        warmup_start = start_date - timedelta(days=400)
        all_data: Dict[str, pd.DataFrame] = {}
        for ticker in assets:
            all_data[ticker] = self.data_provider.get_historical_data(
                ticker, warmup_start, end_date
            )

        # Build a combined price dataframe for the primary asset
        primary_ticker = assets[0]
        if all_data[primary_ticker].empty:
            logger.warning(f"No data for primary asset {primary_ticker}")
            return compute_metrics([], [], self.initial_cash, [])

        # Get trading days from the primary asset's data within our backtest window
        start_ts = pd.Timestamp(start_date)
        end_ts = pd.Timestamp(end_date)
        primary_idx = all_data[primary_ticker].index
        if primary_idx.tz is not None:
            start_ts = start_ts.tz_localize(primary_idx.tz)
            end_ts = end_ts.tz_localize(primary_idx.tz)

        trading_days = primary_idx[(primary_idx >= start_ts) & (primary_idx <= end_ts)]

        equity_curve = []
        daily_returns = []
        prev_value = self.initial_cash

        # Initialize strategy state for backtest
        self.strategy.state = self.strategy.get_default_state()

        for day in trading_days:
            # STRICT: slice data up to previous trading day (no look-ahead)
            sliced_data = {}
            day_open_prices = {}
            for ticker in assets:
                df = all_data[ticker]
                # Data visible to strategy: everything BEFORE today
                visible = df[df.index < day]
                sliced_data[ticker] = visible
                # Today's open price for execution
                if day in df.index:
                    day_open_prices[ticker] = float(df.loc[day, "Open"])

            # Build combined DataFrame for strategy (primary asset)
            primary_visible = sliced_data[primary_ticker]
            if primary_visible.empty or len(primary_visible) < 20:
                continue

            # Detect regime
            regime = MarketRegime.SIDEWAYS
            if regime_detector is not None:
                regime = regime_detector.detect(primary_visible)
                if regime != self.strategy.current_regime:
                    self.strategy.on_regime_change(self.strategy.current_regime, regime)

            # Generate signals
            signals = self.strategy.generate_signals(primary_visible)

            # Build portfolio state
            current_prices = {}
            for ticker in assets:
                vis = sliced_data[ticker]
                if not vis.empty:
                    current_prices[ticker] = float(vis["Close"].iloc[-1])

            portfolio_value = broker.get_portfolio_value(current_prices)
            portfolio = Portfolio(
                positions=broker.get_positions(),
                cash_balance=broker.get_cash_balance(),
                total_value=portfolio_value,
                budget_ceiling=self.initial_cash * 2,
            )

            # Get target allocation
            allocation = self.strategy.get_target_allocation(signals, regime, portfolio)

            # Execute rebalancing orders at today's OPEN price
            self._execute_rebalance(broker, allocation, portfolio, day_open_prices)

            # Record equity
            day_value = broker.get_portfolio_value(
                day_open_prices if day_open_prices else current_prices
            )
            equity_curve.append({"date": day, "value": day_value})
            if prev_value > 0:
                daily_returns.append((day_value - prev_value) / prev_value)
            prev_value = day_value

        return compute_metrics(
            equity_curve=equity_curve,
            daily_returns=daily_returns,
            initial_cash=self.initial_cash,
            trade_log=broker._trade_log,
        )

    def _execute_rebalance(
        self,
        broker: BacktestBroker,
        target_allocation: Dict[str, float],
        portfolio: Portfolio,
        open_prices: Dict[str, float],
    ):
        """Rebalance portfolio toward target allocation at open prices."""
        if not open_prices or portfolio.total_value <= 0:
            return

        total_value = portfolio.total_value

        # Calculate target values
        target_values = {}
        for ticker, pct in target_allocation.items():
            if ticker.upper() == "CASH":
                continue
            target_values[ticker] = total_value * pct

        # Sell first (to free up cash)
        for ticker in list(target_values.keys()):
            if ticker not in open_prices:
                continue
            price = open_prices[ticker]
            pos = portfolio.get_position(ticker)
            current_value = (pos.quantity * price) if pos else 0
            target = target_values[ticker]

            if current_value > target and pos:
                sell_value = current_value - target
                sell_qty = int(sell_value / price)
                if sell_qty > 0:
                    broker.execute_at_price(
                        Order(ticker, "sell", sell_qty), price
                    )

        # Then buy
        for ticker, target in target_values.items():
            if ticker not in open_prices:
                continue
            price = open_prices[ticker]
            pos = portfolio.get_position(ticker)
            current_value = (pos.quantity * price) if pos else 0

            if target > current_value:
                buy_value = target - current_value
                buy_qty = int(buy_value / price)
                if buy_qty > 0 and broker.get_cash_balance() >= price:
                    broker.execute_at_price(
                        Order(ticker, "buy", buy_qty), price
                    )
