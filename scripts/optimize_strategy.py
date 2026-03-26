#!/usr/bin/env python3
"""Optimize strategy parameters using walk-forward validation.

Usage:
    python scripts/optimize_strategy.py --strategy quant40
    python scripts/optimize_strategy.py --strategy quant40 --trials 100 --timeout 600

SAFETY: Results are written to optimization_results/, NOT config.yaml.
You must review and manually apply optimized parameters.
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
from framework.optimizer.param_optimizer import walk_forward_optimize

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Define param search spaces per strategy
PARAM_SPACES = {
    "quant40": {
        "SPY_ALLOCATION": (0.40, 0.80),
        "TREND_ALLOCATION": (0.20, 0.60),
        "SHRINK_FACTOR": (0.50, 1.00),
    },
}

STRATEGY_FACTORIES = {
    "quant40": lambda cfg: __import__("strategies.quant40", fromlist=["Quant40Strategy"]).Quant40Strategy(
        cfg, state_dir="/tmp"
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, choices=list(PARAM_SPACES.keys()))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--cash", type=float, default=25000)
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds")
    args = parser.parse_args()

    config = load_config()
    base_config = config.get("quant_replacing_the_40", {})
    provider = CachedProvider(YFinanceProvider(), cache_dir="data_cache")

    factory = STRATEGY_FACTORIES[args.strategy]
    space = PARAM_SPACES[args.strategy]

    print("=" * 70)
    print(f"WALK-FORWARD OPTIMIZATION: {args.strategy}")
    print(f"Period: {args.start} to {args.end}")
    print(f"Max trials: {args.trials}, Timeout: {args.timeout}s")
    print(f"Search space: {space}")
    print("=" * 70)
    print()
    print("⚠️  Results will be saved to optimization_results/ — NOT config.yaml")
    print("    Review and apply manually after inspection.")
    print()

    result = walk_forward_optimize(
        strategy_factory=factory,
        base_config=base_config,
        param_space=space,
        data_provider=provider,
        full_start=date.fromisoformat(args.start),
        full_end=date.fromisoformat(args.end),
        initial_cash=args.cash,
        max_trials=args.trials,
        timeout_sec=args.timeout,
    )

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(result.summary())
    print(f"\nBest params: {result.best_params}")
    print(f"Walk-forward windows: {len(result.windows)}")
    for w in result.windows:
        print(f"  [{w['train_start']}..{w['train_end']}] → [{w['test_start']}..{w['test_end']}]: "
              f"train={w['train_sharpe']:.2f}, test={w['test_sharpe']:.2f}")

    path = result.save()
    print(f"\nSaved to: {path}")


if __name__ == "__main__":
    main()
