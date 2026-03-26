"""JD Investment Strategy — ported from JDStrategyAutoTrade.py (1,928 lines).

Core strategy (stateful state machine):
  1. Hold world's #1 stock by market cap (AAPL/MSFT/NVDA etc.)
  2. Three-State Model: NORMAL → ALERT → PANIC
  3. Crisis signal: NASDAQ -3% daily drop
  4. NORMAL: Rebalancing protocol (progressive cash building at drawdown tiers)
  5. ALERT: Stake-in protocol (systematic buying during crashes)
  6. PANIC: 4+ crisis signals in 30 days → hold cash until recovery

State is CRITICAL — accumulated across daily runs. The engine loads state
before generate_signals() and saves after execution.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from framework.strategy import BaseStrategy
from framework.types import Signal, SignalDirection, MarketRegime, Portfolio

logger = logging.getLogger(__name__)


class JDMarketState(Enum):
    NORMAL = "normal"
    ALERT = "alert"
    PANIC = "panic"


class JDStrategy(BaseStrategy):
    """JD Investment Strategy — stateful state machine.

    State machine:
        NORMAL: Hold #1 stock. Rebalance cash when stock drops from ATH.
        ALERT:  Crisis detected (-3% NASDAQ drop). Stake-in protocol begins.
        PANIC:  4+ crises in 30 days. Hold cash, wait for recovery.

    Transitions:
        NORMAL → ALERT: any -3% NASDAQ daily drop
        ALERT → PANIC:  4+ drops in 30-day rolling window
        ALERT → NORMAL: 30-day window has 0 drops (recovery)
        PANIC → NORMAL: 30-day window has 0 drops (recovery)
    """

    def __init__(self, config: Dict[str, Any], state_dir: str = "."):
        super().__init__(name="jd_strategy", config=config, state_dir=state_dir)

        # Strategy parameters from config
        self.top_stock_candidates = config.get("TOP_STOCK_CANDIDATES", ["AAPL", "MSFT", "GOOGL", "NVDA", "AMZN"])
        self.market_cap_threshold = config.get("MARKET_CAP_THRESHOLD", 0.10)
        self.minus_3_threshold = config.get("MINUS_3_THRESHOLD", -3.0)
        self.panic_threshold = config.get("PANIC_THRESHOLD", 4)
        self.rolling_window_days = config.get("ROLLING_WINDOW_DAYS", 30)

        # Rebalancing tiers: drawdown% → target cash ratio
        self.rebalancing_thresholds = config.get("REBALANCING_THRESHOLDS", {
            -2.5: 0.10, -5.0: 0.20, -7.5: 0.30,
        })

        # Stake-in intervals and targets (easing plan)
        self.stake_25_intervals = config.get("STAKE_25_INTERVALS", [-5, -10, -15, -20, -25])
        self.stake_25_targets = config.get("STAKE_25_TARGETS", [0.05, 0.10, 0.15, 0.20, 0.25])

        # Safety
        self.min_trade_size = config.get("MIN_TRADE_SIZE_USD", 10)
        self.max_single_trade_pct = config.get("MAX_SINGLE_TRADE_PCT", 1.0)

        # The primary stock ticker (determined by market cap analysis)
        self.primary_ticker = "AAPL"  # default, updated by market cap analysis

    def get_params_for_regime(self, regime: MarketRegime) -> Dict[str, Any]:
        """JD Strategy regime adaptation.

        CRISIS: Tighten crisis thresholds — enter alert faster.
        BEAR: More conservative rebalancing (hold more cash earlier).
        BULL: Standard parameters.
        """
        params = dict(self.config)
        if regime == MarketRegime.CRISIS:
            params["MINUS_3_THRESHOLD"] = -2.0
            params["PANIC_THRESHOLD"] = 3
        elif regime == MarketRegime.BEAR:
            params["REBALANCING_THRESHOLDS"] = {-2.0: 0.15, -4.0: 0.25, -6.0: 0.35}
        return params

    def get_asset_list(self) -> List[str]:
        return self.top_stock_candidates[:2]  # top 2 candidates

    def get_default_state(self) -> Dict[str, Any]:
        return {
            "current_state": JDMarketState.NORMAL.value,
            "world_1_stock_ath": 0.0,
            "current_world_1_ticker": None,
            "nasdaq_ath_at_alert": 0.0,
            "minus_3_dates": [],  # ISO date strings of -3% events
            "stake_levels_executed": [],
            "consecutive_up_days": 0,
            "last_rebalance_date": None,
            "initial_investment_made": False,
        }

    def _get_market_state(self) -> JDMarketState:
        return JDMarketState(self.state.get("current_state", "normal"))

    def _count_crisis_events(self, nasdaq_returns: pd.Series) -> int:
        """Count -3% drops in the last 30 trading days."""
        recent = nasdaq_returns.iloc[-self.rolling_window_days:]
        return int((recent <= self.minus_3_threshold).sum())

    def _check_for_new_crisis(self, nasdaq_returns: pd.Series) -> bool:
        """Check if today is a new -3% day."""
        if nasdaq_returns.empty:
            return False
        today_return = float(nasdaq_returns.iloc[-1])
        return today_return <= self.minus_3_threshold

    def _calculate_drawdown_from_ath(self, current_price: float) -> float:
        """Calculate drawdown of primary stock from its ATH."""
        ath = self.state.get("world_1_stock_ath", current_price)
        if ath <= 0:
            return 0.0
        return ((current_price - ath) / ath) * 100

    def _get_target_cash_ratio(self, drawdown_pct: float) -> float:
        """Get target cash ratio based on stock drawdown from ATH."""
        target = 0.0
        for threshold, ratio in sorted(self.rebalancing_thresholds.items()):
            if drawdown_pct <= threshold:
                target = ratio
        return target

    def _get_stake_target_allocation(self, drawdown_pct: float) -> float:
        """Get target stock allocation during stake-in (ALERT/PANIC state)."""
        for i, interval in enumerate(self.stake_25_intervals):
            if drawdown_pct <= interval and i < len(self.stake_25_targets):
                level_key = f"stake_{interval}"
                if level_key not in self.state.get("stake_levels_executed", []):
                    return self.stake_25_targets[i]
        return 0.0

    def generate_signals(self, data: pd.DataFrame) -> Dict[str, Signal]:
        """Generate signals based on state machine + NASDAQ monitoring.

        The data DataFrame contains the primary stock's OHLCV.
        State machine transitions happen here based on accumulated state.
        """
        if data.empty or len(data) < 30:
            return {self.primary_ticker: Signal(self.primary_ticker, SignalDirection.HOLD, reason="Insufficient data")}

        close = data["Close"].astype(float)
        daily_returns = close.pct_change() * 100  # percentage returns

        current_price = float(close.iloc[-1])
        market_state = self._get_market_state()

        # Update ATH tracking
        if current_price > self.state.get("world_1_stock_ath", 0):
            self.state["world_1_stock_ath"] = current_price

        # Check for new crisis event
        new_crisis = self._check_for_new_crisis(daily_returns)
        crisis_count = self._count_crisis_events(daily_returns)

        # --- STATE MACHINE TRANSITIONS ---
        if market_state == JDMarketState.NORMAL:
            if new_crisis:
                self.state["current_state"] = JDMarketState.ALERT.value
                self.state["nasdaq_ath_at_alert"] = float(close.max())
                today_str = str(data.index[-1].date()) if hasattr(data.index[-1], 'date') else str(data.index[-1])
                dates = self.state.get("minus_3_dates", [])
                dates.append(today_str)
                self.state["minus_3_dates"] = dates[-30:]
                logger.info(f"[JD] NORMAL → ALERT: -3% drop detected ({daily_returns.iloc[-1]:.1f}%)")
                market_state = JDMarketState.ALERT

        elif market_state == JDMarketState.ALERT:
            if crisis_count >= self.panic_threshold:
                self.state["current_state"] = JDMarketState.PANIC.value
                logger.info(f"[JD] ALERT → PANIC: {crisis_count} crises in {self.rolling_window_days} days")
                market_state = JDMarketState.PANIC
            elif crisis_count == 0:
                self.state["current_state"] = JDMarketState.NORMAL.value
                self.state["stake_levels_executed"] = []
                logger.info("[JD] ALERT → NORMAL: 0 crises in rolling window")
                market_state = JDMarketState.NORMAL

        elif market_state == JDMarketState.PANIC:
            if crisis_count == 0:
                self.state["current_state"] = JDMarketState.NORMAL.value
                self.state["stake_levels_executed"] = []
                logger.info("[JD] PANIC → NORMAL: Recovery — 0 crises in rolling window")
                market_state = JDMarketState.NORMAL

        # --- GENERATE SIGNAL BASED ON STATE ---
        drawdown = self._calculate_drawdown_from_ath(current_price)

        if market_state == JDMarketState.NORMAL:
            target_cash = self._get_target_cash_ratio(drawdown)
            if target_cash > 0 and drawdown < -2.5:
                return {self.primary_ticker: Signal(
                    self.primary_ticker, SignalDirection.SELL,
                    strength=target_cash,
                    reason=f"NORMAL rebalance: {drawdown:.1f}% from ATH, target cash {target_cash:.0%}",
                )}
            else:
                return {self.primary_ticker: Signal(
                    self.primary_ticker, SignalDirection.BUY,
                    strength=1.0,
                    reason=f"NORMAL: hold/buy {self.primary_ticker}, {drawdown:.1f}% from ATH",
                )}

        elif market_state == JDMarketState.ALERT:
            stake_target = self._get_stake_target_allocation(drawdown)
            if stake_target > 0:
                return {self.primary_ticker: Signal(
                    self.primary_ticker, SignalDirection.BUY,
                    strength=stake_target,
                    reason=f"ALERT stake-in: buy at {drawdown:.1f}% drawdown, target {stake_target:.0%}",
                )}
            return {self.primary_ticker: Signal(
                self.primary_ticker, SignalDirection.HOLD,
                reason=f"ALERT: monitoring, {drawdown:.1f}% from ATH, {crisis_count} crises",
            )}

        else:  # PANIC
            return {self.primary_ticker: Signal(
                self.primary_ticker, SignalDirection.HOLD,
                reason=f"PANIC: holding cash, {crisis_count} crises in {self.rolling_window_days}d window",
            )}

    def get_target_allocation(
        self,
        signals: Dict[str, Signal],
        regime: MarketRegime,
        portfolio: Portfolio,
    ) -> Dict[str, float]:
        """Compute allocation from JD signals."""
        signal = signals.get(self.primary_ticker)
        if signal is None:
            return {self.primary_ticker: 0.0}

        market_state = self._get_market_state()

        if market_state == JDMarketState.NORMAL:
            if signal.direction == SignalDirection.SELL:
                # Rebalancing: strength = target cash ratio
                stock_pct = 1.0 - signal.strength
                return {self.primary_ticker: stock_pct}
            else:
                # Fully invested
                return {self.primary_ticker: 1.0}

        elif market_state == JDMarketState.ALERT:
            if signal.direction == SignalDirection.BUY:
                return {self.primary_ticker: signal.strength}
            return {self.primary_ticker: 0.0}  # Hold cash

        else:  # PANIC
            return {self.primary_ticker: 0.0}  # All cash
