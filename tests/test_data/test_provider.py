"""Tests for data providers and OHLCV validation."""
import pytest
import pandas as pd
import numpy as np

from framework.data.provider import validate_ohlcv, OHLCV_COLUMNS
from tests.conftest import MockDataProvider, make_ohlcv
from datetime import date


class TestValidateOHLCV:
    def test_valid_dataframe(self):
        df = make_ohlcv(10)
        result = validate_ohlcv(df)
        assert list(result.columns) == OHLCV_COLUMNS
        assert len(result) == 10

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        result = validate_ohlcv(df)
        assert result.empty

    def test_none_input(self):
        result = validate_ohlcv(None)
        assert result.empty

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"Open": [1], "Close": [2]})
        with pytest.raises(ValueError, match="Missing OHLCV columns"):
            validate_ohlcv(df)

    def test_coerces_string_to_numeric(self):
        dates = pd.bdate_range("2023-01-01", periods=3)
        df = pd.DataFrame({
            "Open": ["100", "101", "102"],
            "High": ["105", "106", "107"],
            "Low": ["95", "96", "97"],
            "Close": ["103", "104", "105"],
            "Volume": ["1000000", "2000000", "3000000"],
        }, index=dates)
        result = validate_ohlcv(df)
        assert np.issubdtype(result["Close"].dtype, np.number)

    def test_sorts_by_date(self):
        dates = pd.bdate_range("2023-01-01", periods=5)
        df = pd.DataFrame({
            "Open": [5, 4, 3, 2, 1],
            "High": [5, 4, 3, 2, 1],
            "Low": [5, 4, 3, 2, 1],
            "Close": [5, 4, 3, 2, 1],
            "Volume": [5, 4, 3, 2, 1],
        }, index=dates[::-1])  # reversed
        result = validate_ohlcv(df)
        assert result.index[0] < result.index[-1]


class TestMockDataProvider:
    def test_returns_data_in_range(self):
        spy = make_ohlcv(100, start_date="2023-01-01")
        provider = MockDataProvider({"SPY": spy})
        result = provider.get_historical_data("SPY", date(2023, 3, 1), date(2023, 4, 1))
        assert len(result) > 0
        assert result.index.min() >= pd.Timestamp("2023-03-01")
        assert result.index.max() <= pd.Timestamp("2023-04-01")

    def test_unknown_ticker_returns_empty(self):
        provider = MockDataProvider({})
        result = provider.get_historical_data("FAKE", date(2023, 1, 1), date(2023, 12, 31))
        assert result.empty

    def test_latest_price(self):
        spy = make_ohlcv(10)
        provider = MockDataProvider({"SPY": spy})
        price = provider.get_latest_price("SPY")
        assert price > 0

    def test_latest_price_unknown_ticker(self):
        provider = MockDataProvider({})
        with pytest.raises(ValueError):
            provider.get_latest_price("FAKE")
