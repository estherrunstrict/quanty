"""YFinance data provider for US equities and ETFs."""
import logging
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from framework.data.provider import BaseDataProvider, validate_ohlcv

logger = logging.getLogger(__name__)


class YFinanceProvider(BaseDataProvider):
    """Fetches historical OHLCV data from Yahoo Finance."""

    def get_historical_data(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        # yfinance end is exclusive, so add 1 day
        end_str = (end + timedelta(days=1)).strftime("%Y-%m-%d")
        start_str = start.strftime("%Y-%m-%d")

        logger.info(f"Fetching {ticker} from yfinance: {start_str} to {end_str}")
        df = yf.download(ticker, start=start_str, end=end_str, auto_adjust=False, progress=False)

        if df.empty:
            logger.warning(f"No data returned from yfinance for {ticker}")
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        # Flatten multi-level columns if needed (yfinance sometimes returns multi-index)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Rename Adj Close if present
        if "Adj Close" in df.columns:
            df = df.rename(columns={"Adj Close": "AdjClose"})

        # Keep only OHLCV + optional AdjClose
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume", "AdjClose"] if c in df.columns]
        df = df[keep]

        # Ensure end date is respected (strict upper bound)
        end_ts = pd.Timestamp(end)
        if df.index.tz is not None:
            end_ts = end_ts.tz_localize(df.index.tz)
        df = df[df.index <= end_ts]

        return validate_ohlcv(df)

    def get_latest_price(self, ticker: str) -> float:
        data = yf.download(ticker, period="2d", auto_adjust=False, progress=False)
        if data.empty:
            raise ValueError(f"No price data for {ticker}")
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return float(data["Close"].iloc[-1])
