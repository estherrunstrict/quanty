"""Hands-on / Manual = Toss stock minus NMF2. KIS never contributes.

Jae buys by hand in Toss; KIS is the bots' account. The sleeve used to be
derived from "KIS shares no bot mentioned", which made it hostage to every bot
reporting perfectly — and it misfiled real bot positions as his own twice:

  2026-08-17  SPY 6 + NVDA 19   result file written 1s after the fill
  2026-08-19  069500/305720/364690  hybrid_vb_kr published holdings:[]

Run: python3 -m pytest tests/test_manual_is_toss_only.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

FX = 1400.0


def _kis(**rows):
    """{ticker: {qty, value_native, currency, name, profit}}"""
    out = {}
    for t, (qty, val, cur) in rows.items():
        out[t] = {"qty": qty, "value_native": val, "currency": cur,
                  "name": t, "profit": 0.0}
    return out


def _toss(ticker, qty, krw):
    return {"ticker": ticker, "symbol": ticker, "name": ticker, "qty": qty,
            "currency": "KRW", "value_native": krw, "value_krw": krw,
            "purchaseAmount": krw}


def test_unclaimed_kis_stock_is_not_his_money():
    """hybrid_vb_kr dropped its holdings; those KODEX shares are still the bot's."""
    kis = _kis(**{"364690": (45, 1_665_675, "KRW"),
                  "305720": (97, 1_376_915, "KRW"),
                  "069500": (13, 1_322_880, "KRW")})
    strategies = [{"id": "hybrid_vb", "currency": "MULTI",
                   "kr": {"holdings": []}, "us": {"holdings": []}}]

    card = G.build_manual_sleeve(kis, strategies, FX, toss_holdings=[])

    assert card["value"] == 0
    assert card["extra"]["kis_krw"] == 0
    assert [r for r in card["holdings"] if r.get("source") == "kis"] == []
    # ...but it is recorded, so the bot-ledger problem stays visible.
    assert card["extra"]["kis_unclaimed_krw"] > 4_300_000
    assert set(card["extra"]["kis_unclaimed"]) == {"364690", "305720", "069500"}


def test_partially_reported_kis_ticker_is_not_his_money():
    """The 08-17 shape: bot says 7 SPY, account holds 13."""
    kis = _kis(SPY=(13, 10_012.66, "USD"))
    strategies = [{"id": "quant40", "holdings": [{"ticker": "SPY", "quantity": 7}]}]

    card = G.build_manual_sleeve(kis, strategies, FX, toss_holdings=[])

    assert card["value"] == 0
    assert card["extra"]["kis_unclaimed"]["SPY"] == 6.0


def test_toss_stock_is_his_minus_the_nmf2_ledger():
    kis = _kis()
    rows = [_toss("GOOGL", 246, 118_751_905), _toss("069500", 100, 10_000_000)]
    # NMF2 owns 60 of the 100 069500 shares; the rest is his.
    card = G.build_manual_sleeve(kis, [], FX, toss_holdings=rows,
                                 toss_claims={"069500": 60})

    assert card["extra"]["kis_krw"] == 0
    assert abs(card["extra"]["toss_krw"] - (118_751_905 + 4_000_000)) < 1.0
    assert abs(card["value"] - (118_751_905 + 4_000_000)) < 1.0
    tickers = {r["ticker"] for r in card["holdings"]}
    assert tickers == {"GOOGL", "069500"}


def test_a_fully_nmf2_owned_toss_row_leaves_the_sleeve_entirely():
    kis = _kis()
    rows = [_toss("005180", 134, 10_572_600)]
    card = G.build_manual_sleeve(kis, [], FX, toss_holdings=rows,
                                 toss_claims={"005180": 134})
    assert card["value"] == 0
    assert card["holdings"] == []
