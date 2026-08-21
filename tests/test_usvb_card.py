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
