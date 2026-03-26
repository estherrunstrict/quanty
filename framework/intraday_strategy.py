"""IntraDayStrategy — subclass for strategies that need live price feeds.

VB (Volatility Breakthrough) strategies check live prices during market hours
and trigger when price crosses a target level. This differs from BaseStrategy
which operates on daily OHLCV only.

In LIVE mode:
  - Engine provides yesterday's data for target calculation
  - Strategy returns a BreakoutTarget instead of a Signal
  - Engine (or scheduler) polls live prices and executes when target is hit

In BACKTEST mode:
  - Engine checks if day's High exceeded the target (simulates intraday cross)
  - If yes: fills at the target price
  - If no: no trade for that day
"""
from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio

logger = logging.getLogger(__name__)


@dataclass
class BreakoutTarget:
    """A price level that triggers a buy/sell when crossed intraday."""
    ticker: str
    target_price: float
    direction: str  # "long" or "short"
    size_pct: float  # position size as % of capital
    stop_price: float = 0.0
    reason: str = ""


class IntraDayStrategy(BaseStrategy):
    """Base class for intraday breakout strategies (VB pattern).

    Subclasses implement compute_breakout_targets() instead of generate_signals().
    generate_signals() is implemented here to bridge with the daily framework.
    """

    @abstractmethod
    def compute_breakout_targets(self, data: pd.DataFrame) -> List[BreakoutTarget]:
        """Compute intraday breakout target prices from historical daily data.

        Args:
            data: OHLCV DataFrame up to yesterday's close.

        Returns:
            List of BreakoutTarget — price levels to watch during today's session.
        """

    def generate_signals(self, data: pd.DataFrame) -> Dict[str, Signal]:
        """Bridge: convert breakout targets to daily signals for backtest compatibility.

        In backtest mode, the BacktestEngine checks if the day's High exceeded
        the target price. This method computes the targets and returns them
        as BUY signals with the target price encoded in the strength field.
        """
        targets = self.compute_breakout_targets(data)
        signals = {}
        for target in targets:
            signals[target.ticker] = Signal(
                ticker=target.ticker,
                direction=SignalDirection.BUY if target.direction == "long" else SignalDirection.SELL,
                strength=target.size_pct,
                reason=f"VB target: ${target.target_price:.2f} | {target.reason}",
            )
        return signals

    def get_target_allocation(
        self,
        signals: Dict[str, Signal],
        regime: MarketRegime,
        portfolio: Portfolio,
    ) -> Dict[str, float]:
        """For VB strategies, allocation is determined by breakout targets."""
        allocation = {}
        for ticker, signal in signals.items():
            if signal.direction == SignalDirection.BUY:
                allocation[ticker] = signal.strength  # size_pct from target
            else:
                allocation[ticker] = 0.0
        return allocation
