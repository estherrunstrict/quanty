"""Optuna-based parameter optimizer with walk-forward validation.

Safety rails (from eng review):
  - Max 200 trials per strategy, 30-min timeout
  - Manual trigger only (never auto-writes to live config)
  - Walk-forward: 2-year train, 6-month test, rolling window
  - Results written to a separate file, not config.yaml
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from framework.backtest.engine import BacktestEngine
from framework.backtest.metrics import BacktestMetrics
from framework.data.provider import BaseDataProvider
from framework.strategy import BaseStrategy

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRIALS = 200
DEFAULT_TIMEOUT_SEC = 1800  # 30 minutes
TRAIN_YEARS = 2
TEST_MONTHS = 6


@dataclass
class OptimizationResult:
    strategy_name: str
    best_params: Dict[str, Any]
    best_sharpe: float
    walk_forward_sharpe: float
    walk_forward_cagr: float
    walk_forward_mdd: float
    n_trials: int
    duration_sec: float
    windows: List[Dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.strategy_name}: best Sharpe={self.best_sharpe:.2f}, "
            f"WF Sharpe={self.walk_forward_sharpe:.2f}, "
            f"WF CAGR={self.walk_forward_cagr:.2%}, WF MDD={self.walk_forward_mdd:.2%}, "
            f"trials={self.n_trials}, time={self.duration_sec:.0f}s"
        )

    def save(self, output_dir: str = "optimization_results"):
        """Save results to JSON (NOT config.yaml — safety rail)."""
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{self.strategy_name}_optimized.json")
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, default=str)
        logger.info(f"Optimization results saved to {path}")
        logger.info("NOTE: These are NOT live config. Review and apply manually.")
        return path


def optimize_strategy(
    strategy_factory: Callable[[Dict[str, Any]], BaseStrategy],
    base_config: Dict[str, Any],
    param_space: Dict[str, tuple],
    data_provider: BaseDataProvider,
    train_start: date,
    train_end: date,
    initial_cash: float = 10000.0,
    max_trials: int = DEFAULT_MAX_TRIALS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> OptimizationResult:
    """Run Optuna optimization on a single train window.

    Args:
        strategy_factory: Callable that takes config dict and returns a BaseStrategy.
        base_config: Base strategy parameters.
        param_space: {param_name: (low, high)} ranges to search.
            Supported types: float tuple → suggest_float, int tuple → suggest_int.
        data_provider: Data source for backtesting.
        train_start/end: Training window dates.
        initial_cash: Starting capital for backtests.
        max_trials: Maximum number of Optuna trials.
        timeout_sec: Maximum optimization time in seconds.

    Returns:
        OptimizationResult with best parameters.
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        raise ImportError("Optuna not installed. Run: pip install optuna")

    start_time = time.time()

    def objective(trial):
        config = dict(base_config)
        for name, bounds in param_space.items():
            if isinstance(bounds[0], float):
                config[name] = trial.suggest_float(name, bounds[0], bounds[1])
            else:
                config[name] = trial.suggest_int(name, bounds[0], bounds[1])

        strategy = strategy_factory(config)
        engine = BacktestEngine(strategy, data_provider, initial_cash)
        metrics = engine.run(train_start, train_end)

        # Optimize for Sharpe ratio
        return metrics.sharpe_ratio

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=max_trials, timeout=timeout_sec)

    elapsed = time.time() - start_time
    best = study.best_params

    # Merge best params with base config
    best_config = dict(base_config)
    best_config.update(best)

    return OptimizationResult(
        strategy_name=strategy_factory(base_config).name,
        best_params=best,
        best_sharpe=study.best_value,
        walk_forward_sharpe=0.0,  # Filled by walk-forward validation
        walk_forward_cagr=0.0,
        walk_forward_mdd=0.0,
        n_trials=len(study.trials),
        duration_sec=elapsed,
    )


