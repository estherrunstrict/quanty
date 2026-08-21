"""Reducing hands-on capital must reach Level 2 immediately.

Hands-on is SENIOR: residual = capital - hands-on. Sell part of the sleeve and
the residual grows, so the fleet's 1/N share grows with it. But the Target
column was a function of the residual the allocator saw at 08:20, so the whole
point of reducing hands-on — more capital for the fleet — stayed invisible until
the next morning's run.

Targets are now re-derived from the CURRENT residual using the allocator's own
two rules (ramp, then turnover band). These are PROJECTIONS for the panel; what
a bot actually holds is `current_krw`.

Run: python3 -m pytest tests/test_live_target.py -q
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

NOW = datetime(2026, 8, 21, 17, 0)
FX = 1400.0
CAP, MAN = 540_000_000.0, 175_000_000.0

PROPOSAL = {
    "proposed_at": "2026-08-21T08:20:02",
    "capital": {"total_krw": CAP, "fx": FX},
    "level0": {"manual_krw": MAN, "residual_krw": CAP - MAN},
    "level1": {"X": 0.8, "binding": "cash_floor", "brake": 1.0},
    "level2": {"N": 7, "fleet_pool_krw": (CAP - MAN) * 0.8,
               "per_bot_krw": (CAP - MAN) * 0.8 / 7, "roster": ["quant40"]},
    "allocations": {
        # ramped: target == current * 1.35 exactly, which is how RAMP_MAX is
        # read back out of the proposal instead of hard-coded here.
        "quant40": {"in_etf": True, "account": "kis_us", "currency": "KRW",
                    "current_budget": 20_000_000.0, "target_budget": 27_000_000.0,
                    "target_krw": 27_000_000, "ramped": True},
    },
}


def _state(tmp_path):
    (tmp_path / "proposed_20260821.json").write_text(json.dumps(PROPOSAL))
    return str(tmp_path)


def _load(tmp_path, manual):
    return G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX,
                             live_totals={"investments_krw": CAP, "manual_krw": manual})


def test_ramp_max_is_read_back_from_the_proposal():
    """Never hard-coded — it would drift the first time AM_RAMP_MAX is tuned."""
    assert abs(G.proposal_ramp_max(PROPOSAL["allocations"]) - 1.35) < 1e-9


def test_cutting_hands_on_raises_the_fleet_share_immediately(tmp_path):
    full = _load(tmp_path, MAN)
    half = _load(tmp_path, MAN / 2)

    assert half["level0"]["residual_krw"] > full["level0"]["residual_krw"]
    assert half["level2"]["per_bot_krw"] > full["level2"]["per_bot_krw"]
    q = [b for b in half["bots"] if b["id"] == "quant40"][0]
    assert q["uncapped_krw"] == half["level2"]["per_bot_krw"]


def test_selling_the_whole_sleeve_is_not_read_as_missing_data(tmp_path):
    """`x or fallback` treats a legitimate 0 as absent.

    A hands-on sleeve of exactly zero silently reverted to the allocator's stale
    figure and the residual COLLAPSED instead of jumping to the full capital —
    the one case most worth getting right.
    """
    a = _load(tmp_path, 0.0)
    assert a["level0"]["manual_krw"] == 0.0
    assert abs(a["level0"]["residual_krw"] - CAP) < 1
    assert abs(a["level2"]["per_bot_krw"] - CAP * 0.8 / 7) < 1


def test_target_still_obeys_the_ramp(tmp_path):
    """The freed capital arrives over several runs, not at once.

    Showing 1/N as the target the day the sleeve is sold would display a budget
    no bot is going to be given.
    """
    a = _load(tmp_path, 0.0)
    q = [b for b in a["bots"] if b["id"] == "quant40"][0]

    assert q["limited_by"] == "ramp"
    assert abs(q["target_krw"] - 20_000_000.0 * 1.35) < 1
    assert q["target_krw"] < q["uncapped_krw"]
    assert q["runs_to_target"] >= 2


def test_target_follows_the_share_once_the_ramp_no_longer_binds(tmp_path):
    """When 1/N is within one ramp step, the target IS the share."""
    # Residual small enough that 1/N < current * 1.35.
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX,
                          live_totals={"investments_krw": 200_000_000.0,
                                       "manual_krw": 0.0})
    q = [b for b in a["bots"] if b["id"] == "quant40"][0]
    share = a["level2"]["per_bot_krw"]

    assert share < 20_000_000.0 * 1.35
    assert q["limited_by"] != "ramp"
    assert abs(q["target_krw"] - share) < 1 or q["limited_by"] == "band"


def test_a_move_under_the_band_is_not_worth_making(tmp_path):
    """The allocator holds a bot whose move is under the turnover band."""
    tgt, limited = G._live_target(20_000_000.0, 20_500_000.0, 1.35)
    assert limited == "band"
    assert tgt == 20_000_000.0


def test_drift_always_matches_target_minus_current(tmp_path):
    for manual in (MAN, MAN / 2, 0.0):
        a = _load(tmp_path, manual)
        for b in a["bots"]:
            if b["in_etf"]:
                assert abs(b["drift_krw"] - (b["target_krw"] - b["current_krw"])) < 1
