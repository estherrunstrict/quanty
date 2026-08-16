"""Tests for the NMF2 bot card and its carve-out from the hands-on sleeve.

NMF2 (신마법공식 2.0 + 계절성) trades a real-money ~W1M slice of the Toss account
from Jae's Mac. Its ledger is the only record of which Toss shares are the bot's;
before this card existed all 37 Toss positions were reported to Jae as his own
hands-on money, including NMF2's 30.

Every test here guards one of two invariants, because getting either wrong
misreports real money:

  1. **Carve-out, not copy.** A share claimed by NMF2 leaves the hands-on sleeve
     in the same pass. `bots_krw` rises by exactly what `manual_krw` loses, and
     `totals.reconciliation_gap_krw` stays at zero.
  2. **Graceful degradation.** The ledger is the live bot's own state file, read
     strictly read-only and liable to be missing, rotating, or truncated at any
     moment. Any failure must fall back to the pre-card behaviour (no nmf2 card,
     every Toss share hands-on) — never a crash, never a moved gap.

Fixtures are synthetic; the SHAPES come from the live 2026-08-16 payloads
(`toss-nmf2-bot/state/ledger.json` and `strategy_results/toss_snapshot.json`).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_generator():
    """Import quanty-dashboard/generate_dashboard_data.py, or skip.

    quanty-dashboard is a sibling scratch directory, not part of this repo and
    not a git repo itself (deployed by scp). Walk up until we find it so the
    test works from a checkout, a worktree, or the server.
    """
    for parent in [REPO_ROOT] + list(REPO_ROOT.parents):
        cand = parent / "quanty-dashboard" / "generate_dashboard_data.py"
        if cand.exists():
            sys.path.insert(0, str(cand.parent))
            sys.path.insert(0, str(REPO_ROOT))       # dashboard_equity lives here
            spec = importlib.util.spec_from_file_location("generate_dashboard_data", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("quanty-dashboard/generate_dashboard_data.py not found next to this repo")


@pytest.fixture(scope="module")
def gen():
    return _load_generator()


FX = 1400.0


# --------------------------------------------------------------------------- #
# fixtures shaped like the live payloads
# --------------------------------------------------------------------------- #
def ledger(positions, budget_krw=1000000, cash_krw=134981, **extra):
    led = {"budget_krw": budget_krw, "cash_krw": cash_krw,
           "positions": dict(positions), "created": "2026-07-27T09:05:03",
           "updated": "2026-08-16T18:10:25"}
    led.update(extra)
    return led


def pos(qty, avg_price):
    return {"qty": qty, "avg_price": avg_price}


def toss_row(symbol, qty, value_krw, currency="KRW", value_native=None, name=None):
    return {"symbol": symbol, "name": name or symbol, "qty": qty, "currency": currency,
            "value_native": value_native if value_native is not None else value_krw,
            "value_krw": value_krw, "market_country": "KR"}


def bot(sid, holdings=None, currency="USD", value=0.0, **extra):
    card = {"id": sid, "name": sid, "currency": currency, "value": value,
            "holdings": list(holdings or [])}
    card.update(extra)
    return card


def held(ticker, qty):
    return {"ticker": ticker, "quantity": qty}


# --------------------------------------------------------------------------- #
# the card itself
# --------------------------------------------------------------------------- #
def test_card_marks_ledger_positions_with_the_accounts_own_prices(gen):
    """The ledger knows the shares and the cost; only the snapshot knows today's
    price. The card is the join, and its P/L is mark minus cost."""
    card = gen.build_nmf2_card(
        ledger({"089230": pos(24, 1362.0), "370090": pos(5, 6260.0)}),
        [toss_row("089230", 24, 34560.0), toss_row("370090", 5, 30000.0)], FX)

    assert card["id"] == "nmf2"
    assert card["name"] == "NMF2 (신마법공식 2.0)"
    assert card["currency"] == "KRW"
    assert card["account"] == "toss"
    assert card["value"] == pytest.approx(64560.0)
    assert card["cost_basis"] == pytest.approx(63988.0)      # 24x1362 + 5x6260
    assert card["unrealized_profit"] == pytest.approx(64560.0 - 63988.0)
    # No sells since inception, so Total P/L is the unrealized figure exactly.
    assert card["total_pl_ytd"] == pytest.approx(card["unrealized_profit"])
    assert card["budget"] == pytest.approx(1000000.0)
    assert {r["ticker"] for r in card["holdings"]} == {"089230", "370090"}


def test_holdings_are_sorted_by_value_and_carry_per_row_pnl(gen):
    card = gen.build_nmf2_card(
        ledger({"AAA": pos(10, 100.0), "BBB": pos(10, 100.0)}),
        [toss_row("AAA", 10, 900.0), toss_row("BBB", 10, 1500.0)], FX)
    assert [r["ticker"] for r in card["holdings"]] == ["BBB", "AAA"]
    rows = {r["ticker"]: r for r in card["holdings"]}
    assert rows["BBB"]["profit"] == pytest.approx(500.0)
    assert rows["BBB"]["profit_rate"] == pytest.approx(50.0)
    assert rows["AAA"]["profit"] == pytest.approx(-100.0)


def test_ledger_symbol_missing_from_the_snapshot_is_named_not_guessed(gen):
    """Cost basis is not market value. A symbol the account does not report gets
    zero — inventing qty x avg_price would add money to a total built from
    account rows and blow the reconciliation gap open."""
    card = gen.build_nmf2_card(
        ledger({"089230": pos(24, 1362.0), "GONE": pos(11, 5000.0)}),
        [toss_row("089230", 24, 34560.0)], FX)
    assert card["value"] == pytest.approx(34560.0)          # nothing added for GONE
    assert card["extra"]["unmatched_symbols"] == ["GONE"]
    assert card["extra"]["ticker_count"] == 1
    assert card["extra"]["ledger_position_count"] == 2


def test_bot_never_claims_more_shares_than_the_account_reports(gen):
    """A ledger that has drifted ahead of the account (a fill the broker has not
    settled) must not let the bot value shares that are not there."""
    card = gen.build_nmf2_card(ledger({"089230": pos(100, 1362.0)}),
                               [toss_row("089230", 24, 34560.0)], FX)
    assert card["holdings"][0]["qty"] == pytest.approx(24)
    assert card["holdings"][0]["ledger_qty"] == pytest.approx(100)
    assert card["value"] == pytest.approx(34560.0)


def test_partially_bot_owned_symbol_is_pro_rated(gen):
    """Jae holding the same name by hand splits the row: the bot's shares at the
    account's own mark, the rest hands-on."""
    card = gen.build_nmf2_card(ledger({"089230": pos(10, 1362.0)}),
                               [toss_row("089230", 25, 50000.0)], FX)
    assert card["value"] == pytest.approx(20000.0)          # 10/25 of W50,000


