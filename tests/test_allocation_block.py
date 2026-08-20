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
        # target_budget is the NATIVE amount and is what the panel converts;
        # target_krw is the allocator's own conversion, kept for reference.
        "quant40": {"in_etf": True, "account": "kis_us", "currency": "USD",
                    "current_budget": 14533.14, "target_budget": 19619.74,
                    "target_krw": 27683452,
                    "ramped": True, "kept_by_turnover_band": False},
        "nmf2": {"in_etf": True, "account": "toss", "currency": "KRW",
                 "current_budget": 20055742.03, "target_budget": 27075251.74,
                 "target_krw": 27075252},
        "btc_vb": {"in_etf": False, "account": "upbit", "currency": "KRW",
                   "why": "계좌 제외(Upbit)", "current_budget": 10000000,
                   "target_budget": 0.0, "target_krw": 0},
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
    assert abs(q["target_krw"] - 19619.74 * FX) < 1
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
    assert abs(a["level2"]["per_bot_krw"] - 40276604) < 2


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


def test_ramp_explains_why_target_is_below_the_1_over_n_share(tmp_path):
    """The panel showed 'per bot W40.3M' beside a W27.7M target and looked broken.

    The missing sentence was the ramp: no bot grows more than RAMP_MAX per daily
    run, so today's target is current x factor, not the 1/N share. Both numbers
    have to travel together or the arithmetic reads as a contradiction.
    """
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)
    q = [b for b in a["bots"] if b["id"] == "quant40"][0]

    assert abs(q["uncapped_krw"] - 40276604) < 2  # what 1/N alone would give
    assert abs(q["target_krw"] - 19619.74 * FX) < 1   # what the ramp allows today
    assert q["limited_by"] == "ramp"
    # Derived from the data, never hard-coded: 27,683,452 / 20,506,261 = 1.35
    assert abs(q["ramp_factor"] - 1.35) < 0.001
    # And it says how long the gap takes to close, because "capped" alone does
    # not distinguish two days from two months.
    assert q["runs_to_target"] == 3


def test_bots_at_target_are_not_labelled_as_limited(tmp_path):
    """A bot already at its 1/N share has nothing holding it back."""
    prop = json.loads(json.dumps(PROPOSAL))
    prop["allocations"]["nmf2"]["target_krw"] = prop["level2"]["per_bot_krw"]
    a = G.load_allocation(_state(tmp_path, proposal=prop), now=NOW, fx_rate=FX)
    n = [b for b in a["bots"] if b["id"] == "nmf2"][0]

    assert n["limited_by"] == ""
    assert n["ramp_factor"] is None
    assert n["runs_to_target"] is None


def test_capital_provenance_travels_with_the_number(tmp_path):
    """The hero and this panel price the same money hours apart.

    Without the measurement time and FX on the panel, the two totals just look
    like a contradiction (W541.3M here vs W540.1M in the hero on 2026-08-20).
    """
    (tmp_path / "bots.json").write_text(json.dumps({
        "as_of": "2026-08-20 06:30 KST", "capital_basis": "measured+external",
        "total_capital_krw": 541270441, "fx_usdkrw": FX}))
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=1402.5)

    assert a["capital_as_of"] == "2026-08-20 06:30 KST"
    assert a["capital_basis"] == "measured+external"
    # Provenance travels even though the panel now prices itself in the
    # DASHBOARD's frame (see test_panel_is_priced_in_the_dashboards_frame):
    # knowing WHEN the allocator measured is what explains a stale input.
    assert a["fx"] == 1402.5
    assert a["allocator_frame"]["fx"] == FX


def test_block_holds_together_arithmetically(tmp_path):
    """residual x X == fleet pool, and pool / N == the 1/N share."""
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX)
    L0, L1, L2 = a["level0"], a["level1"], a["level2"]

    assert abs(L0["residual_krw"] * L1["X"] - L2["fleet_pool_krw"]) < 2
    assert abs(L2["fleet_pool_krw"] / L2["N"] - L2["per_bot_krw"]) < 2


def test_runs_to_target_is_none_when_it_cannot_be_known(tmp_path):
    assert G._runs_to_target(0, 100, 50) is None          # no current budget
    assert G._runs_to_target(100, 50, 60) is None         # already past 1/N
    assert G._runs_to_target(100, 200, 100) is None       # factor 1.0, never arrives
    assert G._runs_to_target(None, 200, 100) is None


def test_panel_is_priced_in_the_dashboards_frame(tmp_path):
    """Total capital on the panel must equal Total Investments in the hero.

    Two different "total capital" figures on one page read as a bug no matter
    how carefully the footnote explains the FX and the clock. The chain is
    RECOMPUTED at the dashboard's frame rather than rescaled, so it still closes.
    """
    live = {"investments_krw": 539329199.0, "manual_krw": 176757222.0}
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=1402.5, live_totals=live)

    assert a["capital_krw"] == live["investments_krw"]      # matches the hero
    assert a["level0"]["manual_krw"] == live["manual_krw"]
    assert a["fx"] == 1402.5                                # dashboard's rate, not 1411
    assert a["repriced_live"] is True

    # And the arithmetic still closes in the new frame.
    L0, L1, L2 = a["level0"], a["level1"], a["level2"]
    assert abs(L0["residual_krw"] - (live["investments_krw"] - live["manual_krw"])) < 1
    assert abs(L0["residual_krw"] * L1["X"] - L2["fleet_pool_krw"]) < 2
    assert abs(L2["fleet_pool_krw"] / L2["N"] - L2["per_bot_krw"]) < 2


def test_ramp_factor_survives_the_reprice(tmp_path):
    """FX must cancel out of the ramp ratio.

    Converting `current` at the dashboard rate while taking `target_krw` straight
    from the proposal (written at the allocator's rate) smears the FX difference
    into their ratio — the ramp then reads x1.36 instead of the x1.35 actually
    applied. Both sides come from the native amounts.
    """
    live = {"investments_krw": 539329199.0, "manual_krw": 176757222.0}
    for rate in (1402.5, 1411.0, 1500.0):
        a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=rate, live_totals=live)
        q = [b for b in a["bots"] if b["id"] == "quant40"][0]
        assert abs(q["ramp_factor"] - 1.35) < 0.001, "ramp drifted at fx {}".format(rate)
        # target/current in KRW must agree with the native ratio at every rate.
        assert abs(q["target_krw"] / q["current_krw"] - 1.35) < 0.001


def test_allocator_frame_is_preserved_for_comparison(tmp_path):
    """What the allocator actually used stays visible, so a stale input shows.

    On 2026-08-20 collect.py read a dashboard whose Toss sleeve was a day old,
    so hands-on came in W12.1M high and every bot was sized off an understated
    residual. That is only findable if both frames are reported.
    """
    live = {"investments_krw": 539329199.0, "manual_krw": 176757222.0}
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=1402.5, live_totals=live)

    assert a["allocator_frame"]["manual_krw"] == 188850155
    assert a["allocator_frame"]["residual_krw"] == 352420286
    assert a["allocator_frame"]["fx"] == FX
    # The live figure and the allocator's differ — that gap is the point.
    assert a["level0"]["manual_krw"] != a["allocator_frame"]["manual_krw"]


def test_falls_back_to_allocator_frame_without_live_totals(tmp_path):
    """A dry run has no totals block; the panel must still render."""
    a = G.load_allocation(_state(tmp_path), now=NOW, fx_rate=FX, live_totals=None)
    assert a["capital_krw"] == 541270441
    assert a["level0"]["residual_krw"] == 352420286
    assert a["repriced_live"] is False
