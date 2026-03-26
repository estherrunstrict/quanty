"""Regime hypothesis test — does regime detection actually help each strategy?

For each strategy, runs two backtests:
  1. Fixed params (baseline)
  2. Regime-adaptive params

Compares risk-adjusted returns. Only recommends regime detection where it
demonstrably improves Sharpe ratio without increasing MDD by more than 3%.

Validation rule (from eng review):
  regime-adaptive must not increase MDD by more than 3% vs fixed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from framework.backtest.engine import BacktestEngine
from framework.backtest.metrics import BacktestMetrics
from framework.data.provider import BaseDataProvider
from framework.regime.detector import RegimeDetector
from framework.strategy import BaseStrategy

logger = logging.getLogger(__name__)

MAX_MDD_INCREASE = 0.03  # 3 percentage points


@dataclass
class HypothesisResult:
    strategy_name: str
    baseline_metrics: BacktestMetrics
    regime_metrics: BacktestMetrics
    sharpe_improvement: float
    mdd_change: float  # positive = worse (more drawdown)
    regime_helps: bool
    reason: str

    def summary(self) -> str:
        verdict = "APPLY" if self.regime_helps else "SKIP"
        return (
            f"[{verdict}] {self.strategy_name}: "
            f"Sharpe {self.baseline_metrics.sharpe_ratio:.2f} → {self.regime_metrics.sharpe_ratio:.2f} "
            f"(Δ{self.sharpe_improvement:+.2f}), "
            f"MDD {self.baseline_metrics.max_drawdown:.1%} → {self.regime_metrics.max_drawdown:.1%} "
            f"(Δ{self.mdd_change:+.1%}) | {self.reason}"
        )


def run_hypothesis_test(
    strategy: BaseStrategy,
    data_provider: BaseDataProvider,
    start_date: date,
    end_date: date,
    initial_cash: float = 10000.0,
    market_name: str = "US",
) -> HypothesisResult:
    """Run A/B backtest: fixed params vs regime-adaptive params.

    Returns HypothesisResult with recommendation.
    """
    # A: Baseline (no regime detection)
    logger.info(f"[{strategy.name}] Running baseline backtest (no regime)...")
    engine_a = BacktestEngine(strategy, data_provider, initial_cash)
    # Reset strategy state for clean baseline
    strategy.state = strategy.get_default_state()
    strategy.active_params = dict(strategy.config)
    strategy.current_regime = None
    baseline = engine_a.run(start_date, end_date, regime_detector=None)

    # B: With regime detection
    logger.info(f"[{strategy.name}] Running regime-adaptive backtest...")
    detector = RegimeDetector(market_name=market_name)
    engine_b = BacktestEngine(strategy, data_provider, initial_cash)
    strategy.state = strategy.get_default_state()
    strategy.active_params = dict(strategy.config)
    strategy.current_regime = None
    regime = engine_b.run(start_date, end_date, regime_detector=detector)

    # Compare
    sharpe_delta = regime.sharpe_ratio - baseline.sharpe_ratio
    # MDD is negative; more negative = worse. mdd_change > 0 means regime is worse.
    mdd_change = baseline.max_drawdown - regime.max_drawdown  # both negative
    # If regime MDD is -25% and baseline is -20%, mdd_change = -0.20 - (-0.25) = 0.05 → worse by 5%

    regime_helps = False
    reason = ""

    if sharpe_delta > 0.05 and mdd_change <= MAX_MDD_INCREASE:
        regime_helps = True
        reason = f"Sharpe improved by {sharpe_delta:.2f}, MDD within tolerance"
    elif sharpe_delta > 0.05 and mdd_change > MAX_MDD_INCREASE:
        reason = f"Sharpe improved but MDD increased by {mdd_change:.1%} (>{MAX_MDD_INCREASE:.0%} limit)"
    elif sharpe_delta <= 0.05:
        reason = f"Sharpe improvement too small ({sharpe_delta:+.2f})"
    else:
        reason = "No clear benefit"

    result = HypothesisResult(
        strategy_name=strategy.name,
        baseline_metrics=baseline,
        regime_metrics=regime,
        sharpe_improvement=sharpe_delta,
        mdd_change=mdd_change,
        regime_helps=regime_helps,
        reason=reason,
    )

    logger.info(result.summary())
    return result
