"""Tests for the Kim Hyo-jin bubble-collapse exit overlay."""
from __future__ import annotations

import pytest

from framework.regime.exit_overlay import (
    MarketExitOverlay,
    breadth_divergence,
    composite_exit_overlay,
    cumulative_return,
    gate_long_weight,
    global_breadth_signal,
    global_negative_count,
    rate_overlay_factor,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class TestCumulativeReturn:
    def test_compounds(self):
        assert cumulative_return([0.01, 0.01]) == pytest.approx(0.0201)

    def test_empty(self):
        assert cumulative_return([]) == 0.0

    def test_window_takes_tail(self):
        # only last 2 of the three used
        assert cumulative_return([1.0, 0.01, 0.01], window=2) == pytest.approx(0.0201)


# --------------------------------------------------------------------------- #
# Signal A — breadth divergence
# --------------------------------------------------------------------------- #
class TestBreadthDivergence:
    def test_calm_broad_rally_low_score(self):
        # everything rising together — no forced concentration
        up = [0.004] * 80
        res = breadth_divergence(up, up, up)
        assert not res.concentration
        assert res.score < 0.3

    def test_forced_concentration_high_score(self):
        # leader steadily up, broad and old-economy steadily down
        leader = [0.004] * 80
        broad = [-0.002] * 80
        old = [-0.003] * 80
        res = breadth_divergence(leader, broad, old)
        assert res.concentration
        assert res.spread > 0.10
        assert res.score >= 0.9
        assert res.divergence_days == 20  # every persistence-window day diverges

    def test_divergence_day_counting(self):
        # last 20 days: alternate leader-up/broad-down with flat days
        leader = ([0.01, 0.0] * 10)
        broad = ([-0.01, 0.0] * 10)
        old = [-0.01] * 20
        res = breadth_divergence(leader, broad, old, persistence_window=20)
        assert res.divergence_days == 10

    def test_spread_without_concentration_is_dampened(self):
        # leader far ahead but broad still positive -> not the warning
        leader = [0.005] * 80
        broad = [0.001] * 80
        old = [0.001] * 80
        res = breadth_divergence(leader, broad, old)
        assert not res.concentration
        # dampened by the 0.4 factor
        assert res.score <= 0.4


# --------------------------------------------------------------------------- #
# Signal B — global negative count
# --------------------------------------------------------------------------- #
class TestGlobalNegativeCount:
    def test_counts_negatives(self):
        rets = {"DE": -0.05, "IN": 0.10, "TW": -0.01, "BR": 0.02}
        count, negs = global_negative_count(rets)
        assert count == 2
        assert negs == ["DE", "TW"]

    def test_below_warn_low_score(self):
        rets = {"DE": -0.05, "IN": 0.10, "TW": 0.03, "BR": 0.02}
        res = global_breadth_signal(rets, warn_count=3, danger_count=6)
        assert res.negative_count == 1
        assert res.score == 0.0

    def test_danger_count_saturates(self):
        rets = {c: -0.05 for c in ["a", "b", "c", "d", "e", "f"]}
        res = global_breadth_signal(rets, warn_count=3, danger_count=6)
        assert res.score == pytest.approx(1.0)

    def test_stepping_up_flag(self):
        rets = {"a": -0.05, "b": -0.05, "c": -0.05, "d": -0.05}  # 4 negative
        res = global_breadth_signal(rets, prior_counts=[1, 2, 3], warn_count=3, danger_count=6)
        assert res.stepping_up
        # step-up nudges score above the plain ramp at count=4
        plain = global_breadth_signal(rets, warn_count=3, danger_count=6)
        assert res.score > plain.score

    def test_no_step_when_not_increasing(self):
        rets = {"a": -0.05, "b": -0.05}  # 2 negative
        res = global_breadth_signal(rets, prior_counts=[3, 4], warn_count=3, danger_count=6)
        assert not res.stepping_up


# --------------------------------------------------------------------------- #
# Signal C — rate overlay
# --------------------------------------------------------------------------- #
class TestRateOverlay:
    def test_far_below_full_factor(self):
        res = rate_overlay_factor(3.0, 5.0, approach_band_bp=50, floor=0.2)
        assert res.factor == pytest.approx(1.0)
        assert not res.breached

    def test_breach_floors(self):
        res = rate_overlay_factor(5.1, 5.0, floor=0.2)
        assert res.breached
        assert res.factor == pytest.approx(0.2)

    def test_within_band_ramps(self):
        # 25bp below a 5.0 prior high, 50bp band -> halfway from floor to 1
        res = rate_overlay_factor(4.75, 5.0, approach_band_bp=50, floor=0.2)
        assert res.factor == pytest.approx(0.2 + 0.8 * 0.5)
        assert res.gap_bp == pytest.approx(25.0)

    def test_at_prior_high_is_breach(self):
        res = rate_overlay_factor(5.0, 5.0, floor=0.2)
        assert res.breached
        assert res.factor == pytest.approx(0.2)


# --------------------------------------------------------------------------- #
# Composite + gating
# --------------------------------------------------------------------------- #
class TestComposite:
    def _calm_inputs(self):
        up = [0.003] * 80
        return dict(
            leader_returns=up, broad_returns=up, oldecon_returns=up,
            country_returns={"a": 0.05, "b": 0.04, "c": 0.06},
            current_yield=3.0, prior_cycle_high=5.0,
        )

    def test_calm_full_budget_no_exit(self):
        ov = MarketExitOverlay()
        res = ov.evaluate(**self._calm_inputs())
        assert res.risk_budget == pytest.approx(1.0, abs=0.05)
        assert not res.exit_triggered
        assert res.warnings == []

    def test_all_three_firing_triggers_exit(self):
        ov = MarketExitOverlay()
        res = ov.evaluate(
            leader_returns=[0.004] * 80,
            broad_returns=[-0.002] * 80,
            oldecon_returns=[-0.003] * 80,
            country_returns={c: -0.05 for c in "abcdef"},
            current_yield=5.1,
            prior_cycle_high=5.0,
            prior_negative_counts=[3, 4, 5],
        )
        assert res.exit_triggered
        assert res.risk_budget <= 0.2
        assert len(res.warnings) == 3

    def test_rate_breach_gates_even_calm_breadth(self):
        # breadth/global calm, but rate breached -> budget capped at floor
        breadth = breadth_divergence([0.003] * 80, [0.003] * 80, [0.003] * 80)
        gb = global_breadth_signal({"a": 0.05, "b": 0.05})
        rate = rate_overlay_factor(5.1, 5.0, floor=0.2)
        res = composite_exit_overlay(breadth, gb, rate)
        assert res.risk_budget <= 0.2
        assert res.exit_triggered

    def test_gate_scales_long_weight(self):
        ov = MarketExitOverlay()
        res = ov.evaluate(**self._calm_inputs())
        # calm -> near full weight preserved
        assert gate_long_weight(1.0, res) == pytest.approx(res.risk_budget)
        # flat/short weight untouched
        assert gate_long_weight(0.0, res) == 0.0
        assert gate_long_weight(-0.5, res) == -0.5

    def test_result_gate_method_matches_function(self):
        ov = MarketExitOverlay()
        res = ov.evaluate(**self._calm_inputs())
        assert res.gate(0.8) == pytest.approx(gate_long_weight(0.8, res))
