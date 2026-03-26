"""Uranium VB (Volatility Breakthrough) — ported from UraniumVBAutoTrade.py.

Strategy: Combined VB + Momentum for URNM + URA ETFs
  - VB Entry: price > prev_close + k * prev_range (adaptive k based on noise ratio)
  - 3-tier regime: BULL (1.3x size), NEUTRAL (0.7x), BEAR (0x — no entry)
  - Multi-day hold with trailing ATR stop (max 10 days)
  - Asset management: 2.5% risk/trade, 8% heat limit, drawdown scaling
  - Exit: trailing stop, max hold, regime bear, RSI overbought

Backtest: CAGR 30.5%, MDD -19.5%, Sharpe 1.10, Win Rate 51%
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd
import numpy as np

from framework.intraday_strategy import IntraDayStrategy, BreakoutTarget
from framework.types import MarketRegime

logger = logging.getLogger(__name__)

# Default parameters (from optimized grid search)
DEFAULTS = {
    "tickers": ["URA", "URNM"],
    "k_base": 0.3,
    "k_min": 0.25,
    "k_max": 0.7,
    "noise_lookback": 20,
    "regime_fast": 10,
    "regime_mid": 30,
    "regime_slow": 60,
    "atr_period": 14,
    "atr_stop_init": 1.2,
    "atr_trail": 2.5,
    "max_hold_days": 10,
    "risk_per_trade": 0.025,
    "heat_limit": 0.08,
    "max_position": 0.60,
    "regime_bull_mult": 1.3,
    "regime_neutral_mult": 0.7,
}


def calc_atr(ohlc: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = ohlc["High"], ohlc["Low"], ohlc["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_noise_ratio(ohlc: pd.DataFrame, lb: int = 20) -> pd.Series:
    c = ohlc["Close"]
    direction = (c - c.shift(lb)).abs()
    path = c.diff().abs().rolling(lb).sum()
    eff = direction / path.replace(0, np.nan)
    return 1 - eff


def calc_regime(close: pd.Series, fast: int = 10, mid: int = 30, slow: int = 60) -> int:
    """Returns 2=bull, 1=neutral, 0=bear."""
    if len(close) < slow + 5:
        return 1
    ma_f = close.rolling(fast).mean()
    ma_m = close.rolling(mid).mean()
    ma_s = close.rolling(slow).mean()
    bull = (ma_f.iloc[-1] > ma_m.iloc[-1]) and (ma_m.iloc[-1] > ma_s.iloc[-1]) and (close.iloc[-1] > ma_s.iloc[-1])
    bear = (close.iloc[-1] < ma_s.iloc[-1]) or ((ma_f.iloc[-1] < ma_m.iloc[-1]) and (ma_m.iloc[-1] < ma_s.iloc[-1]))
    if bull:
        return 2
    elif bear:
        return 0
    return 1


class UraniumVBStrategy(IntraDayStrategy):
    """Uranium ETF volatility breakthrough with regime filter."""

    def __init__(self, config: Dict[str, Any], state_dir: str = "."):
        merged = {**DEFAULTS, **config}
        super().__init__(name="uranium_vb", config=merged, state_dir=state_dir)
        self.tickers = merged["tickers"]
        self.k_base = merged["k_base"]
        self.k_min = merged["k_min"]
        self.k_max = merged["k_max"]
        self.noise_lookback = merged["noise_lookback"]
        self.risk_per_trade = merged["risk_per_trade"]
        self.regime_bull_mult = merged["regime_bull_mult"]
        self.regime_neutral_mult = merged["regime_neutral_mult"]
        self.atr_period = merged["atr_period"]
        self.atr_stop_init = merged["atr_stop_init"]

    def get_asset_list(self) -> List[str]:
        return self.tickers

    def get_default_state(self) -> Dict[str, Any]:
        return {
            "open_positions": {},
            "peak_equity": 0,
            "trade_history": [],
        }

    def compute_breakout_targets(self, data: pd.DataFrame) -> List[BreakoutTarget]:
        """Compute VB breakout targets for each uranium ticker."""
        targets = []

        if data.empty or len(data) < 70:
            return targets

        for ticker_idx, ticker in enumerate(self.tickers):
            # Use the primary data (in multi-asset backtest, engine provides per-asset)
            # For simplicity, use the same data for all tickers in single-asset mode
            close = data["Close"].astype(float)
            high = data["High"].astype(float)
            low = data["Low"].astype(float)

            # Adaptive k based on noise ratio
            noise = calc_noise_ratio(data, self.noise_lookback)
            k = self.k_base
            if not noise.empty and pd.notna(noise.iloc[-1]):
                k = np.clip(self.k_base * (1 + noise.iloc[-1]), self.k_min, self.k_max)

            # Previous day's range
            prev_close = float(close.iloc[-1])
            prev_range = float(high.iloc[-1] - low.iloc[-1])

            # Breakout target: prev_close + k * prev_range
            target_price = prev_close + k * prev_range

            # Regime filter
            regime = calc_regime(close, self.active_params.get("regime_fast", 10),
                                 self.active_params.get("regime_mid", 30),
                                 self.active_params.get("regime_slow", 60))

            if regime == 0:  # BEAR: no entry
                continue

            # Position size based on regime
            size_mult = self.regime_bull_mult if regime == 2 else self.regime_neutral_mult
            size_pct = min(self.risk_per_trade * size_mult, 0.60)

            # ATR-based stop
            atr = calc_atr(data, self.atr_period)
            stop_dist = float(atr.iloc[-1]) * self.atr_stop_init if pd.notna(atr.iloc[-1]) else prev_range
            stop_price = target_price - stop_dist

            targets.append(BreakoutTarget(
                ticker=ticker,
                target_price=round(target_price, 2),
                direction="long",
                size_pct=size_pct,
                stop_price=round(stop_price, 2),
                reason=f"VB k={k:.2f}, regime={'BULL' if regime == 2 else 'NEUTRAL'}, size={size_pct:.1%}",
            ))

        return targets