def test_usd_toss_row_is_repriced_at_the_dashboard_fx(gen):
    card = gen.build_nmf2_card(ledger({"NKE": pos(2, 70.0)}),
                               [toss_row("NKE", 2, 2_000_000.0, currency="USD",
                                         value_native=140.0)], FX)
    assert card["value"] == pytest.approx(140.0 * FX)


def test_ledger_cash_is_reported_but_not_added_to_value(gen):
    """Toss cash is already whole inside totals.cash_krw. Adding the ledger's
    cash to the card would book the same won twice."""
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0)}, cash_krw=134981),
                               [toss_row("089230", 24, 34560.0)], FX)
    assert card["value"] == pytest.approx(34560.0)
    assert card["extra"]["cash_krw"] == pytest.approx(134981.0)


# --------------------------------------------------------------------------- #
# carve-out: what the bot claims, the hands-on sleeve gives up
# --------------------------------------------------------------------------- #
def test_nmf2_symbols_leave_the_manual_sleeve(gen):
    toss = [toss_row("089230", 24, 34560.0), toss_row("370090", 5, 30000.0),
            toss_row("005180", 3, 90000.0)]                 # 005180: Jae's own
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0), "370090": pos(5, 6260.0)}),
                               toss, FX)
    claims = {r["ticker"]: r["qty"] for r in card["holdings"]}

    before = gen.build_manual_sleeve({}, [], FX, toss_holdings=toss)
    after = gen.build_manual_sleeve({}, [], FX, toss_holdings=toss, toss_claims=claims)

    assert before["extra"]["toss_krw"] == pytest.approx(154560.0)
    assert after["extra"]["toss_krw"] == pytest.approx(90000.0)
    assert before["value"] - after["value"] == pytest.approx(card["value"])
    assert [r["ticker"] for r in after["holdings"]] == ["005180"]
    assert after["extra"]["toss_bot_claimed_krw"] == pytest.approx(card["value"])


