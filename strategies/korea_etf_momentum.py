"""Korea ETF Momentum Rotation — ported from KoreaETFMomentumAutoTrade.py.

Strategy: Multi-TF Dual Momentum (1/3/6/12m weighted)
Universe: KODEX Silver Futures (144600) + TIGER Raw Materials (139220)
Rebalance: Monthly (first trading day of each month)
Absolute filter: 12m return > 0, else go to CASH

Backtest (2014-2026): CAGR 40.1%, MDD -21.7%, Sharpe 1.24

Momentum scoring:
  score = 0.4 * m1 + 0.3 * m3 + 0.2 * m6 + 0.1 * m12
  where m_N = (current_price / price_N_months_ago) - 1

Winner-takes-all: highest scoring qualified ETF gets 100% allocation.
If no ETF has positive 12m return → go to CASH (absolute momentum filter).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import numpy as np

from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio

logger = logging.getLogger(__name__)

# Momentum weights for multi-timeframe scoring
MOMENTUM_WEIGHTS = {1: 0.4, 3: 0.3, 6: 0.2, 12: 0.1}
# Lookback indices for monthly data (assuming monthly frequency)
LOOKBACK_MAP = {1: -2, 3: -4, 6: -7, 12: -13}


def calc_momentum_scores(monthly_prices: Dict[str, pd.Series]) -> Dict[str, Dict]:
    """Calculate multi-TF momentum scores for each ticker.

    Args:
        monthly_prices: {ticker: Series of monthly close prices}

    Returns:
        {ticker: {"score": float, "abs_mom_12m": float, "qualified": bool, "details": dict}}
    """
    results = {}
    for ticker, prices in monthly_prices.items():
        if prices is None or len(prices) < 13:
            results[ticker] = {"score": 0, "abs_mom_12m": 0, "qualified": False, "details": {}}
            continue

        current = float(prices.iloc[-1])
        returns = {}
        for months, idx in LOOKBACK_MAP.items():
            past = float(prices.iloc[idx])
            returns[months] = (current / past - 1) if past > 0 else 0

        score = sum(MOMENTUM_WEIGHTS[m] * returns[m] for m in MOMENTUM_WEIGHTS)
        qualified = returns[12] > 0  # absolute momentum filter

        results[ticker] = {
            "score": score,
            "abs_mom_12m": returns[12],
            "qualified": qualified,
            "price": current,
            "details": {f"{m}m": returns[m] * 100 for m in MOMENTUM_WEIGHTS},
        }

    return results


class KoreaETFMomentumStrategy(BaseStrategy):
    """Korea ETF momentum rotation — monthly rebalance."""

    def __init__(self, config: Dict[str, Any], state_dir: str = "."):
        super().__init__(name="korea_etf_momentum", config=config, state_dir=state_dir)
        self.etfs = config.get("etfs", {"144600": "KODEX Silver Futures", "139220": "TIGER Raw Materials"})
        self.invest_ratio = config.get("invest_ratio", 0.95)

    def get_params_for_regime(self, regime: MarketRegime) -> Dict[str, Any]:
        """Regime adaptation for momentum strategy.

        CRISIS: Go to cash regardless of momentum scores.
        BEAR: Reduce invest_ratio (hold more cash buffer).
        BULL/SIDEWAYS: Standard parameters.
        """
        params = dict(self.config)
        if regime == MarketRegime.CRISIS:
            params["invest_ratio"] = 0.0  # All cash in crisis
        elif regime == MarketRegime.BEAR:
            params["invest_ratio"] = 0.70  # Hold 30% cash buffer
        return params

    def get_asset_list(self) -> List[str]:
        return list(self.etfs.keys())

    def get_default_state(self) -> Dict[str, Any]:
        return {
            "last_rebalance_month": "",
            "target_ticker": None,
            "target_name": None,
        }

    def _should_rebalance(self, current_date) -> bool:
        """Check if we should rebalance (first trading day of new month)."""
        last_month = self.state.get("last_rebalance_month", "")
        if hasattr(current_date, 'strftime'):
            current_month = current_date.strftime("%Y-%m")
        else:
            current_month = str(current_date)[:7]
        return current_month != last_month

    def _resample_to_monthly(self, daily_data: pd.DataFrame) -> pd.Series:
        """Convert daily OHLCV to monthly close prices."""
        close = daily_data["Close"].astype(float)
        try:
            monthly = close.resample("ME").last().dropna()
        except ValueError:
            monthly = close.resample("M").last().dropna()
        return monthly

    def generate_signals(self, data: pd.DataFrame) -> Dict[str, Signal]:
        """Generate momentum signals.

        Note: In the full system, this needs monthly prices for ALL ETFs.
        In backtest mode, the engine provides data for the primary asset.
        For multi-asset momentum, we need the engine to provide all asset data.

        For now: if data has enough history, compute momentum on available data.
        The signal strength encodes which ETF to hold.
        """
        if data.empty or len(data) < 260:  # ~12 months of daily data
            return {list(self.etfs.keys())[0]: Signal(
                list(self.etfs.keys())[0], SignalDirection.HOLD,
                reason="Insufficient data for momentum calculation",
            )}

        # Check if we should rebalance
        current_date = data.index[-1]
        if not self._should_rebalance(current_date):
            target = self.state.get("target_ticker")
            if target:
                return {target: Signal(target, SignalDirection.BUY, strength=1.0,
                                       reason=f"Holding {target} until next rebalance")}
            return {list(self.etfs.keys())[0]: Signal(
                list(self.etfs.keys())[0], SignalDirection.HOLD,
                reason="Not rebalance day",
            )}

        # Compute monthly prices from daily data
        monthly = self._resample_to_monthly(data)
        if len(monthly) < 13:
            return {list(self.etfs.keys())[0]: Signal(
                list(self.etfs.keys())[0], SignalDirection.HOLD,
                reason=f"Need 13 months of data, have {len(monthly)}",
            )}

        # For single-asset backtest: compute momentum on the primary asset
        scores = calc_momentum_scores({list(self.etfs.keys())[0]: monthly})

        # Find best qualified
        qualified = {t: d for t, d in scores.items() if d["qualified"]}
        if qualified:
            best = max(qualified, key=lambda t: qualified[t]["score"])
            # Update state
            self.state["target_ticker"] = best
            self.state["target_name"] = self.etfs.get(best, best)
            if hasattr(current_date, 'strftime'):
                self.state["last_rebalance_month"] = current_date.strftime("%Y-%m")

            return {best: Signal(best, SignalDirection.BUY, strength=1.0,
                                 reason=f"Momentum winner: score={scores[best]['score']:.4f}")}
        else:
            # All negative 12m → go to cash
            self.state["target_ticker"] = "CASH"
            if hasattr(current_date, 'strftime'):
                self.state["last_rebalance_month"] = current_date.strftime("%Y-%m")
            return {list(self.etfs.keys())[0]: Signal(
                list(self.etfs.keys())[0], SignalDirection.SELL,
                strength=1.0,
                reason="All ETFs negative 12m return → CASH",
            )}

    def get_target_allocation(
        self,
        signals: Dict[str, Signal],
        regime: MarketRegime,
        portfolio: Portfolio,
    ) -> Dict[str, float]:
        """Winner-takes-all: 100% to the momentum winner, 0% to rest."""
        allocation = {t: 0.0 for t in self.etfs}
        for ticker, signal in signals.items():
            if signal.direction == SignalDirection.BUY and ticker in self.etfs:
                allocation[ticker] = self.invest_ratio
        return allocation
