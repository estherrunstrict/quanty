"""A bot that owns its whole account still gets a budget, a cost and a rate.

btc_vb is the one bot the allocator leaves out — Upbit is not an investable
account under the capital policy — so it carries no allocated budget and runs on
whatever the exchange holds. dashboard_server computes profit_rate_ytd_pct as
total_pl / BUDGET and returns None when the budget is zero, so on 2026-08-20 a
bot that was fully invested and up W367k YTD showed three blanks: Budget Cap
"Unlimited", Invested "—", Profit % "—".

For such a bot the account balance IS the budget, and `value` already is that
balance. Deployed cost is value minus unrealized gain.

Run: python3 -m pytest tests/test_capital_fields.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402


def _btc(**kw):
    s = {"id": "btc_vb", "value": 9808473.0, "budget": 0,
         "unrealized_profit": 453472.0, "realized_profit_ytd": -86641.0,
         "total_pl_ytd": 366830.0, "profit_rate_ytd_pct": None}
    s.update(kw)
    return s


def test_account_balance_becomes_the_budget():
    s = _btc()
    G.annotate_capital_fields([s])

    assert s["budget"] == 9808473.0          # == the Upbit account total
    assert s["budget_basis"] == "account"


def test_invested_is_capital_deployed_not_zero():
    """value - unrealized. A bold W0 beside a W9.8M position reads as a wipeout."""
    s = _btc()
    G.annotate_capital_fields([s])

    assert s["cost_basis"] == 9808473.0 - 453472.0
    assert s["cost_basis_basis"] == "derived"


def test_rate_uses_the_budget_so_it_compares_with_other_bots():
    """Every other bot's rate is total_pl / budget; this one must match."""
    s = _btc()
    G.annotate_capital_fields([s])

    assert s["profit_rate_basis"] == "budget"
    assert abs(s["profit_rate_ytd_pct"] - 366830.0 / 9808473.0 * 100) < 0.01


def test_existing_budget_and_rate_are_left_alone():
    """A funded bot must not be touched — its budget is the allocator's."""
    s = {"id": "quant40", "value": 13833.9, "budget": 14533.14,
         "cost_basis": 13862.45, "unrealized_profit": -28.55,
         "total_pl_ytd": 1670.94, "profit_rate_ytd_pct": 11.5}
    G.annotate_capital_fields([s])

    assert s["budget"] == 14533.14
    assert s["cost_basis"] == 13862.45
    assert s["profit_rate_ytd_pct"] == 11.5
    assert s["profit_rate_basis"] == "budget"
    assert "budget_basis" not in s


def test_a_genuinely_unfunded_bot_is_not_given_a_budget():
    """korea_etf is paper at value 0 — inventing a budget would be a lie."""
    s = {"id": "korea_etf", "value": 0, "budget": 0, "cost_basis": 0,
         "unrealized_profit": 0, "total_pl_ytd": -1246635.0,
         "profit_rate_ytd_pct": None}
    G.annotate_capital_fields([s])

    assert not s["budget"]
    assert s["profit_rate_ytd_pct"] is None     # no denominator exists
    assert "budget_basis" not in s


def test_deployed_fallback_is_labelled_when_no_budget_can_be_found():
    """Different denominator, so it must not be read as a budget return."""
    s = {"id": "odd", "value": 0, "budget": 0, "cost_basis": 5000.0,
         "unrealized_profit": 0, "total_pl_ytd": 500.0,
         "profit_rate_ytd_pct": None}
    G.annotate_capital_fields([s])

    assert s["profit_rate_basis"] == "deployed"
    assert abs(s["profit_rate_ytd_pct"] - 10.0) < 0.01


def test_hands_on_sleeve_is_never_given_a_budget():
    """It is senior capital, not an allocation — a budget there is meaningless."""
    s = {"id": "manual", "value": 176757222.0, "budget": 0,
         "unrealized_profit": 0, "total_pl_ytd": -14866327.0,
         "profit_rate_ytd_pct": None}
    G.annotate_capital_fields([s])

    assert not s.get("budget")
    assert "budget_basis" not in s