def test_partial_claim_leaves_the_remainder_hands_on(gen):
    toss = [toss_row("089230", 25, 50000.0)]
    card = gen.build_nmf2_card(ledger({"089230": pos(10, 1362.0)}), toss, FX)
    sleeve = gen.build_manual_sleeve({}, [], FX, toss_holdings=toss,
                                     toss_claims={"089230": 10})
    assert sleeve["holdings"][0]["qty"] == pytest.approx(15)
    assert sleeve["holdings"][0]["bot_claimed_qty"] == pytest.approx(10)
    assert sleeve["extra"]["toss_krw"] == pytest.approx(30000.0)
    assert sleeve["extra"]["toss_krw"] + card["value"] == pytest.approx(50000.0)


def test_manual_caveat_no_longer_claims_to_contain_nmf2(gen):
    sleeve = gen.build_manual_sleeve({}, [], FX, toss_holdings=[toss_row("005180", 3, 90000.0)])
    caveat = sleeve["extra"]["caveat"]
    assert "excluded" in caveat
    assert "no bot card" not in caveat


def test_a_toss_card_never_claims_kis_shares(gen):
    """Toss and KIS share Korea's 6-digit ticker space. If the nmf2 card were fed
    into the KIS attribution the same code would be credited twice — once out of
    Toss, once out of KIS — and the gap would open by the KIS row's value."""
    kis = gen.kis_holdings_map({
        "kr": {"holdings": [{"ticker": "089230", "quantity": 40, "value": 999999.0}]},
        "us": {"holdings": []}})
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0)}),
                               [toss_row("089230", 24, 34560.0)], FX)
    assert gen.collect_bot_claims([card]) == {}
    # ...so the KIS row stays entirely hands-on.
    sleeve = gen.build_manual_sleeve(kis, [card], FX)
    assert sleeve["extra"]["kis_krw"] == pytest.approx(999999.0)


# --------------------------------------------------------------------------- #
# reconciliation: the gap must not move
# --------------------------------------------------------------------------- #
class _Now:
    def strftime(self, _fmt):
        return "2026-08-16 18:30 KST"


def fresh_toss(total, cash):
    return {"total_krw": total, "cash_krw": cash, "holdings_krw": total - cash,
            "as_of": "2026-08-16T18:21:53+09:00", "stale": False}


def _scenario(gen, toss_claims, nmf2_card):
    """One publish, with and without the NMF2 carve-out, over identical inputs."""
    toss = [toss_row("089230", 24, 34560.0), toss_row("370090", 5, 30000.0),
            toss_row("005180", 3, 90000.0)]                 # 005180: Jae's own
    toss_cash = 5379088.88
    # stock_value must be present and consistent with the rows: build_accounts
    # totals the account from it, while the sleeves are built from the rows.
    kis = {"kr": {"holdings": [{"ticker": "069500", "quantity": 13, "value": 1430780.0}],
                  "stock_value": 1430780.0},
           "us": {"holdings": [{"ticker": "SPY", "quantity": 7, "value": 5434.38}],
                  "stock_value": 5434.38},
           "krw_cash": 10754883.0, "usd_cash": 13526.01, "unsettled_us_sell_krw": 1638128.0}
    strategies = [bot("quant40", [held("SPY", 7)], value=5434.38)]
    holdings = gen.kis_holdings_map(kis)
    manual = gen.build_manual_sleeve(holdings, strategies, FX, toss_holdings=toss,
                                     toss_claims=toss_claims)
    cards = strategies + ([nmf2_card] if nmf2_card else []) + [manual]
    accounts = gen.build_accounts(kis, {"exchange_rate": FX, "upbit_krw": 9737967.2},
                                  fresh_toss(sum(r["value_krw"] for r in toss) + toss_cash,
                                             toss_cash),
                                  cards, _Now())
    return gen.build_totals(accounts, cards, manual, holdings, FX)


