"""Tests for regime hypothesis testing."""
from __future__ import annotations

import pytest
from datetime import date

from framework.regime.hypothesis_test import run_hypothesis_test, HypothesisResult
from strategies.quant40 import Quant40Strategy
from tests.conftest import MockDataProvider, make_ohlcv


class TestHypothesisTest:
    @pytest.fixture
    def provider(self):
        spy = make_ohlcv(600, start_price=400, trend=0.0004, start_date="2022-01-01")
        ief = make_ohlcv(600, start_price=100, trend=-0.0001, start_date="2022-01-01")
        sh = make_ohlcv(600, start_price=15, trend=-0.0003, start_date="2022-01-01")
        return MockDataProvider({"SPY": spy, "IEF": ief, "SH": sh})

    @pytest.fixture
    def strategy(self):
        config = {
            "SPY_ALLOCATION": 0.60,
            "TREND_ALLOCATION": 0.40,
            "EQUITY_ASSET": "SPY",
            "SAFE_ASSET": "IEF",
            "INVERSE_EQUITY_ASSET": "SH",
            "SIGNAL_ASSET": "SPY",
        }
        return Quant40Strategy(config, state_dir="/tmp")

    def test_returns_hypothesis_result(self, strategy, provider):
        result = run_hypothesis_test(
            strategy, provider,
            start_date=date(2023, 1, 1),
            end_date=date(2024, 6, 1),
            initial_cash=10000,
        )
        assert isinstance(result, HypothesisResult)
        assert result.strategy_name == "quant40"

    def test_result_has_metrics(self, strategy, provider):
        result = run_hypothesis_test(
            strategy, provider,
            start_date=date(2023, 1, 1),
            end_date=date(2024, 6, 1),
        )
        assert result.baseline_metrics is not None
        assert result.regime_metrics is not None
        assert isinstance(result.sharpe_improvement, float)
        assert isinstance(result.mdd_change, float)

    def test_result_has_recommendation(self, strategy, provider):
        result = run_hypothesis_test(
            strategy, provider,
            start_date=date(2023, 1, 1),
            end_date=date(2024, 6, 1),
        )
        assert isinstance(result.regime_helps, bool)
        assert len(result.reason) > 0

    def test_summary_format(self, strategy, provider):
        result = run_hypothesis_test(
            strategy, provider,
            start_date=date(2023, 1, 1),
            end_date=date(2024, 6, 1),
        )
        summary = result.summary()
        assert "quant40" in summary
        assert "APPLY" in summary or "SKIP" in summary
