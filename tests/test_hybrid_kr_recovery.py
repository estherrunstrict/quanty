"""Hybrid VB KR leg: recover a dropped basket from the account balance.

Reproduces 2026-08-19, where HYBRID_VB_KR wrote `holdings: []` while really
holding five ETFs. `open_positions` covered only the two the VB rule had
entered (132030, 144600), so the other three — 069500, 305720, 364690, W4.37M —
were claimed by nobody and surfaced as Jae's own hand-picked stock.

Run: python3 -m pytest tests/test_hybrid_kr_recovery.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

REGIME = {"069500": "BEAR", "229200": "BEAR", "305720": "BEAR", "091170": "NEUTRAL",
          "364690": "BEAR", "132030": "BULL", "144600": "NEUTRAL"}

ACCT_KR = [
    {"ticker": "069500", "quantity": 13, "value": 1_406_080, "profit": -284_895,
     "profit_rate": -16.7},
    {"ticker": "132030", "quantity": 406, "value": 10_466_680, "profit": 615_090,
     "profit_rate": 6.2},
    {"ticker": "144600", "quantity": 229, "value": 2_575_105, "profit": -27_480,
     "profit_rate": -1.06},
    {"ticker": "305720", "quantity": 97, "value": 1_465_185, "profit": -492_225,
     "profit_rate": -25.1},
    {"ticker": "364690", "quantity": 45, "value": 1_759_500, "profit": -381_150,
     "profit_rate": -17.8},
]


def _totals(rows=None):
    return {"kr": {"holdings": list(rows if rows is not None else ACCT_KR)},
            "us": {"holdings": []}}


def _hybrid(kr_holdings, open_kr=None, regime=REGIME):
    return {"id": "hybrid_vb", "currency": "MULTI", "budget_kr": 36_551_589.85,
            "kr": {"holdings": list(kr_holdings), "regime": dict(regime),
                   "value": 0, "realized_profit_ytd": 0},
            "us": {"holdings": []},
            "open_positions": {"kr": dict(open_kr or {}), "us": {}}}


def test_recovers_the_whole_basket_on_a_dropout():
    hybrid = _hybrid([], open_kr={"132030": {"shares": 406}, "144600": {"shares": 229}})
    api = {"strategies": [hybrid]}

    got = G.recover_hybrid_vb_kr_holdings(api, _totals())

    assert {t for t, _ in got} == {"069500", "132030", "144600", "305720", "364690"}
    kr = api["strategies"][0]["kr"]
    assert len(kr["holdings"]) == 5
    assert kr["value"] == 17_672_550          # the full KR leg, not just the 2 entered
    assert all(h["recovered_from_account"] for h in kr["holdings"])
    # Cost basis is derived from the account's own profit figure.
    row = next(h for h in kr["holdings"] if h["ticker"] == "132030")
    assert row["quantity"] == 406
    assert abs(row["avg_price"] - (10_466_680 - 615_090) / 406) < 1e-4


def test_does_not_touch_a_leg_that_reported_normally():
    live = [{"ticker": "132030", "quantity": 406, "value": 10_466_680}]
    hybrid = _hybrid(live)
    api = {"strategies": [hybrid]}

    assert G.recover_hybrid_vb_kr_holdings(api, _totals()) == []
    assert api["strategies"][0]["kr"]["holdings"] == live


def test_never_takes_a_ticker_another_card_publishes():
    hybrid = _hybrid([])
    kem = {"id": "korea_etf",
           "holdings": [{"ticker": "132030", "quantity": 406, "value": 10_466_680}]}
    api = {"strategies": [hybrid, kem]}

    got = G.recover_hybrid_vb_kr_holdings(api, _totals())

    assert "132030" not in {t for t, _ in got}


def test_never_takes_a_ticker_another_bots_ledger_names():
    """A card can drop `holdings` and still know what it entered."""
    hybrid = _hybrid([])
    other = {"id": "korea_etf", "holdings": [],
             "open_positions": {"132030": {"shares": 406, "entry_price": 24350.0}}}
    api = {"strategies": [hybrid, other]}

    got = G.recover_hybrid_vb_kr_holdings(api, _totals())

    assert "132030" not in {t for t, _ in got}
    assert "069500" in {t for t, _ in got}          # the rest still recover


def test_ignores_account_rows_outside_the_bots_universe():
    rows = ACCT_KR + [{"ticker": "999999", "quantity": 5, "value": 500_000,
                       "profit": 0, "profit_rate": 0}]
    hybrid = _hybrid([])
    api = {"strategies": [hybrid]}

    got = G.recover_hybrid_vb_kr_holdings(api, _totals(rows))

    assert "999999" not in {t for t, _ in got}


def test_no_universe_means_no_claim():
    """Without regime or a ledger there is no defensible basis to claim rows."""
    hybrid = _hybrid([], regime={})
    hybrid["open_positions"] = {"kr": {}, "us": {}}
    api = {"strategies": [hybrid]}

    assert G.recover_hybrid_vb_kr_holdings(api, _totals()) == []


def test_korea_etf_recovery_yields_to_hybrids_own_ledger():
    """The documented 132030 misattribution: both drop, KEM must not take it."""
    kem = {"id": "korea_etf", "holdings": [],
           "extra": {"target_ticker": "132030"}}
    hybrid = _hybrid([], open_kr={"132030": {"shares": 406, "entry_price": 24350.0}})
    api = {"strategies": [kem, hybrid]}

    assert G.recover_missing_bot_holdings(api, _totals()) == 0
    assert kem["holdings"] == []