def test_carving_nmf2_out_moves_money_between_sleeves_but_not_the_gap(gen):
    """The whole point of the change, stated as one assertion: bots gains exactly
    what hands-on loses, and investments/gap are untouched."""
    toss = [toss_row("089230", 24, 34560.0), toss_row("370090", 5, 30000.0),
            toss_row("005180", 3, 90000.0)]
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0), "370090": pos(5, 6260.0)}),
                               toss, FX)
    claims = {r["ticker"]: r["qty"] for r in card["holdings"]}

    before = _scenario(gen, None, None)
    after = _scenario(gen, claims, card)

    assert after["investments_krw"] == pytest.approx(before["investments_krw"])
    assert after["bots_krw"] - before["bots_krw"] == pytest.approx(card["value"])
    assert before["manual_krw"] - after["manual_krw"] == pytest.approx(card["value"])
    assert after["cash_krw"] == pytest.approx(before["cash_krw"])
    assert before["reconciliation_gap_krw"] == pytest.approx(0.0, abs=1.0)
    assert after["reconciliation_gap_krw"] == pytest.approx(0.0, abs=1.0)
    assert after["reconciliation_warning"] is False
    assert after["bots_breakdown"]["toss_krw"] == pytest.approx(card["value"])
    assert sum(after["split_pct"].values()) == pytest.approx(100.0, abs=0.05)


def test_totals_still_partition_investments_exactly(gen):
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0)}),
                               [toss_row("089230", 24, 34560.0)], FX)
    t = _scenario(gen, {"089230": 24}, card)
    assert t["bots_krw"] + t["manual_krw"] + t["cash_krw"] == pytest.approx(
        t["investments_krw"], abs=1.0)


# --------------------------------------------------------------------------- #
# graceful degradation — the ledger belongs to a live bot and may vanish
# --------------------------------------------------------------------------- #
def test_missing_ledger_file_reads_as_empty(gen, tmp_path):
    assert gen.load_nmf2_ledger(str(tmp_path / "nope.json")) == {}


def test_unreadable_ledger_reads_as_empty(gen, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text('{"budget_krw": 1000000, "positions": {"089230"')   # truncated mid-write
    assert gen.load_nmf2_ledger(str(path)) == {}


def test_ledger_with_the_wrong_shape_reads_as_empty(gen, tmp_path):
    for payload in ('[]', '"nope"', '{"budget_krw": 1000000}', '{"positions": []}'):
        path = tmp_path / "ledger.json"
        path.write_text(payload)
        assert gen.load_nmf2_ledger(str(path)) == {}, payload


def test_a_real_ledger_round_trips(gen, tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger({"089230": pos(24, 1362.0)})))
    led = gen.load_nmf2_ledger(str(path))
    assert led["budget_krw"] == 1000000
    assert led["positions"]["089230"]["qty"] == 24


def test_no_ledger_means_no_card_and_an_unchanged_manual_sleeve(gen):
    """Degradation target: exactly the pre-NMF2-card behaviour — every Toss share
    hands-on, no crash, no zero-value ghost card in the bot grid."""
    toss = [toss_row("089230", 24, 34560.0), toss_row("005180", 3, 90000.0)]
    for bad in ({}, {"positions": {}}, None):
        assert gen.build_nmf2_card(bad, toss, FX) is None
    sleeve = gen.build_manual_sleeve({}, [], FX, toss_holdings=toss, toss_claims={})
    assert sleeve["extra"]["toss_krw"] == pytest.approx(124560.0)
    assert sleeve["extra"]["toss_bot_claimed_krw"] == 0.0


def test_empty_toss_snapshot_yields_a_zero_card_that_claims_nothing(gen):
    """The Mac job is asleep: no prices exist, so the bot is worth 0 on paper and
    the sleeve keeps whatever the (also empty) snapshot gave it. A gap is
    impossible because both sides read the same empty list."""
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0)}), [], FX)
    assert card["value"] == 0.0
    assert card["holdings"] == []
    assert card["extra"]["unmatched_symbols"] == ["089230"]
    assert gen.build_manual_sleeve({}, [], FX, toss_holdings=[])["value"] == 0.0


def test_ledger_rows_with_no_quantity_or_a_bad_shape_are_skipped(gen):
    card = gen.build_nmf2_card(
        ledger({"089230": pos(24, 1362.0), "SOLD": pos(0, 900.0), "JUNK": "not-a-dict"}),
        [toss_row("089230", 24, 34560.0), toss_row("SOLD", 4, 1000.0)], FX)
    assert [r["ticker"] for r in card["holdings"]] == ["089230"]
    assert card["value"] == pytest.approx(34560.0)


def test_zero_budget_ledger_does_not_divide_by_zero(gen):
    card = gen.build_nmf2_card(ledger({"089230": pos(24, 1362.0)}, budget_krw=0),
                               [toss_row("089230", 24, 34560.0)], FX)
    assert card["extra"]["deployed_pct"] == 0.0
    assert card["budget"] == 0.0
