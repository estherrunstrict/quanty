"""Tests for parameter optimizer and walk-forward validation."""
from __future__ import annotations

import json
import os
import tempfile
import pytest
from datetime import date

from tests.conftest import MockDataProvider, make_ohlcv

# Skip all tests if optuna not installed
optuna = pytest.importorskip("optuna")

from framework.optimizer.param_optimizer import (
    optimize_strategy, walk_forward_optimize, OptimizationResult,
)
from strategies.quant40 import Quant40Strategy


BASE_CONFIG = {
    "SPY_ALLOCATION": 0.60,
    "TREND_ALLOCATION": 0.40,
    "EQUITY_ASSET": "SPY",
    "SAFE_ASSET": "IEF",
    "INVERSE_EQUITY_ASSET": "SH",
    "SIGNAL_ASSET": "SPY",
}


def make_strategy(config):
    return Quant40Strategy(config, state_dir="/tmp")


@pytest.fixture
def provider():
    spy = make_ohlcv(800, start_price=400, trend=0.0003, start_date="2021-01-01")
    ief = make_ohlcv(800, start_price=100, trend=-0.0001, start_date="2021-01-01")
    sh = make_ohlcv(800, start_price=15, trend=-0.0002, start_date="2021-01-01")
    return MockDataProvider({"SPY": spy, "IEF": ief, "SH": sh})


class TestOptimizeStrategy:
    def test_basic_optimization(self, provider):
        """Optimizer should run and return results."""
        result = optimize_strategy(
            strategy_factory=make_strategy,
            base_config=BASE_CONFIG,
            param_space={"SPY_ALLOCATION": (0.40, 0.80)},
            data_provider=provider,
            train_start=date(2022, 1, 1),
            train_end=date(2023, 6, 1),
            max_trials=5,  # Small for test speed
            timeout_sec=60,
        )
        assert isinstance(result, OptimizationResult)
        assert result.n_trials == 5
        assert "SPY_ALLOCATION" in result.best_params
        assert 0.40 <= result.best_params["SPY_ALLOCATION"] <= 0.80

    def test_respects_max_trials(self, provider):
        result = optimize_strategy(
            strategy_factory=make_strategy,
            base_config=BASE_CONFIG,
            param_space={"SPY_ALLOCATION": (0.40, 0.80)},
            data_provider=provider,
            train_start=date(2022, 1, 1),
            train_end=date(2023, 1, 1),
            max_trials=3,
            timeout_sec=300,
        )
        assert result.n_trials == 3


class TestOptimizationResultSafety:
    def test_saves_to_file_not_config(self, provider):
        """Safety rail: results go to optimization_results/, not config.yaml."""
        result = optimize_strategy(
            strategy_factory=make_strategy,
            base_config=BASE_CONFIG,
            param_space={"SPY_ALLOCATION": (0.40, 0.80)},
            data_provider=provider,
            train_start=date(2022, 1, 1),
            train_end=date(2023, 1, 1),
            max_trials=3,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = result.save(output_dir=tmpdir)
            assert os.path.exists(path)
            assert "config.yaml" not in path

            with open(path) as f:
                data = json.load(f)
            assert "best_params" in data
            assert data["strategy_name"] == "quant40"

    def test_summary_format(self, provider):
        result = optimize_strategy(
            strategy_factory=make_strategy,
            base_config=BASE_CONFIG,
            param_space={"SPY_ALLOCATION": (0.40, 0.80)},
            data_provider=provider,
            train_start=date(2022, 1, 1),
            train_end=date(2023, 1, 1),
            max_trials=3,
        )
        summary = result.summary()
        assert "quant40" in summary
        assert "Sharpe" in summary
