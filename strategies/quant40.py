"""Quant 60/40 Strategy — ported from UsaStockAutoTrade.py.

Core logic:
  1. Calculate trend signal from 4 MA pairs on SPY using T-1 data
     MA pairs: (8,32), (16,64), (32,128), (64,256)
     Score 0-4: each pair where short > long = +1
     Signal mapping: 4→1.0, 3→0.5, 2→0.0, 1→-0.5, 0→-1.0
  2. SPY gets 60% static allocation + trend portion of 40%
  3. When signal is negative, SH (inverse ETF) gets a share of the 40%
  4. IEF (bonds) is held as the safe asset for remaining capital

Capital allocation respects the budget_ceiling from config.
"""
import logging
from typing import Any, Dict

import pandas as pd

from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio

logger = logging.getLogger(__name__)

# MA pair windows for trend signal
MA_PAIRS = [(8, 32), (16, 64), (32, 128), (64, 256)]

# Score-to-signal mapping
SIGNAL_MAP = {4: 1.0, 3: 0.5, 2: 0.0, 1: -0.5, 0: -1.0}


def compute_trend_signal(close: pd.Series):
    """Compute trend signal from 4 MA pairs.

    Args:
        close: Series of closing prices (at least 256 rows for full warmup).

    Returns:
        (signal_value, score) where signal is in [-1.0, 1.0] and score is 0-4.
    """
    if len(close) < 256:
        logger.warning(f"Insufficient data for trend signal: {len(close)} < 256")
        return 0.0, 2  # neutral

    score = 0
    for short_w, long_w in MA_PAIRS:
        short_ma = close.rolling(window=short_w).mean().iloc[-1]
        long_ma = close.rolling(window=long_w).mean().iloc[-1]
        if pd.notna(short_ma) and pd.notna(long_ma) and short_ma >= long_ma:
            score += 1

    return SIGNAL_MAP.get(score, 0.0), score


class Quant40Strategy(BaseStrategy):
    """Quant 60/40 with trend overlay.

    Trades: SPY (equity), IEF (bonds), SH (inverse equity).
    """

    def __init__(self, config: Dict[str, Any], state_dir: str = "."):
        super().__init__(name="quant40", config=config, state_dir=state_dir)
        self.spy_allocation = config.get("SPY_ALLOCATION", 0.60)
        self.trend_allocation = config.get("TREND_ALLOCATION", 0.40)
        self.equity_asset = config.get("EQUITY_ASSET", "SPY")
        self.safe_asset = config.get("SAFE_ASSET", "IEF")
        self.inverse_asset = config.get("INVERSE_EQUITY_ASSET", "SH")
        self.signal_asset = config.get("SIGNAL_ASSET", "SPY")
        self.min_rebalance_pct = config.get("MIN_REBALANCE_PCT", 0.05)

    def get_asset_list(self):
        return [self.equity_asset, self.safe_asset, self.inverse_asset]

    def generate_signals(self, data: pd.DataFrame) -> Dict[str, Signal]:
        """Generate signals based on SPY trend.

        Uses T-1 data (yesterday's close) to avoid look-ahead bias.
        The BacktestEngine already enforces this by slicing data before today.
        """
        if data.empty or len(data) < 256:
            return {
                self.equity_asset: Signal(self.equity_asset, SignalDirection.HOLD, reason="Insufficient data"),
            }

        close = data["Close"].astype(float)
        signal_value, score = compute_trend_signal(close)

        direction = SignalDirection.HOLD
        if signal_value > 0:
            direction = SignalDirection.BUY
        elif signal_value < 0:
            direction = SignalDirection.SELL

        return {
            self.equity_asset: Signal(
                self.equity_asset,
                direction,
                strength=abs(signal_value),
                reason=f"Trend score {score}/4 -> signal {signal_value}",
            ),
        }

    def get_target_allocation(
        self,
        signals: Dict[str, Signal],
        regime: MarketRegime,
        portfolio: Portfolio,
    ) -> Dict[str, float]:
        """Compute target allocation based on trend signal.

        Static 60% to SPY always.
        Trend 40% portion:
          signal >= 0: SPY gets (trend_allocation * signal), rest to IEF
          signal < 0:  SH gets (trend_allocation * |signal|), rest to IEF
        """
        spy_signal = signals.get(self.equity_asset)
        if spy_signal is None:
            # Default: 60/40 SPY/IEF
            return {
                self.equity_asset: self.spy_allocation,
                self.safe_asset: self.trend_allocation,
                self.inverse_asset: 0.0,
            }

        signal_value = spy_signal.strength
        if spy_signal.direction == SignalDirection.SELL:
            signal_value = -signal_value

        # Static portion: always SPY
        spy_pct = self.spy_allocation

        # Trend portion
        if signal_value >= 0:
            spy_trend = self.trend_allocation * signal_value
            sh_pct = 0.0
        else:
            spy_trend = 0.0
            sh_pct = self.trend_allocation * abs(signal_value)

        spy_pct += spy_trend
        ief_pct = max(0.0, 1.0 - spy_pct - sh_pct)

        return {
            self.equity_asset: spy_pct,
            self.safe_asset: ief_pct,
            self.inverse_asset: sh_pct,
        }

    def get_params_for_regime(self, regime: MarketRegime) -> Dict[str, Any]:
        """Adapt parameters for regime (Phase 3 — placeholder for now)."""
        params = dict(self.config)
        if regime == MarketRegime.CRISIS:
            # In crisis: reduce equity, increase safe asset
            params["SPY_ALLOCATION"] = 0.30
            params["TREND_ALLOCATION"] = 0.20
        return params
