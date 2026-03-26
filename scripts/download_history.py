#!/usr/bin/env python3
"""Download historical data to local parquet cache.

Usage:
    python scripts/download_history.py                    # Download all default tickers
    python scripts/download_history.py --tickers SPY IEF  # Download specific tickers
    python scripts/download_history.py --years 5          # Download 5 years of data
"""
import argparse
import os
import sys
from datetime import date, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.data.yfinance_provider import YFinanceProvider
from framework.data.cache import CachedProvider

DEFAULT_TICKERS = ["SPY", "IEF", "SH", "^IXIC"]  # SPY, bonds, inverse, NASDAQ Composite
DEFAULT_YEARS = 4


def main():
    parser = argparse.ArgumentParser(description="Download historical data to cache")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS)
    parser.add_argument("--cache-dir", default="data_cache")
    args = parser.parse_args()

    provider = YFinanceProvider()
    cached = CachedProvider(provider, cache_dir=args.cache_dir)

    end = date.today() - timedelta(days=1)
    start = date(end.year - args.years, end.month, end.day)

    print(f"Downloading {len(args.tickers)} tickers from {start} to {end}")
    print(f"Cache directory: {args.cache_dir}")

    for ticker in args.tickers:
        print(f"\n--- {ticker} ---")
        try:
            df = cached.get_historical_data(ticker, start, end)
            print(f"  Rows: {len(df)}")
            if not df.empty:
                print(f"  Date range: {df.index.min().date()} to {df.index.max().date()}")
                print(f"  Latest close: {df['Close'].iloc[-1]:.2f}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
