"""Tests for Korea ETF Momentum strategy."""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from strategies.korea_etf_momentum import KoreaETFMomentumStrategy, calc_momentum_scores
from framework.types import SignalDirection, MarketRegime, Portfolio
from tests.conftest import make_ohlcv


@pytest.fixture
def config():
    return {
        "etfs": {"144600": "KODEX Silver", "139220": "TIGER Raw Materials"},
        "invest_ratio": 0.95,
    }


@pytest.fixture
def strategy(config):
    s = KoreaETFMomentumStrategy(config, state_dir="/tmp")
    s.state = s.get_default_state()
    return s


class TestMomentumScoring:
    def test_uptrending_asset_qualifies(self):
        """Asset with positive 12m return should qualify."""
        dates = pd.date_range("2022-01-01", periods=14, freq="ME")
        prices = pd.Series(range(100, 114), index=dates, dtype=float)
        scores = calc_momentum_scores({"TEST": prices})
        assert scores["TEST"]["qualified"] is True
        assert scores["TEST"]["score"] > 0

    def test_downtrending_asset_disqualified(self):
        """Asset with negative 12m return fails absolute momentum filter."""
        dates = pd.date_range("2022-01-01", periods=14, freq="ME")
        prices = pd.Series(range(114, 100, -1), index=dates, dtype=float)
        scores = calc_momentum_scores({"TEST": prices})
        assert scores["TEST"]["qualified"] is False

    def test_insufficient_data(self):
        """Less than 13 months → not qualified."""
        dates = pd.date_range("2022-01-01", periods=5, freq="ME")
        prices = pd.Series(range(100, 105), index=dates, dtype=float)
        scores = calc_momentum_scores({"TEST": prices})
        assert scores["TEST"]["qualified"] is False
        assert scores["TEST"]["score"] == 0

    def test_winner_takes_all(self):
        """Highest scoring qualified asset gets selected."""
        dates = pd.date_range("2022-01-01", periods=14, freq="ME")
        # Asset A: strong uptrend
        a = pd.Series([100 + i * 3 for i in range(14)], index=dates, dtype=float)
        # Asset B: weak uptrend
        b = pd.Series([100 + i * 1 for i in range(14)], index=dates, dtype=float)

        scores = calc_momentum_scores({"A": a, "B": b})
        assert scores["A"]["score"] > scores["B"]["score"]
        assert scores["A"]["qualified"] and scores["B"]["qualified"]

    def test_all_negative_returns_cash(self):
        """Both assets negative 12m → go to CASH."""
        dates = pd.date_range("2022-01-01", periods=14, freq="ME")
        a = pd.Series([100 - i * 2 for i in range(14)], index=dates, dtype=float)
        b = pd.Series([100 - i * 3 for i in range(14)], index=dates, dtype=float)

        scores = calc_momentum_scores({"A": a, "B": b})
        qualified = {t: d for t, d in scores.items() if d["qualified"]}
        assert len(qualified) == 0  # both fail → CASH


class TestKoreaETFMomentumStrategy:
    def test_asset_list(self, strategy):
        assert set(strategy.get_asset_list()) == {"144600", "139220"}

    def test_insufficient_data_holds(self, strategy):
        data = make_ohlcv(50)
        signals = strategy.generate_signals(data)
        ticker = list(signals.keys())[0]
        assert signals[ticker].direction == SignalDirection.HOLD

    def test_rebalance_logic(self, strategy):
        """Strategy should rebalance on new month."""
        data = make_ohlcv(300, start_price=100, trend=0.001)
        strategy.state["last_rebalance_month"] = ""  # Force rebalance

        signals = strategy.generate_signals(data)
        # Should have made a decision
        assert len(signals) > 0

    def test_no_rebalance_same_month(self, strategy):
        """Strategy should hold during same month."""
        data = make_ohlcv(300, start_price=100, trend=0.001)
        last_date = data.index[-1]
        strategy.state["last_rebalance_month"] = last_date.strftime("%Y-%m")
        strategy.state["target_ticker"] = "144600"

        signals = strategy.generate_signals(data)
        assert signals["144600"].direction == SignalDirection.BUY
        assert "until next rebalance" in signals["144600"].reason

    def test_allocation_winner_takes_all(self, strategy):
        from framework.types import Signal
        signals = {"144600": Signal("144600", SignalDirection.BUY, strength=1.0)}
        portfolio = Portfolio(total_value=10000000, cash_balance=10000000)
        alloc = strategy.get_target_allocation(signals, MarketRegime.BULL, portfolio)
        assert alloc["144600"] == pytest.approx(0.95)
        assert alloc["139220"] == 0.0
