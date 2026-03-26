#!/usr/bin/env python3
"""Run a strategy in live or dry-run mode.

Usage:
    python scripts/run_strategy.py --strategy quant40 --mode live
    python scripts/run_strategy.py --strategy quant40 --mode dry-run
    python scripts/run_strategy.py --strategy all --mode live
"""
import argparse
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.config.loader import load_config, load_secrets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("run_strategy")


def main():
    parser = argparse.ArgumentParser(description="Run trading strategy")
    parser.add_argument("--strategy", required=True, help="Strategy name or 'all'")
    parser.add_argument("--mode", choices=["live", "dry-run", "shadow"], default="dry-run")
    args = parser.parse_args()

    config = load_config()
    secrets = load_secrets()

    logger.info(f"Strategy: {args.strategy}, Mode: {args.mode}")

    if args.mode == "live":
        logger.warning("LIVE MODE — real orders will be placed!")
    elif args.mode == "dry-run":
        logger.info("DRY-RUN MODE — no real orders")
    elif args.mode == "shadow":
        logger.info("SHADOW MODE — comparing signals with existing system")

    # TODO: Implement strategy dispatch based on --strategy
    # This will be completed after remaining strategies are ported
    logger.info("Strategy runner scaffold ready. Implementation pending Phase 2.")


if __name__ == "__main__":
    main()
