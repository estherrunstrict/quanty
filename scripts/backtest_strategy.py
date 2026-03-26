#!/usr/bin/env python3
"""Run a backtest for a specific strategy.

Usage:
    python scripts/backtest_strategy.py --strategy quant40 --start 2023-01-01 --end 2025-12-31
    python scripts/backtest_strategy.py --strategy quant40 --start 2023-01-01 --end 2025-12-31 --cash 25000
"""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.backtest.engine import BacktestEngine
from framework.data.yfinance_provider import YFinanceProvider
from framework.data.cache import CachedProvider
from framework.config.loader import load_config
from framework.regime.detector import RegimeDetector


STRATEGIES = {
    "quant40": lambda config: _make_quant40(config),
}


def _make_quant40(config):
    from strategies.quant40 import Quant40Strategy
    return Quant40Strategy(config.get("quant_replacing_the_40", {}), state_dir="/tmp")


def main():
    parser = argparse.ArgumentParser(description="Run strategy backtest")
    parser.add_argument("--strategy", required=True, choices=STRATEGIES.keys())
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=25000, help="Initial cash")
    parser.add_argument("--with-regime", action="store_true", help="Enable regime detection")
    parser.add_argument("--cache-dir", default="data_cache")
    args = parser.parse_args()

    config = load_config()
    strategy = STRATEGIES[args.strategy](config)

    provider = CachedProvider(YFinanceProvider(), cache_dir=args.cache_dir)
    engine = BacktestEngine(strategy, provider, initial_cash=args.cash)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    regime_detector = None
    if args.with_regime:
        regime_detector = RegimeDetector(market_name="US")

    print(f"Running backtest: {args.strategy}")
    print(f"Period: {start} to {end}")
    print(f"Initial cash: ${args.cash:,.2f}")
    print(f"Regime detection: {'ON' if args.with_regime else 'OFF'}")
    print()

    metrics = engine.run(start, end, regime_detector=regime_detector)

    print("=" * 60)
    print(f"RESULTS: {metrics.summary()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
