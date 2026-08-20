"""The asset-management layer, as the dashboard reports it.

The allocator decides how much capital each bot runs; the bots only READ the
budget it writes. That inversion is the governance model, and until 2026-08-20
it was invisible on the page — you could see what a bot DID with its money and
never see who set the amount, or that a bot had been dropped from the roster.

Two distinctions this block must not blur:

  * **applied vs proposed.** apply.py is the step a human owns. The proposal
    file keeps status "proposed" even after the budgets are written, so
    freshness has to come from last_applied.json, never from the proposal.
  * **native vs KRW.** `current_budget` is USD for the KIS-US bots while
    `target_krw` is already KRW. Compared raw, a $14.5k budget looks like a
    rounding error beside a W27.7M target.

Run: python3 -m pytest tests/test_allocation_block.py -q
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

NOW = datetime(2026, 8, 20, 20, 9)
FX = 1411.0

PROPOSAL = {
    "proposed_at": "2026-08-20T08:20:02",
    "status": "proposed",
    "policy_version": "v2-fleet-etf-equal-weight",
    "capital": {"total_krw": 541270441, "fx": FX},
    "level0": {"manual_krw": 188850155, "residual_krw": 352420286},
    "level1": {"X": 0.8, "binding": "cash_floor", "brake": 1.0,
               "kelly_half": 1.1289, "x_vol_cap": 0.94, "sigma": 0.1064,
               "mu_shrunk": 0.0256, "fleet_dd": 0.0462, "n_days": 108},
    "level2": {"roster": ["quant40", "nmf2"], "N": 7,
               "fleet_pool_krw": 281936229, "per_bot_krw": 40276604},
    "allocations": {
        "quant40": {"in_etf": True, "account": "kis_us", "currency": "USD",
                    "current_budget": 14533.14, "target_krw": 27683452,
                    "ramped": True, "kept_by_turnover_band": False},
        "nmf2": {"in_etf": True, "account": "toss", "currency": "KRW",
                 "current_budget": 20055742.03, "target_krw": 27075252},
        "btc_vb": {"in_etf": False, "account": "upbit", "currency": "KRW",
                   "why": "계좌 제외(Upbit)", "current_budget": 10000000,
                   "target_krw": 0},
    },
    "totals": {"deployed_krw": 381417919, "cash_pct": 0.2953},
}

APPLIED = {"applied_at": "2026-08-20T08:20:02",
           "manual_krw": 188850155,
           "targets": {"quant40": 27683452, "nmf2": 27075252, "btc_vb": 0}}


def _state(tmp_path, proposal=PROPOSAL, applied=APPLIED, name="proposed_20260820.json"):
    (tmp_path / name).write_text(json.dumps(proposal))
    if applied is not None:
        (tmp_path / "last_applied.json").write_text(json.dumps(applied))
    return str(tmp_path)


def test_usd_budgets_are_normalised_to_krw(tmp_path):
    """The comparison is meaningless while one side is dollars."""
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)
    q = [b for b in a["bots"] if b["id"] == "quant40"][0]

    assert abs(q["current_krw"] - 14533.14 * FX) < 1
    assert q["target_krw"] == 27683452
    assert abs(q["drift_krw"] - (q["target_krw"] - q["current_krw"])) < 1
    # KRW bots pass through untouched.
    n = [b for b in a["bots"] if b["id"] == "nmf2"][0]
    assert abs(n["current_krw"] - 20055742.03) < 1


def test_three_levels_are_reported(tmp_path):
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)

    assert a["level0"]["manual_krw"] == 188850155
    assert a["level0"]["residual_krw"] == 352420286
    assert a["level1"]["X"] == 0.8
    # WHICH constraint bound X is the interesting part: kelly, the vol cap and
    # the cash floor produce one number with three different meanings.
    assert a["level1"]["binding"] == "cash_floor"
    assert a["level2"]["N"] == 7
    assert a["level2"]["per_bot_krw"] == 40276604


def test_applied_is_distinct_from_proposed(tmp_path):
    """Governance: nothing moves until apply.py writes it."""
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)
    assert a["applied"] is True
    q = [b for b in a["bots"] if b["id"] == "quant40"][0]
    assert q["applied_krw"] == 27683452

    # Same proposal, never applied.
    d = tmp_path / "unapplied"
    d.mkdir()
    b = G.load_allocation(_state(d, applied=None), now=NOW, fx_rate=FX)
    assert b["applied"] is False
    assert [x for x in b["bots"] if x["id"] == "quant40"][0]["applied_krw"] is None


def test_excluded_bots_keep_their_reason(tmp_path):
    """'Not in the fleet' is only useful with the why attached."""
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)
    btc = [b for b in a["bots"] if b["id"] == "btc_vb"][0]
    assert btc["in_etf"] is False
    assert "Upbit" in btc["why"]
    # In-fleet bots sort ahead of excluded ones.
    assert a["bots"][-1]["in_etf"] is False


def test_stale_allocation_is_flagged(tmp_path):
    """The allocator runs daily; a two-day-old apply means it missed a run."""
    old = dict(APPLIED, applied_at="2026-08-18T08:20:02")
    a = G.load_allocation(_state(tmp_path, applied=old), now=NOW, fx_rate=FX)
    assert a["age_hours"] > G.ALLOC_MAX_AGE_HOURS
    assert a["stale"] is True


def test_missing_state_returns_none_rather_than_raising(tmp_path):
    """A publish must never fail because the allocator is absent."""
    assert G.load_allocation(str(tmp_path / "nope"), now=NOW, fx_rate=FX) is None


def test_unreadable_proposal_returns_none(tmp_path):
    (tmp_path / "proposed_20260820.json").write_text("{ not json")
    assert G.load_allocation(str(tmp_path), now=NOW, fx_rate=FX) is None
