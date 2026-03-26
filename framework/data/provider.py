"""BaseDataProvider ABC — data fetching separated from execution.

OHLCV Data Contract:
  Index: DatetimeIndex (timezone-aware or naive, daily frequency)
  Columns: Open (float), High (float), Low (float), Close (float), Volume (float)
  Optional: AdjClose (float)

DataProviders normalize ALL sources to this schema before strategies see data.
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

import pandas as pd


OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class BaseDataProvider(ABC):
    """Abstract base for all data providers."""

    @abstractmethod
    def get_historical_data(
        self, ticker: str, start: date, end: date
    ) -> pd.DataFrame:
        """Return OHLCV data. `end` is INCLUSIVE upper bound.

        In backtest mode, BacktestEngine enforces end <= simulated_date.
        Provider MUST NOT return data after `end`.

        Returns DataFrame with DatetimeIndex and OHLCV columns.
        """

    @abstractmethod
    def get_latest_price(self, ticker: str) -> float:
        """Return the most recent closing price for a ticker."""


def validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize an OHLCV DataFrame."""
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    # Ensure required columns exist
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")

    # Ensure numeric types
    for col in OHLCV_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Sort by date ascending
    df = df.sort_index()

    return df
