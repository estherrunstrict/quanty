"""Shared test fixtures."""
from __future__ import annotations

import pandas as pd
import numpy as np
import pytest
from datetime import date, timedelta
from typing import Dict, Optional

from framework.data.provider import BaseDataProvider


class MockDataProvider(BaseDataProvider):
    """In-memory data provider for testing."""

    def __init__(self, data: Optional[Dict[str, pd.DataFrame]] = None):
        self._data = data or {}

    def get_historical_data(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        if ticker not in self._data:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        df = self._data[ticker]
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if df.index.tz is not None:
            start_ts = start_ts.tz_localize(df.index.tz)
            end_ts = end_ts.tz_localize(df.index.tz)
        return df[(df.index >= start_ts) & (df.index <= end_ts)]

    def get_latest_price(self, ticker: str) -> float:
        if ticker not in self._data or self._data[ticker].empty:
            raise ValueError(f"No data for {ticker}")
        return float(self._data[ticker]["Close"].iloc[-1])


def make_ohlcv(
    n_days: int = 300,
    start_price: float = 100.0,
    volatility: float = 0.01,
    trend: float = 0.0003,
    start_date: str = "2023-01-01",
) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    dates = pd.bdate_range(start=start_date, periods=n_days)
    np.random.seed(42)

    # Random walk with drift
    returns = np.random.normal(trend, volatility, n_days)
    prices = start_price * np.exp(np.cumsum(returns))

    # Build OHLCV from close prices
    close = prices
    open_p = np.roll(close, 1)
    open_p[0] = start_price
    high = np.maximum(open_p, close) * (1 + np.abs(np.random.normal(0, 0.005, n_days)))
    low = np.minimum(open_p, close) * (1 - np.abs(np.random.normal(0, 0.005, n_days)))
    volume = np.random.randint(1_000_000, 50_000_000, n_days)

    df = pd.DataFrame({
        "Open": open_p,
        "High": high,
        "Low": low,
        "Close": close,
        "Volume": volume.astype(float),
    }, index=dates)
    return df


@pytest.fixture
def sample_ohlcv():
    """300 days of synthetic OHLCV data starting 2023-01-01."""
    return make_ohlcv(300)


@pytest.fixture
def mock_provider(sample_ohlcv):
    """MockDataProvider with SPY, IEF, SH data."""
    # SPY: uptrending
    spy = make_ohlcv(500, start_price=400, trend=0.0004, start_date="2022-01-01")
    # IEF: slight downtrend (bonds)
    ief = make_ohlcv(500, start_price=100, trend=-0.0001, start_date="2022-01-01")
    # SH: inverse of SPY
    sh = make_ohlcv(500, start_price=15, trend=-0.0003, start_date="2022-01-01")
    return MockDataProvider({"SPY": spy, "IEF": ief, "SH": sh})
