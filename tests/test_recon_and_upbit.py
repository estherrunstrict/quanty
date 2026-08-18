"""Unit tests for the 2026-08-18 reconciliation + Upbit-staleness fixes.

Both fixes are the kind that only misbehave in a state you cannot reach on
demand — a bot mid-fill, or a feed that died three days ago — so they are
pinned with fixtures rather than waiting for the condition in production.

Run: ~/myenv/bin/python3 -m pytest tests/test_recon_and_upbit.py -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

NOW = datetime(2026, 8, 18, 20, 0, 0)


def _totals(us_rows=(), kr_rows=()):
    return {"kr": {"holdings": list(kr_rows)}, "us": {"holdings": list(us_rows)}}


# ── reconcile_underreported_bot_holdings ─────────────────────────────────────
def test_tops_up_the_sole_claimant_to_the_account_quantity():
    """The 2026-08-17 case: bot wrote its file a second after the fill."""
    api = {"strategies": [
        {"id": "quant40", "holdings": [
            {"ticker": "SPY", "quantity": 7, "value": 5352.91,
             "avg_price": 764.70, "profit": 0.0}]},
    ]}
    totals = _totals(us_rows=[
        {"ticker": "SPY", "quantity": 13, "value": 10012.66, "profit": -13.19}])

    topped = G.reconcile_underreported_bot_holdings(api, totals)

    assert topped == [("quant40", "SPY", 7.0, 13.0)]
    row = api["strategies"][0]["holdings"][0]
    assert row["quantity"] == 13
    assert row["bot_reported_qty"] == 7.0
    assert row["topped_up_from_account"] is True
    # Cost basis is the BROKER's blend across both lots, not the bot's.
    assert abs(row["avg_price"] - (10012.66 + 13.19) / 13) < 1e-6
    # The card total follows its own rows.
    assert abs(api["strategies"][0]["value"] - 10012.66) < 0.01


def test_leaves_an_ambiguous_ticker_in_the_gap():
    """Two cards claim it — guessing an owner would put money on the wrong bot."""
    api = {"strategies": [
        {"id": "quant40", "holdings": [{"ticker": "SPY", "quantity": 4, "value": 1.0}]},
        {"id": "jd_strategy", "holdings": [{"ticker": "SPY", "quantity": 3, "value": 1.0}]},
    ]}
    totals = _totals(us_rows=[
        {"ticker": "SPY", "quantity": 13, "value": 10012.66, "profit": 0.0}])

    assert G.reconcile_underreported_bot_holdings(api, totals) == []
    assert api["strategies"][0]["holdings"][0]["quantity"] == 4


def test_ignores_a_non_kis_account_bot():
    """NMF2 trades Toss; Korea's 6-digit tickers collide with KIS names."""
    api = {"strategies": [
        {"id": "nmf2", "account": "toss",
         "holdings": [{"ticker": "069500", "quantity": 5, "value": 1.0}]},
    ]}
    totals = _totals(kr_rows=[
        {"ticker": "069500", "quantity": 13, "value": 999.0, "profit": 0.0}])

    assert G.reconcile_underreported_bot_holdings(api, totals) == []


def test_does_nothing_when_the_bot_already_matches():
    api = {"strategies": [
        {"id": "quant40", "holdings": [{"ticker": "SPY", "quantity": 13, "value": 1.0}]}]}
    totals = _totals(us_rows=[
        {"ticker": "SPY", "quantity": 13, "value": 10012.66, "profit": 0.0}])

    assert G.reconcile_underreported_bot_holdings(api, totals) == []


def test_never_shrinks_a_bot_that_claims_more_than_the_account():
    """A bot over-claiming is a bot-side bug; this function must not mask it by
    quietly rewriting the position downward."""
    api = {"strategies": [
        {"id": "quant40", "holdings": [{"ticker": "SPY", "quantity": 20, "value": 1.0}]}]}
    totals = _totals(us_rows=[
        {"ticker": "SPY", "quantity": 13, "value": 10012.66, "profit": 0.0}])

    assert G.reconcile_underreported_bot_holdings(api, totals) == []
    assert api["strategies"][0]["holdings"][0]["quantity"] == 20


# ── _days_since ─────────────────────────────────────────────────────────────
def test_days_since_parses_the_shapes_bots_actually_write():
    assert G._days_since("2026-08-17", NOW) == 1.83
    assert G._days_since("2026-08-17 20:00", NOW) == 1.0
    assert G._days_since("2026-08-17 20:00 KST", NOW) == 1.0
    assert G._days_since("2026-08-17T22:30:09.859462", NOW) == 1.83


def test_days_since_returns_none_rather_than_claiming_freshness():
    """An unreadable stamp must read as unknown, never as 0 days old."""
    assert G._days_since(None, NOW) is None
    assert G._days_since("", NOW) is None
    assert G._days_since("last tuesday", NOW) is None


# ── Upbit staleness ─────────────────────────────────────────────────────────
def _accounts_with_btc_last_run(last_run):
    strategies = [{"id": "btc_vb", "value": 9737967.0,
                   "is_holding": False, "last_run": last_run}]
    portfolio = {"upbit_krw": 9737967.0, "exchange_rate": 1415.2}
    return G.build_accounts(None, portfolio, {}, strategies, NOW)["upbit"]


def test_upbit_fresh_within_the_window():
    up = _accounts_with_btc_last_run("2026-08-17")
    assert up["stale"] is False
    assert up["as_of"] == "2026-08-17"
    assert up["age_days"] == 1.83


def test_upbit_goes_stale_on_age_and_keeps_the_money():
    """The old rule could only fire on a zero, so a dead feed republished the
    same number forever without ever being flagged."""
    up = _accounts_with_btc_last_run("2026-08-10")
    assert up["stale"] is True
    # Toss contract: as_of is None when stale, the real stamp moves aside.
    assert up["as_of"] is None
    assert up["last_seen_at"] == "2026-08-10"
    assert "no live Upbit query" in up["note"]
    # The balance is real; zeroing it would understate net worth by ~W9.7M.
    assert up["total_krw"] == 9737967.0
    assert up["cash_krw"] == 9737967.0


def test_upbit_still_stale_on_a_zero_balance():
    strategies = [{"id": "btc_vb", "value": 0.0, "is_holding": False,
                   "last_run": "2026-08-18"}]
    up = G.build_accounts(None, {"upbit_krw": 0.0}, {}, strategies, NOW)["upbit"]
    assert up["stale"] is True


def test_upbit_unknown_last_run_is_not_treated_as_stale_by_age():
    """Unknown age must not fabricate an alarm either — only a zero balance or a
    genuinely old stamp flips the flag."""
    up = _accounts_with_btc_last_run(None)
    assert up["stale"] is False
    assert "age_days" not in up
