"""Holdings show the company, not the code.

The bots' result files store tickers only, so the cards rendered raw codes —
069500, 132030, 364690, GLTR. Nobody reads 364690 and thinks "KODEX Innovation
Tech Active". The two sleeves that already showed names (NMF2 and hands-on, fed
by the Toss snapshot) are what made the difference obvious.

KIS returns a display name with every holding and query_account_total keeps it,
so the labels come from the BROKER rather than a hand-kept table: they cannot
drift from the account, and a newly bought ticker is named the day it appears.

Run: python3 -m pytest tests/test_holding_names.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

TOTALS = {
    "kr": {"holdings": [
        {"ticker": "069500", "name": "KODEX 200"},
        {"ticker": "132030", "name": "KODEX 골드선물(H)"},
    ]},
    "us": {"holdings": [
        {"ticker": "SPY", "name": "SPDR S&P 500"},
        {"ticker": "NVDA", "name": "엔비디아"},
        {"ticker": "GLTR", "name": "ABERDEEN STANDARD PHYSICAL PRECIOUS METALS"},
    ]},
}


def test_map_is_built_from_broker_labels():
    m = G.account_name_map(TOTALS)
    assert m["069500"] == "KODEX 200"
    assert m["SPY"] == "SPDR S&P 500"
    assert len(m) == 5


def test_a_label_equal_to_the_ticker_is_not_a_name():
    """KIS falls back to the code when it has no name; that is not a label."""
    m = G.account_name_map({"us": {"holdings": [{"ticker": "XYZ", "name": "XYZ"}]}})
    assert "XYZ" not in m


def test_holdings_get_named():
    strategies = [{"id": "quant40", "holdings": [{"ticker": "SPY", "quantity": 18}]}]
    G.annotate_holding_names(strategies, G.account_name_map(TOTALS))
    assert strategies[0]["holdings"][0]["name"] == "SPDR S&P 500"


def test_both_legs_of_a_two_market_bot_get_named():
    strategies = [{"id": "hybrid_vb",
                   "kr": {"holdings": [{"ticker": "132030"}]},
                   "us": {"holdings": [{"ticker": "GLTR"}]}}]
    G.annotate_holding_names(strategies, G.account_name_map(TOTALS))
    assert strategies[0]["kr"]["holdings"][0]["name"] == "KODEX 골드선물(H)"
    assert strategies[0]["us"]["holdings"][0]["name"].startswith("ABERDEEN")


def test_open_positions_get_named_inside_the_value():
    """They are keyed BY ticker, so the row renderer can only reach a name
    stored inside the position dict."""
    strategies = [{"id": "hybrid_vb", "open_positions": {
        "kr": {"132030": {"shares": 406}},
        "us": {"GLTR": {"shares": 1}},
    }}]
    G.annotate_holding_names(strategies, G.account_name_map(TOTALS))
    assert strategies[0]["open_positions"]["kr"]["132030"]["name"] == "KODEX 골드선물(H)"
    assert strategies[0]["open_positions"]["us"]["GLTR"]["name"].startswith("ABERDEEN")


def test_existing_names_are_never_overwritten():
    """Toss-sourced sleeves carry Korean names the KIS account does not have."""
    strategies = [{"id": "nmf2", "holdings": [
        {"ticker": "069500", "name": "내가 붙인 이름"}]}]
    G.annotate_holding_names(strategies, G.account_name_map(TOTALS))
    assert strategies[0]["holdings"][0]["name"] == "내가 붙인 이름"


def test_an_unlabelled_ticker_keeps_its_code():
    """144600 is held by a bot but not by the account — no label exists.
    Falling back to the code is honest; inventing one would not be."""
    strategies = [{"id": "hybrid_vb", "holdings": [{"ticker": "144600"}]}]
    G.annotate_holding_names(strategies, G.account_name_map(TOTALS))
    assert "name" not in strategies[0]["holdings"][0]


def test_empty_map_is_a_no_op_and_never_raises():
    strategies = [{"id": "x", "holdings": [{"ticker": "SPY"}]}]
    G.annotate_holding_names(strategies, {})
    G.annotate_holding_names(None, {"SPY": "n"})
    assert "name" not in strategies[0]["holdings"][0]
