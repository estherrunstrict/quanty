"""BTC Volatility Breakthrough (Long-Only) — ported from upbit_vb_strategy_auto_trade.py.

Strategy: Larry Williams VB with Equity Momentum allocation
  - Entry: Open + 0.40 * ATR(7) breakout (long only)
  - Filter: Close > MA(5) trend filter
  - Exit: next day open (09:00 KST)
  - Allocation: 100% when equity > MA(40), 30% when below
  - Commission: 0.05% (Upbit KRW market)

Backtest parameters optimized from 21,660 combo grid search.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd
import numpy as np

from framework.intraday_strategy import IntraDayStrategy, BreakoutTarget
from framework.types import MarketRegime

logger = logging.getLogger(__name__)

DEFAULTS = {
    "ticker": "KRW-BTC",
    "k_long": 0.40,
    "atr_period": 7,
    "ma_period": 5,
    "equity_ma_period": 40,
    "bull_allocation": 1.00,
    "bear_allocation": 0.30,
}


class BTCVBStrategy(IntraDayStrategy):
    """BTC volatility breakthrough with equity momentum allocation.

    Daily flow:
      1. If holding from yesterday → exit at market open
      2. Calculate today's breakout target: Open + k * ATR
      3. Check trend filter: Close > MA(5)
      4. Determine allocation (equity momentum: equity curve > MA(40))
      5. If price crosses target during the day → enter long

    This is a 1-day hold strategy: enter on breakout, exit next open.
    """

    def __init__(self, config: Dict[str, Any], state_dir: str = "."):
        merged = {**DEFAULTS, **config}
        super().__init__(name="btc_vb", config=merged, state_dir=state_dir)
        self.ticker = merged["ticker"]
        self.k_long = merged["k_long"]
        self.atr_period = merged["atr_period"]
        self.ma_period = merged["ma_period"]
        self.equity_ma_period = merged["equity_ma_period"]
        self.bull_allocation = merged["bull_allocation"]
        self.bear_allocation = merged["bear_allocation"]

    def get_asset_list(self) -> List[str]:
        return [self.ticker]

    def get_default_state(self) -> Dict[str, Any]:
        return {
            "equity_history": [],  # [(date_str, equity_value), ...]
            "is_holding": False,
            "entry_price": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0.0,
            "current_allocation": 1.0,
        }

    def _calc_atr(self, data: pd.DataFrame) -> float:
        """Calculate ATR for breakout target."""
        h = data["High"].astype(float)
        l = data["Low"].astype(float)
        c = data["Close"].astype(float)
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()
        return float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0

    def _check_trend_filter(self, data: pd.DataFrame) -> bool:
        """Close > MA(5) trend filter."""
        close = data["Close"].astype(float)
        if len(close) < self.ma_period:
            return False
        ma = close.rolling(self.ma_period).mean()
        return float(close.iloc[-1]) > float(ma.iloc[-1])

    def _get_equity_allocation(self) -> float:
        """Determine allocation based on equity curve momentum."""
        history = self.state.get("equity_history", [])
        if len(history) < self.equity_ma_period:
            return self.bull_allocation  # Default to full allocation early on

        equity_values = [h[1] for h in history[-self.equity_ma_period:]]
        equity_ma = np.mean(equity_values)
        current_equity = equity_values[-1]

        if current_equity > equity_ma:
            return self.bull_allocation
        else:
            return self.bear_allocation

    def compute_breakout_targets(self, data: pd.DataFrame) -> List[BreakoutTarget]:
        """Compute BTC VB breakout target."""
        if data.empty or len(data) < max(self.atr_period, self.ma_period) + 5:
            return []

        # Trend filter must pass
        if not self._check_trend_filter(data):
            logger.debug(f"[BTC VB] Trend filter failed: Close < MA({self.ma_period})")
            return []

        # Calculate breakout target
        atr = self._calc_atr(data)
        if atr <= 0:
            return []

        prev_close = float(data["Close"].iloc[-1])
        target_price = prev_close + self.k_long * atr

        # Equity momentum allocation
        allocation = self._get_equity_allocation()

        return [BreakoutTarget(
            ticker=self.ticker,
            target_price=round(target_price, 0),  # BTC prices in KRW, round to whole
            direction="long",
            size_pct=allocation,
            reason=f"BTC VB: target={target_price:,.0f}, ATR={atr:,.0f}, alloc={allocation:.0%}",
        )]
