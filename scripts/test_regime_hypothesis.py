#!/usr/bin/env python3
"""Run regime hypothesis tests for all strategies.

Usage:
    python scripts/test_regime_hypothesis.py
    python scripts/test_regime_hypothesis.py --strategy quant40
    python scripts/test_regime_hypothesis.py --start 2020-01-01 --end 2025-12-31
"""
import argparse
import os
import sys
import logging
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.config.loader import load_config
from framework.data.yfinance_provider import YFinanceProvider
from framework.data.cache import CachedProvider
from framework.regime.hypothesis_test import run_hypothesis_test

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

STRATEGY_FACTORIES = {
    "quant40": lambda cfg: __import__("strategies.quant40", fromlist=["Quant40Strategy"]).Quant40Strategy(
        cfg.get("quant_replacing_the_40", {}), state_dir="/tmp"
    ),
    "jd_strategy": lambda cfg: __import__("strategies.jd_strategy", fromlist=["JDStrategy"]).JDStrategy(
        cfg.get("jd_strategy", {}), state_dir="/tmp"
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=list(STRATEGY_FACTORIES.keys()), default=None)
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--cash", type=float, default=25000)
    args = parser.parse_args()

    config = load_config()
    provider = CachedProvider(YFinanceProvider(), cache_dir="data_cache")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    strategies = [args.strategy] if args.strategy else list(STRATEGY_FACTORIES.keys())

    print("=" * 70)
    print("REGIME HYPOTHESIS TEST")
    print(f"Period: {start} to {end} | Cash: ${args.cash:,.0f}")
    print("=" * 70)

    results = []
    for name in strategies:
        strategy = STRATEGY_FACTORIES[name](config)
        result = run_hypothesis_test(strategy, provider, start, end, args.cash)
        results.append(result)
        print(result.summary())

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for r in results:
        verdict = "APPLY regime detection" if r.regime_helps else "SKIP (keep fixed params)"
        print(f"  {r.strategy_name}: {verdict}")


if __name__ == "__main__":
    main()
