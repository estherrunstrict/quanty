"""CachedProvider — wraps any provider with local parquet storage."""
import logging
import os
from datetime import date, timedelta

import pandas as pd

from framework.data.provider import BaseDataProvider, validate_ohlcv

logger = logging.getLogger(__name__)


class CachedProvider(BaseDataProvider):
    """Wraps a data provider with local parquet file caching.

    Fallback chain: parquet cache -> wrapped provider.
    Cache is append-only: new data is appended after market close.
    """

    def __init__(self, wrapped: BaseDataProvider, cache_dir: str = "data_cache"):
        self._wrapped = wrapped
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, ticker: str) -> str:
        safe_name = ticker.replace("/", "_").replace("=", "_")
        return os.path.join(self._cache_dir, f"{safe_name}.parquet")

    def get_historical_data(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        cache_path = self._cache_path(ticker)

        # Try cache first
        cached = self._read_cache(cache_path, start, end)
        if cached is not None and len(cached) > 0:
            # Check if cache covers the requested range
            cache_end = cached.index.max().date() if hasattr(cached.index.max(), 'date') else cached.index.max()
            if cache_end >= end - timedelta(days=3):  # Allow 3-day gap for weekends/holidays
                logger.info(f"Cache hit for {ticker}: {len(cached)} rows")
                return cached

        # Cache miss — fetch from wrapped provider
        logger.info(f"Cache miss for {ticker}, fetching from provider")
        fresh = self._wrapped.get_historical_data(ticker, start, end)

        if not fresh.empty:
            self._write_cache(cache_path, fresh)

        return fresh

    def get_latest_price(self, ticker: str) -> float:
        return self._wrapped.get_latest_price(ticker)

    def _read_cache(self, path: str, start: date, end: date):
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path)
            if df.empty:
                return None
            # Filter to requested range
            start_ts = pd.Timestamp(start)
            end_ts = pd.Timestamp(end)
            if df.index.tz is not None:
                start_ts = start_ts.tz_localize(df.index.tz)
                end_ts = end_ts.tz_localize(df.index.tz)
            mask = (df.index >= start_ts) & (df.index <= end_ts)
            return validate_ohlcv(df[mask])
        except Exception as e:
            logger.warning(f"Corrupt cache file {path}: {e}")
            return None

    def _write_cache(self, path: str, df: pd.DataFrame):
        try:
            if os.path.exists(path):
                existing = pd.read_parquet(path)
                df = pd.concat([existing, df])
                df = df[~df.index.duplicated(keep="last")]
                df = df.sort_index()
            df.to_parquet(path)
            logger.info(f"Cache updated: {path} ({len(df)} rows)")
        except Exception as e:
            logger.warning(f"Failed to write cache {path}: {e}")
