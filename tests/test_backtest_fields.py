"""annotate_backtest — registry-fed backtest blocks + kill-switch state.

Pins the omit rule: a bot with no filed backtest gets NO block (never a
standing em dash), hybrid_vb gets one block per leg plus the WORSE leg's kill
state, and every strategy gets a kill_state so the badge logic never reads
undefined.

Run: python3 -m pytest tests/test_backtest_fields.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

REGISTRY = {
    "bots": {
        "quant40": {"cagr_pct": 9.7, "mdd_pct": -20.4, "sharpe": 0.87,
                    "window": "2021-2026", "asof": "2026-08-22",
                    "kill_threshold_pct": -20.4},
        "jd_strategy": {"cagr_pct": None, "mdd_pct": None},
        "hybrid_vb_kr": {"cagr_pct": 37.8, "mdd_pct": -19.7, "sharpe": 1.84,
                         "kill_threshold_pct": -19.7},
        "hybrid_vb_us": {"cagr_pct": 12.2, "mdd_pct": -19.9, "sharpe": 0.84,
                         "kill_threshold_pct": -19.9},
    }
}
HEALTH = {
    "bots": {
        "quant40": {"kill_state": "warn", "dd_ratio": 0.97},
        "hybrid_vb_kr": {"kill_state": "killed", "dd_ratio": 1.05},
        "hybrid_vb_us": {"kill_state": "ok", "dd_ratio": 0.2},
    }
}


def annotate(strategies):
    return G.annotate_backtest(strategies, registry=REGISTRY, health=HEALTH)


def test_filed_backtest_becomes_a_block():
    s = [{"id": "quant40"}]
    annotate(s)
    assert s[0]["backtest"]["cagr_pct"] == 9.7
    assert s[0]["backtest"]["mdd_pct"] == -20.4
    assert s[0]["backtest"]["kill_threshold_pct"] == -20.4
    assert s[0]["kill_state"] == "warn"
    assert s[0]["dd_ratio"] == 0.97


def test_no_backtest_means_no_block_at_all():
    s = [{"id": "jd_strategy"}, {"id": "nmf2"}]
    annotate(s)
    for row in s:
        assert "backtest" not in row       # omit-shaped, never null-filled
        assert row["kill_state"] == "ok"   # but the badge input always exists


def test_hybrid_gets_per_leg_blocks_and_worst_leg_state():
    s = [{"id": "hybrid_vb"}]
    annotate(s)
    assert s[0]["backtest_kr"]["cagr_pct"] == 37.8
    assert s[0]["backtest_us"]["mdd_pct"] == -19.9
    assert "backtest" not in s[0]
    assert s[0]["kill_state"] == "killed"      # worse of killed/ok
    assert s[0]["dd_ratio"] == 1.05


def test_missing_registry_and_health_are_harmless():
    s = [{"id": "quant40"}, {"id": "manual"}]
    G.annotate_backtest(s, registry={}, health={})
    assert all("backtest" not in row for row in s)
    assert all(row["kill_state"] == "ok" for row in s)
