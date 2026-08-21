"""Hands-on is deducted INSIDE X, not before it.

The old chain was `pool = X * (C - hands_on)`, which quietly broke the cash
floor. Hands-on positions are exposure too, so total exposure came to
`hands_on + X*(C - hands_on)` — on 2026-08-21 that was 86.4% of capital against
an X of 80%, leaving 13.6% cash under a 20% floor, while `binding` cheerfully
reported "cash_floor".

The chain (user-specified 2026-08-21):

    L0  total capital                C
    L1  X                            exposure budget = C * X
    L2  budget - hands-on = pool,    per bot = pool / N

Total exposure is then exactly C*X and the floor holds by construction.

The cost, deliberately accepted: when X is low the fleet can go to zero, because
hands-on alone can exceed the whole allowance. On 2026-08-08..13 X sat at X_MIN
(10%) and C*X was W60M against W177M hands-on. daily.py's MAX_STEP rail holds a
change that large for a human, so a fleet cannot be silently recalled.

Run: python3 -m pytest tests/test_exposure_budget.py -q
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

NOW = datetime(2026, 8, 21, 18, 0)
FX = 1400.0
C, M, N = 540_000_000.0, 175_000_000.0, 7

PROPOSAL = {
    "proposed_at": "2026-08-21T08:20:02",
    "capital": {"total_krw": C, "fx": FX},
    "level0": {"manual_krw": M, "residual_krw": C - M},
    "level1": {"X": 0.8, "binding": "cash_floor", "brake": 1.0},
    "level2": {"N": N, "fleet_pool_krw": 0, "per_bot_krw": 0, "roster": ["quant40"]},
    "allocations": {
        "quant40": {"in_etf": True, "account": "kis_us", "currency": "KRW",
                    "current_budget": 20_000_000.0, "target_budget": 27_000_000.0,
                    "target_krw": 27_000_000, "ramped": True},
    },
}


def _load(tmp_path, manual=M, capital=C, X=None):
    prop = json.loads(json.dumps(PROPOSAL))
    if X is not None:
        prop["level1"]["X"] = X
    (tmp_path / "proposed_20260821.json").write_text(json.dumps(prop))
    return G.load_allocation(str(tmp_path), now=NOW, fx_rate=FX,
                             live_totals={"investments_krw": capital,
                                          "manual_krw": manual})


def test_exposure_budget_is_x_times_total_capital(tmp_path):
    a = _load(tmp_path)
    assert abs(a["level2"]["exposure_budget_krw"] - C * 0.8) < 1


def test_fleet_pool_is_the_budget_minus_hands_on(tmp_path):
    a = _load(tmp_path)
    assert abs(a["level2"]["fleet_pool_krw"] - (C * 0.8 - M)) < 1
    assert abs(a["level2"]["per_bot_krw"] - (C * 0.8 - M) / N) < 1


def test_total_exposure_equals_x_so_the_cash_floor_holds(tmp_path):
    """The whole point. Under the old chain this came to 86.4%, not 80%."""
    a = _load(tmp_path)
    total = a["level0"]["manual_krw"] + a["level2"]["fleet_pool_krw"]
    assert abs(total / C - 0.8) < 1e-6
    assert abs((1 - total / C) - 0.2) < 1e-6


def test_the_old_chain_would_have_overshot(tmp_path):
    """Pin the bug so nobody 'restores' it."""
    old_total = M + (C - M) * 0.8
    assert old_total / C > 0.86          # overshoots the 80% allowance
    a = _load(tmp_path)
    new_total = a["level0"]["manual_krw"] + a["level2"]["fleet_pool_krw"]
    assert new_total < old_total


def test_cutting_hands_on_frees_exactly_that_much_for_the_fleet(tmp_path):
    """Now a linear, one-for-one relationship — easier to reason about."""
    full = _load(tmp_path, manual=M)
    half = _load(tmp_path, manual=M / 2)
    freed = half["level2"]["fleet_pool_krw"] - full["level2"]["fleet_pool_krw"]
    assert abs(freed - M / 2) < 1


def test_low_x_recalls_the_fleet_and_says_so(tmp_path):
    """X_MIN 10% with W175M hands-on: the allowance is W54M, all of it senior."""
    a = _load(tmp_path, X=0.10)
    assert a["level2"]["exposure_budget_krw"] < a["level0"]["manual_krw"]
    assert a["level2"]["fleet_pool_krw"] == 0
    assert a["level2"]["per_bot_krw"] == 0
    assert a["level2"]["fleet_recalled"] is True


def test_pool_never_goes_negative(tmp_path):
    a = _load(tmp_path, manual=C)          # everything in hands-on
    assert a["level2"]["fleet_pool_krw"] == 0
    assert a["level2"]["per_bot_krw"] == 0


def test_selling_the_whole_sleeve_gives_the_fleet_the_full_budget(tmp_path):
    a = _load(tmp_path, manual=0.0)
    assert abs(a["level2"]["fleet_pool_krw"] - C * 0.8) < 1
    assert a["level2"]["fleet_recalled"] is False
