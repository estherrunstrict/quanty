"""The USVB paper card is watched but never counted.

Its spec passed every promotion gate on 2026-08-21 (docs/result-usvb-spec-review.md),
so the paper track needs to be VISIBLE — a paper track nobody can see is a paper
track nobody audits. But its equity exists in no real account, so the one thing
this card must never do is leak into the real-money arithmetic: account totals,
reconciliation, the equity-history aggregate.

Run: python3 -m pytest tests/test_usvb_card.py -q
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_dashboard_data as G  # noqa: E402

NOW = datetime(2026, 8, 21, 19, 30)

STATUS = {
    "updated_at": "2026-08-21T19:11:49", "mode": "paper",
    "start": "2026-08-08", "start_usd": 20000.0,
    "equity_usd": 19803.13, "sleeve_cash_usd": 6000.0,
    "core_usd": 13803.13, "core_units": 19.4156, "qqq_close": 710.93,
    "pending": [], "recent": [],
}


def _card(tmp_path, status=STATUS):
    p = tmp_path / "status.json"
    p.write_text(json.dumps(status))
    return G.build_usvb_card(path=str(p), now=NOW)


def test_card_reports_the_paper_track(tmp_path):
    c = _card(tmp_path)
    assert c["id"] == "usvb" and c["mode"] == "paper"
    assert c["paper_equity"] is True                 # the load-bearing field
    assert c["value"] == 19803.13
    assert abs(c["total_pl_ytd"] - (-196.87)) < 0.01
    assert abs(c["profit_rate_ytd_pct"] - (-0.98)) < 0.02
    assert c["holdings"][0]["ticker"] == "QQQ"
    assert abs(c["stale_hours"] - 0.3) < 0.05


def test_paper_equity_never_enters_the_real_money_totals(tmp_path):
    """The one invariant that matters: watched, not counted."""
    c = _card(tmp_path)
    accounts = {
        "kis": {"total_krw": 100e6, "cash_krw": 10e6, "kr_stock_krw": 20e6,
                "us_stock_krw": 70e6},
        "upbit": {"total_krw": 10e6, "cash_krw": 0, "position_krw": 10e6},
        "toss": {"total_krw": 50e6, "cash_krw": 5e6, "holdings_krw": 45e6},
        "cma": {"total_krw": 0, "cash_krw": 0},
    }
    manual = {"id": "manual", "value": 30e6, "extra": {}}
    base = G.build_totals(accounts, [manual], manual, {}, 1400.0)
    with_usvb = G.build_totals(accounts, [c, manual], manual, {}, 1400.0)

    for key in ("investments_krw", "bots_krw", "manual_krw", "cash_krw",
                "bots_cards_krw", "reconciliation_gap_krw"):
        assert with_usvb[key] == base[key], "%s moved by the paper card" % key


def test_paper_equity_is_kept_out_of_the_equity_history(tmp_path):
    """The comparison chart aggregates real P/L; simulated P/L must not join."""
    assert "usvb" in G.AGG_SKIP_IDS
    c = _card(tmp_path)
    live = {"id": "quant40", "currency": "USD",
            "realized_profit_ytd": 100.0, "unrealized_profit": 50.0}
    hist = tmp_path / "eq.jsonl"
    G.write_equity_snapshot([live, c], 1400.0, "2026-08-21", path=str(hist))
    rows = [json.loads(l) for l in hist.read_text().splitlines() if l.strip()]
    assert rows and "quant40" in rows[-1], "the live bot row must be written"
    assert "usvb" not in rows[-1], "paper P/L leaked into the aggregate history"


def test_card_survives_garbage(tmp_path):
    assert G.build_usvb_card(path=str(tmp_path / "nope.json")) is None
    p = tmp_path / "bad.json"
    p.write_text("{ not json")
    assert G.build_usvb_card(path=str(p)) is None
    p.write_text(json.dumps({"mode": "paper"}))      # no equity
    assert G.build_usvb_card(path=str(p)) is None


def test_usvb_has_fleet_identity():
    """Colour, chip, cadence — a first-class citizen, not a stray card."""
    assert G.BOT_COLORS["usvb"] == "#c754c7"
    assert G.BOT_CHIPS["usvb"] == "🟣"
    assert G.BOT_CADENCE_HOURS["usvb"] == 24


# ---------------------------------------------------------------------------
# LIVE ("testing") card — added 2026-08-21 when USVB started trading real money.
#
# The bug this pins shut: the card was hardcoded `mode: "paper"` and read the
# paper track's status.json. USVB went live and the paper crons were removed in
# the same change, so the dashboard showed a FROZEN simulated equity while real
# orders were filling — and the shares it bought were claimed by nobody, so the
# hands-on sleeve absorbed them as Jae's own buying.
# ---------------------------------------------------------------------------

FX = 1387.0
LIVE_LEDGER = {
    "mode": "testing", "budget_usd": 2000.0, "cash_usd": 1927.89,
    "updated": "2026-08-21T20:50:15",
    "positions": {"TQQQ": {"qty": 1, "avg_price": 72.04}},
}
TOSS_ROWS = [
    {"symbol": "TQQQ", "name": "프로셰어즈 울트라프로 QQQ", "qty": 1,
     "currency": "USD", "value_native": 73.50, "value_krw": 73.50 * FX},
    {"symbol": "005930", "name": "삼성전자", "qty": 10,
     "currency": "KRW", "value_native": 700000, "value_krw": 700000},
]


def test_live_ledger_produces_a_testing_card_not_a_paper_one():
    c = G.build_usvb_live_card(LIVE_LEDGER, TOSS_ROWS, FX)
    assert c["mode"] == "testing", "real money must not read as 'paper'"
    assert not c.get("paper_equity"), "this equity IS in a real account"
    assert c["account"] == "toss"
    assert c["currency"] == "KRW", "totals.toss_bots_krw sums `value` as KRW"
    assert c["value"] == round(73.50 * FX, 2)


def test_paper_ledger_is_not_mistaken_for_live():
    """No stamp -> no live card, so the paper path still runs."""
    assert G.build_usvb_live_card({"positions": {}}, TOSS_ROWS, FX) is None
    assert G.build_usvb_live_card({"mode": "paper", "positions": {}}, TOSS_ROWS, FX) is None
    assert G.build_usvb_live_card({}, TOSS_ROWS, FX) is None


def test_a_flat_book_still_renders_a_card():
    """Same-day strategy: flat is its NORMAL resting state. NMF2 returns None on an
    empty book; doing that here would delete the card from the grid every night."""
    led = dict(LIVE_LEDGER, positions={})
    c = G.build_usvb_live_card(led, TOSS_ROWS, FX)
    assert c is not None, "card must not vanish when the bot is flat"
    assert c["value"] == 0.0 and c["holdings"] == []
    assert "관망" in c["extra"]["status"]
    assert c["extra"]["budget_usd"] == 2000.0, "budget is still visible while flat"


def test_its_shares_leave_the_hands_on_sleeve():
    """The whole point of claiming: one share cannot be in two buckets."""
    c = G.build_usvb_live_card(LIVE_LEDGER, TOSS_ROWS, FX)
    claims = {r["ticker"]: r["qty"] for r in c["holdings"]}
    assert claims == {"TQQQ": 1}
    manual = G.build_manual_sleeve({}, [], FX, TOSS_ROWS, claims)
    assert "TQQQ" not in {h["ticker"] for h in manual["holdings"]}
    assert manual["value"] == 700000, "only the untouched Samsung row is hands-on"

    unclaimed = G.build_manual_sleeve({}, [], FX, TOSS_ROWS, {})
    assert "TQQQ" in {h["ticker"] for h in unclaimed["holdings"]}, "fixture sanity"


def test_never_claims_more_than_the_account_holds():
    led = dict(LIVE_LEDGER, positions={"TQQQ": {"qty": 99, "avg_price": 72.04}})
    c = G.build_usvb_live_card(led, TOSS_ROWS, FX)
    assert c["holdings"][0]["qty"] == 1, "account has 1 share; ledger claims 99"
    assert c["value"] == round(73.50 * FX, 2)


def test_a_symbol_missing_from_the_snapshot_is_named_not_valued():
    led = dict(LIVE_LEDGER, positions={"SQQQ": {"qty": 5, "avg_price": 20.0}})
    c = G.build_usvb_live_card(led, TOSS_ROWS, FX)
    assert c["value"] == 0.0, "cost basis is not market value"
    assert c["extra"]["unmatched_symbols"] == ["SQQQ"]


def test_live_card_counts_in_the_real_money_totals():
    """The mirror of test_paper_equity_never_enters_the_real_money_totals."""
    c = G.build_usvb_live_card(LIVE_LEDGER, TOSS_ROWS, FX)
    accounts = {"kis": {}, "upbit": {}, "toss": {"total_krw": 0.0, "cash_krw": 0.0}}
    t = G.build_totals(accounts, [c], None, {}, FX)
    assert t["bots_krw"] == c["value"], "a toss-account bot must reach bots_krw"
