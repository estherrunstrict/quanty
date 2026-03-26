"""BaseStrategy ABC — the interface every strategy implements.

Engine lifecycle per run:
  1. engine loads strategy state via strategy.load_state()
  2. regime = detector.detect(data)
  3. if regime != strategy.current_regime:
       strategy.on_regime_change(old, regime)
  4. signals = strategy.generate_signals(data)   # reads self.active_params
  5. allocation = strategy.get_target_allocation(signals, regime, portfolio)
  6. engine saves state via strategy.save_state()
  7. broker.execute(allocation)
"""
from __future__ import annotations

import json
import os
import shutil
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import pandas as pd

from framework.types import Signal, MarketRegime, Portfolio

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Base class for all daily-frequency strategies."""

    def __init__(self, name: str, config: Dict[str, Any], state_dir: str = "."):
        self.name = name
        self.config = config
        self.active_params = dict(config)
        self.current_regime: Optional[MarketRegime] = None
        self._state_dir = state_dir
        self._state_file = os.path.join(state_dir, f"{name}_state.json")
        self.state: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Abstract methods — subclasses MUST implement
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> Dict[str, Signal]:
        """Return buy/sell/hold signals for each asset.

        Args:
            data: OHLCV DataFrame with DatetimeIndex.
                  Columns: Open, High, Low, Close, Volume (all float).
                  In LIVE mode: data up to yesterday's close.
                  In BACKTEST mode: data up to the simulated "current date"
                  (future data is never visible — enforced by BacktestEngine).
        """

    @abstractmethod
    def get_target_allocation(
        self,
        signals: Dict[str, Signal],
        regime: MarketRegime,
        portfolio: Portfolio,
    ) -> Dict[str, float]:
        """Return target % allocation per asset.

        Returns a dict like {"SPY": 0.60, "IEF": 0.40, "CASH": 0.0}.
        Values should sum to <= 1.0.
        """

    @abstractmethod
    def get_asset_list(self) -> list[str]:
        """Return the list of tickers this strategy trades."""

    # ------------------------------------------------------------------
    # Regime adaptation
    # ------------------------------------------------------------------

    def get_params_for_regime(self, regime: MarketRegime) -> Dict[str, Any]:
        """Return parameter overrides for the given regime.

        Default: return base config (no override).
        Override in subclass to adapt parameters per regime.
        """
        return dict(self.config)

    def on_regime_change(self, old_regime: Optional[MarketRegime], new_regime: MarketRegime):
        """Hook called when regime changes. Updates active params."""
        self.current_regime = new_regime
        self.active_params = self.get_params_for_regime(new_regime)
        logger.info(f"[{self.name}] Regime changed: {old_regime} -> {new_regime}")

    # ------------------------------------------------------------------
    # State management (framework-managed)
    # ------------------------------------------------------------------

    def get_default_state(self) -> Dict[str, Any]:
        """Return the default state for a fresh strategy. Override in subclass."""
        return {}

    def load_state(self) -> Dict[str, Any]:
        """Load persistent state from JSON file. Creates default if missing."""
        if not os.path.exists(self._state_file):
            self.state = self.get_default_state()
            logger.info(f"[{self.name}] No state file found, using defaults")
            return self.state

        try:
            with open(self._state_file, "r") as f:
                self.state = json.load(f)
            logger.info(f"[{self.name}] State loaded from {self._state_file}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[{self.name}] Corrupt state file: {e}")
            bak = self._state_file + ".bak"
            if os.path.exists(bak):
                logger.info(f"[{self.name}] Recovering from backup {bak}")
                try:
                    with open(bak, "r") as f:
                        self.state = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    logger.error(f"[{self.name}] Backup also corrupt, using defaults")
                    self.state = self.get_default_state()
            else:
                self.state = self.get_default_state()
        return self.state

    def save_state(self):
        """Atomically save state to JSON file (write temp, rename)."""
        os.makedirs(self._state_dir, exist_ok=True)
        # Keep previous version as backup
        if os.path.exists(self._state_file):
            shutil.copy2(self._state_file, self._state_file + ".bak")

        tmp_file = self._state_file + ".tmp"
        try:
            with open(tmp_file, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
            os.replace(tmp_file, self._state_file)
            logger.debug(f"[{self.name}] State saved to {self._state_file}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to save state: {e}")
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