def walk_forward_optimize(
    strategy_factory: Callable[[Dict[str, Any]], BaseStrategy],
    base_config: Dict[str, Any],
    param_space: Dict[str, tuple],
    data_provider: BaseDataProvider,
    full_start: date,
    full_end: date,
    initial_cash: float = 10000.0,
    train_years: int = TRAIN_YEARS,
    test_months: int = TEST_MONTHS,
    max_trials: int = DEFAULT_MAX_TRIALS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> OptimizationResult:
    """Walk-forward optimization: train on N years, test on M months, roll forward.

    Prevents overfitting by validating optimized params on out-of-sample data.

    Windows:
      [train_start..train_end] → optimize → [test_start..test_end] → evaluate
      Roll forward by test_months, repeat.
    """
    windows = []
    train_start = full_start
    all_test_sharpes = []
    all_test_cagrs = []
    all_test_mdds = []
    best_params_across = {}

    total_start = time.time()

    while True:
        train_end = date(
            train_start.year + train_years,
            train_start.month,
            min(train_start.day, 28),
        )
        test_start = train_end + timedelta(days=1)
        test_end = date(
            test_start.year + (test_start.month + test_months - 1) // 12,
            (test_start.month + test_months - 1) % 12 + 1,
            min(test_start.day, 28),
        )

        if test_end > full_end:
            break

        logger.info(f"Window: train [{train_start} to {train_end}], test [{test_start} to {test_end}]")

        # Optimize on training window
        opt_result = optimize_strategy(
            strategy_factory, base_config, param_space,
            data_provider, train_start, train_end,
            initial_cash, max_trials, timeout_sec,
        )

        # Test optimized params on out-of-sample window
        test_config = dict(base_config)
        test_config.update(opt_result.best_params)
        test_strategy = strategy_factory(test_config)
        test_engine = BacktestEngine(test_strategy, data_provider, initial_cash)
        test_metrics = test_engine.run(test_start, test_end)

        window_result = {
            "train_start": str(train_start),
            "train_end": str(train_end),
            "test_start": str(test_start),
            "test_end": str(test_end),
            "best_params": opt_result.best_params,
            "train_sharpe": opt_result.best_sharpe,
            "test_sharpe": test_metrics.sharpe_ratio,
            "test_cagr": test_metrics.cagr,
            "test_mdd": test_metrics.max_drawdown,
        }
        windows.append(window_result)
        all_test_sharpes.append(test_metrics.sharpe_ratio)
        all_test_cagrs.append(test_metrics.cagr)
        all_test_mdds.append(test_metrics.max_drawdown)
        best_params_across = opt_result.best_params

        logger.info(
            f"  Train Sharpe: {opt_result.best_sharpe:.2f}, "
            f"Test Sharpe: {test_metrics.sharpe_ratio:.2f}, "
            f"Test CAGR: {test_metrics.cagr:.2%}"
        )

        # Roll forward by test_months
        train_start = date(
            train_start.year + (train_start.month + test_months - 1) // 12,
            (train_start.month + test_months - 1) % 12 + 1,
            min(train_start.day, 28),
        )

    total_time = time.time() - total_start

    import numpy as np
    avg_sharpe = float(np.mean(all_test_sharpes)) if all_test_sharpes else 0.0
    avg_cagr = float(np.mean(all_test_cagrs)) if all_test_cagrs else 0.0
    avg_mdd = float(np.mean(all_test_mdds)) if all_test_mdds else 0.0

    return OptimizationResult(
        strategy_name=strategy_factory(base_config).name,
        best_params=best_params_across,
        best_sharpe=max(all_test_sharpes) if all_test_sharpes else 0.0,
        walk_forward_sharpe=avg_sharpe,
        walk_forward_cagr=avg_cagr,
        walk_forward_mdd=avg_mdd,
        n_trials=max_trials * len(windows),
        duration_sec=total_time,
        windows=windows,
    )
