"""RegimeDetector — classifies current market state.

Uses MA crossovers (50/200 SMA), volatility, and crisis event counting.
Returns: BULL, BEAR, SIDEWAYS, CRISIS

Per-market: each market (KRX, US, crypto) gets its own instance.
"""
import logging
from typing import Optional

import pandas as pd
import numpy as np

from framework.types import MarketRegime

logger = logging.getLogger(__name__)


class RegimeDetector:
    """Detects market regime from OHLCV data.

    Regime state machine:
        BULL:     SMA50 > SMA200, price > SMA50
        BEAR:     SMA50 < SMA200, price < SMA50
        SIDEWAYS: mixed signals (SMA50 ~ SMA200 or price between them)
        CRISIS:   N+ large drops (>threshold%) in rolling window
    """

    def __init__(
        self,
        market_name: str = "US",
        sma_short: int = 50,
        sma_long: int = 200,
        crisis_threshold_pct: float = -3.0,
        crisis_count: int = 4,
        crisis_window_days: int = 30,
    ):
        self.market_name = market_name
        self.sma_short = sma_short
        self.sma_long = sma_long
        self.crisis_threshold_pct = crisis_threshold_pct
        self.crisis_count = crisis_count
        self.crisis_window_days = crisis_window_days

    def detect(self, data: pd.DataFrame) -> MarketRegime:
        """Detect market regime from OHLCV data.

        Args:
            data: OHLCV DataFrame with at least sma_long rows.

        Returns:
            MarketRegime enum value.
        """
        if data is None or len(data) < self.sma_long:
            logger.warning(
                f"[{self.market_name}] Insufficient data for regime detection "
                f"({len(data) if data is not None else 0} < {self.sma_long})"
            )
            return MarketRegime.SIDEWAYS

        close = data["Close"].astype(float)

        # Check for crisis first (overrides other regimes)
        if self._is_crisis(close):
            return MarketRegime.CRISIS

        # MA-based regime
        sma_short = close.rolling(self.sma_short).mean()
        sma_long = close.rolling(self.sma_long).mean()

        current_price = close.iloc[-1]
        current_sma_short = sma_short.iloc[-1]
        current_sma_long = sma_long.iloc[-1]

        if pd.isna(current_sma_short) or pd.isna(current_sma_long):
            return MarketRegime.SIDEWAYS

        if current_sma_short > current_sma_long and current_price > current_sma_short:
            return MarketRegime.BULL
        elif current_sma_short < current_sma_long and current_price < current_sma_short:
            return MarketRegime.BEAR
        else:
            return MarketRegime.SIDEWAYS

    def _is_crisis(self, close: pd.Series) -> bool:
        """Check if there are N+ large drops in the rolling window."""
        if len(close) < self.crisis_window_days + 1:
            return False

        daily_returns = close.pct_change() * 100  # percentage
        recent = daily_returns.iloc[-self.crisis_window_days:]
        big_drops = (recent <= self.crisis_threshold_pct).sum()

        return big_drops >= self.crisis_count
