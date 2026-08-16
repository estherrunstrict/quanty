"""Tests for the Hands-on / Manual sleeve and the accounts/totals contract
(Tasks 3 and 5 of the whole-investment dashboard plan).

The sleeve answers "which of Jae's broker positions belong to no bot?", so every
test here is really a test of a subtraction that, if it is wrong, either hides
his own money or credits a bot's position to him. All fixtures are synthetic;
the SHAPES come from the live 2026-08-16 payloads (KIS `get_account_totals`,
`/api/data` strategy cards, and the Mac-side Toss snapshot).
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


def kis_totals(kr=(), us=()):
    """A get_account_totals()-shaped payload with only what the sleeve reads."""
    return {
        "kr": {"holdings": list(kr), "stock_value": sum(h["value"] for h in kr)},
        "us": {"holdings": list(us), "stock_value": sum(h["value"] for h in us)},
        "krw_cash": 0.0, "usd_cash": 0.0, "unsettled_us_sell_krw": 0.0,
    }


def kr_row(ticker, qty, value, profit=0.0, name=None):
    return {"ticker": ticker, "name": name or ticker, "quantity": qty,
            "value": value, "profit": profit}


def bot(sid, holdings=None, **kw):
    card = {"id": sid, "name": sid, "currency": "USD", "value": 0.0,
            "holdings": list(holdings or [])}
    card.update(kw)
    return card


def held(ticker, qty):
    return {"ticker": ticker, "quantity": qty}


# ── (1) a bot-owned position subtracts completely ──────────────────────────── #

def test_fully_bot_owned_position_leaves_no_manual_row(gen):
    holdings = gen.kis_holdings_map(kis_totals(us=[kr_row("SPY", 7, 5434.38)]))
    sleeve = gen.build_manual_sleeve(holdings, [bot("quant40", [held("SPY", 7)])], FX)
    assert sleeve["holdings"] == []
    assert sleeve["value"] == 0.0
    assert sleeve["id"] == "manual" and sleeve["name"] == "Hands-on / Manual"


# ── (2) partial overlap — account 100, bots 60 -> manual 40 ─────────────────── #

def test_partial_overlap_keeps_only_the_unclaimed_shares(gen):
    holdings = gen.kis_holdings_map(kis_totals(us=[kr_row("NVDA", 100, 22000.0, profit=1000.0)]))
    sleeve = gen.build_manual_sleeve(holdings, [bot("jd_strategy", [held("NVDA", 60)])], FX)
    (row,) = sleeve["holdings"]
    assert row["ticker"] == "NVDA"
    assert row["qty"] == 40
    assert row["bot_claimed_qty"] == 60
    assert row["value"] == pytest.approx(8800.0)          # 40 x 220 (pro-rated mark)
    assert row["value_krw"] == pytest.approx(8800.0 * FX)
    assert row["profit"] == pytest.approx(400.0)          # profit pro-rates too
    assert sleeve["value"] == pytest.approx(8800.0 * FX)


def test_two_bots_claiming_the_same_ticker_are_capped_at_the_account(gen):
    """korea_etf's account-recovered 132030 and hybrid_vb's live open position in
    the SAME 51 shares must not subtract 102 shares and invent a negative sleeve."""
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("132030", 51, 1241085.0)]))
    strategies = [
        bot("korea_etf", [held("132030", 51)], currency="KRW"),
        bot("hybrid_vb", [], currency="MULTI",
            open_positions={"kr": {"132030": {"shares": 51, "entry_price": 24760.0}}}),
    ]
    sleeve = gen.build_manual_sleeve(holdings, strategies, FX)
    assert sleeve["holdings"] == []
    assert sleeve["value"] == 0.0


# ── (3) zero / negative diffs are excluded ─────────────────────────────────── #

def test_bot_claiming_more_than_the_account_holds_never_goes_negative(gen):
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("069500", 13, 1430780.0)]))
    sleeve = gen.build_manual_sleeve(holdings, [bot("ghost", [held("069500", 99)], currency="KRW")], FX)
    assert sleeve["holdings"] == []
    assert sleeve["value"] == 0.0


def test_zero_quantity_account_rows_are_dropped(gen):
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("005930", 0, 0.0)]))
    assert holdings == {}
    assert gen.build_manual_sleeve(holdings, [], FX)["holdings"] == []


# ── (4) overseas positions convert at the dashboard FX rate ────────────────── #

def test_overseas_ticker_converts_at_fx_and_krw_passes_through(gen):
    holdings = gen.kis_holdings_map(kis_totals(
        kr=[kr_row("305720", 97, 1440450.0, name="KODEX 2차전지")],
        us=[kr_row("AAPL", 2, 611.86)],
    ))
    sleeve = gen.build_manual_sleeve(holdings, [], FX)
    by_ticker = {r["ticker"]: r for r in sleeve["holdings"]}
    assert by_ticker["305720"]["value_krw"] == pytest.approx(1440450.0)   # KRW passthrough
    assert by_ticker["305720"]["name"] == "KODEX 2차전지"
    assert by_ticker["AAPL"]["value_krw"] == pytest.approx(611.86 * FX)   # USD x rate
    assert sleeve["value"] == pytest.approx(1440450.0 + 611.86 * FX)
    assert sleeve["holdings"][0]["value_krw"] >= sleeve["holdings"][-1]["value_krw"]  # sorted desc


# ── (5) empty account -> empty sleeve, no crash ────────────────────────────── #

def test_empty_account_yields_an_empty_sleeve(gen):
    for empty in ({}, None):
        sleeve = gen.build_manual_sleeve(empty, [bot("quant40", [held("SPY", 7)])], FX)
        assert sleeve["holdings"] == []
        assert sleeve["value"] == 0.0
        assert sleeve["extra"]["ticker_count"] == 0


def test_missing_totals_payload_yields_an_empty_holdings_map(gen):
    assert gen.kis_holdings_map(None) == {}
    assert gen.kis_holdings_map({}) == {}
    assert gen.kis_holdings_map({"kr": None, "us": {"holdings": None}}) == {}


# ── bot claims: the dropout that would otherwise be reported as Jae's money ── #

def test_open_positions_count_as_a_bot_claim_when_the_card_dropped_its_holdings(gen):
    """Live 2026-08-16 shape: hybrid_vb's KR leg reported NO holdings while its
    published open_positions still held 091170/144600 worth ~W8.1M. Without the
    open_positions source that money is presented to Jae as hands-on."""
    holdings = gen.kis_holdings_map(kis_totals(kr=[
        kr_row("091170", 468, 7567560.0), kr_row("144600", 52, 544960.0),
        kr_row("364690", 45, 1800000.0),
    ]))
    hybrid = bot("hybrid_vb", [], currency="MULTI",
                 kr={"holdings": [], "value": 0},
                 open_positions={"kr": {"091170": {"shares": 468}, "144600": {"shares": 52}}})
    sleeve = gen.build_manual_sleeve(holdings, [hybrid], FX)
    assert [r["ticker"] for r in sleeve["holdings"]] == ["364690"]
    assert sleeve["value"] == pytest.approx(1800000.0)


def test_overlapping_claim_sources_in_one_card_are_maxed_not_summed(gen):
    """hybrid_vb publishes its US leg twice (top-level `holdings` AND `us`) and a
    third time as open_positions. Summing them would subtract 195 of 65 shares."""
    claims = gen.collect_bot_claims([bot(
        "hybrid_vb", [held("DBA", 65)], currency="MULTI",
        us={"holdings": [held("DBA", 65)]},
        open_positions={"us": {"DBA": {"shares": 65}}},
    )])
    assert claims == {"DBA": 65}


def test_manual_card_is_never_its_own_claimant(gen):
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("069500", 13, 1430780.0)]))
    first = gen.build_manual_sleeve(holdings, [], FX)
    again = gen.build_manual_sleeve(holdings, [first], FX)      # re-run over its own output
    assert again["value"] == first["value"]


def test_manual_card_reports_no_pnl_keys(gen):
    """The hero and get_portfolio() both sum unrealized/realized across
    strategies; the Toss half has no cost basis, so a half-covered P/L must not
    leak into the headline number."""
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("069500", 13, 1430780.0, profit=-272870.0)]))
    sleeve = gen.build_manual_sleeve(holdings, [], FX)
    assert "unrealized_profit" not in sleeve
    assert "realized_profit_ytd" not in sleeve
    assert sleeve["extra"]["kis_unrealized_krw"] == pytest.approx(-272870.0)


# ── Toss rows join the sleeve whole (no bot card represents that account) ──── #

def toss_row(symbol, qty, value_krw, currency="KRW", value_native=None, name=None):
    return {"symbol": symbol, "name": name or symbol, "qty": qty, "currency": currency,
            "value_native": value_native if value_native is not None else value_krw,
            "value_krw": value_krw}


def test_toss_holdings_are_added_whole_and_broken_out(gen):
    holdings = gen.kis_holdings_map(kis_totals(kr=[kr_row("069500", 13, 1430780.0)]))
    sleeve = gen.build_manual_sleeve(holdings, [], FX, toss_holdings=[
        toss_row("039240", 16, 31072.0),
        toss_row("NKE", 2.5, 250000.0, currency="USD", value_native=178.5),
    ])
    expected_toss = 31072.0 + 178.5 * FX
    assert sleeve["extra"]["kis_krw"] == pytest.approx(1430780.0)
    assert sleeve["extra"]["toss_krw"] == pytest.approx(expected_toss)
    assert sleeve["extra"]["toss_ticker_count"] == 2
    assert sleeve["value"] == pytest.approx(1430780.0 + expected_toss)
    assert {r["source"] for r in sleeve["holdings"]} == {"kis", "toss"}


def test_toss_usd_rows_are_repriced_at_the_dashboard_fx_not_the_snapshots(gen):
    """The snapshot converts with its own yfinance rate; accounts.toss.total_krw
    is re-priced at the KIS rate. If the sleeve kept the snapshot's KRW the two
    disagree and open a reconciliation gap the size of the FX drift."""
    stale_rate_krw = 178.5 * 1412.0
    sleeve = gen.build_manual_sleeve({}, [], FX, toss_holdings=[
        toss_row("NKE", 2.5, stale_rate_krw, currency="USD", value_native=178.5)])
    assert sleeve["extra"]["toss_krw"] == pytest.approx(178.5 * FX)
    assert sleeve["extra"]["toss_krw"] != pytest.approx(stale_rate_krw)


def test_toss_rows_with_no_value_or_quantity_are_skipped(gen):
    sleeve = gen.build_manual_sleeve({}, [], FX, toss_holdings=[
        toss_row("DEAD", 0, 0.0), toss_row("ZERO", 5, 0.0), "not-a-dict",
    ])
    assert sleeve["holdings"] == []
    assert sleeve["extra"]["toss_krw"] == 0.0


# ── accounts / totals contract (Task 5) ────────────────────────────────────── #

class _Now:
    def strftime(self, _fmt):
        return "2026-08-16 18:30 KST"


def fresh_toss(total=192507276.61, cash=5379027.4):
    return {"total_krw": total, "cash_krw": cash, "holdings_krw": total - cash,
            "as_of": "2026-08-16T18:21:53+09:00", "stale": False}


def stale_toss():
    return {"total_krw": 0.0, "cash_krw": 0.0, "holdings_krw": 0.0,
            "as_of": None, "stale": True, "note": "no snapshot"}


def test_accounts_emits_all_three_sleeves_with_kis_settlement_in_flight(gen):
    totals = kis_totals(kr=[kr_row("069500", 13, 1430780.0)], us=[kr_row("SPY", 7, 5434.38)])
    totals.update({"krw_cash": 10754883.0, "usd_cash": 13526.01, "unsettled_us_sell_krw": 1638128.0})
    accounts = gen.build_accounts(totals, {"exchange_rate": FX, "upbit_krw": 9737967.2},
                                  fresh_toss(), [bot("btc_vb", currency="KRW", is_holding=False)],
                                  _Now())
    assert set(accounts) == {"kis", "upbit", "toss"}
    expected_cash = 10754883.0 + 13526.01 * FX + 1638128.0
    assert accounts["kis"]["cash_krw"] == pytest.approx(expected_cash)
    assert accounts["kis"]["total_krw"] == pytest.approx(1430780.0 + 5434.38 * FX + expected_cash)
    assert accounts["kis"]["stale"] is False
    # BTC bot flat -> its Upbit sleeve is idle cash, not deployed bot capital.
    assert accounts["upbit"]["cash_krw"] == pytest.approx(9737967.2)
    assert accounts["upbit"]["position_krw"] == 0.0


def test_accounts_never_drops_kis_when_the_query_failed(gen):
    accounts = gen.build_accounts(None, {}, stale_toss(), [], _Now())
    assert accounts["kis"]["total_krw"] == 0.0
    assert accounts["kis"]["stale"] is True
    assert accounts["kis"]["as_of"] is None
    assert accounts["toss"]["stale"] is True


def test_upbit_position_counts_as_bots_when_the_bot_is_holding(gen):
    accounts = gen.build_accounts(None, {"upbit_krw": 5000000.0}, stale_toss(),
                                  [bot("btc_vb", currency="KRW", is_holding=True)], _Now())
    assert accounts["upbit"]["position_krw"] == pytest.approx(5000000.0)
    assert accounts["upbit"]["cash_krw"] == 0.0


def test_totals_partition_reconciles_exactly(gen):
    """bots + manual + cash must equal KIS + Upbit + Toss by construction —
    a non-zero gap means an input went missing, which is what the warning is for."""
    totals = kis_totals(kr=[kr_row("069500", 13, 1430780.0), kr_row("132030", 51, 1241085.0)],
                        us=[kr_row("SPY", 7, 5434.38), kr_row("NVDA", 22, 4953.52)])
    totals.update({"krw_cash": 10754883.0, "usd_cash": 13526.01, "unsettled_us_sell_krw": 1638128.0})
    strategies = [
        bot("btc_vb", currency="KRW", value=9737967.2, is_holding=False),
        bot("korea_etf", [held("132030", 51)], currency="KRW", value=1241085.0),
        bot("quant40", [held("SPY", 7)], value=5434.38),
        bot("jd_strategy", [held("NVDA", 16)], value=3602.56),
    ]
    holdings = gen.kis_holdings_map(totals)
    manual = gen.build_manual_sleeve(holdings, strategies, FX, toss_holdings=[
        toss_row("039240", 16, 187128249.21)])
    accounts = gen.build_accounts(totals, {"exchange_rate": FX, "upbit_krw": 9737967.2},
                                  fresh_toss(), strategies, _Now())
    t = gen.build_totals(accounts, strategies, manual, holdings, FX)

    assert t["investments_krw"] == pytest.approx(
        accounts["kis"]["total_krw"] + accounts["upbit"]["total_krw"] + accounts["toss"]["total_krw"])
    assert t["bots_krw"] + t["manual_krw"] + t["cash_krw"] == pytest.approx(t["investments_krw"], abs=1.0)
    assert t["reconciliation_gap_krw"] == pytest.approx(0.0, abs=1.0)
    assert t["reconciliation_warning"] is False
    assert sum(t["split_pct"].values()) == pytest.approx(100.0, abs=0.05)
    # Unclaimed 069500 + 6 unclaimed NVDA + the whole Toss account are hands-on.
    assert t["manual_krw"] == pytest.approx(
        1430780.0 + (4953.52 * 6 / 22) * FX + 187128249.21)
    assert t["manual_breakdown"]["toss_krw"] == pytest.approx(187128249.21)


def test_totals_flag_the_gap_when_an_account_total_is_missing(gen):
    """KIS totals unavailable but the cards still report positions: the money
    stops adding up, and the hero must say so rather than print a smaller total."""
    strategies = [bot("quant40", [held("SPY", 7)], value=5434.38)]
    accounts = {"kis": {"total_krw": 0.0, "cash_krw": 0.0, "stale": True},
                "upbit": {"total_krw": 50000000.0, "cash_krw": 0.0, "position_krw": 0.0},
                "toss": stale_toss()}
    t = gen.build_totals(accounts, strategies, {"value": 0.0}, {}, FX)
    assert t["reconciliation_gap_krw"] == pytest.approx(50000000.0)
    assert t["reconciliation_warning"] is True


def test_card_attribution_gap_surfaces_a_bot_card_that_lost_its_holdings(gen):
    totals = kis_totals(kr=[kr_row("091170", 468, 7567560.0)])
    hybrid = bot("hybrid_vb", [], currency="MULTI", kr={"holdings": [], "value": 0},
                 us={"holdings": [], "value": 0},
                 open_positions={"kr": {"091170": {"shares": 468}}})
    holdings = gen.kis_holdings_map(totals)
    manual = gen.build_manual_sleeve(holdings, [hybrid], FX)
    accounts = gen.build_accounts(totals, {"exchange_rate": FX}, stale_toss(), [hybrid], _Now())
    t = gen.build_totals(accounts, [hybrid], manual, holdings, FX)
    assert manual["value"] == 0.0                              # not Jae's money
    assert t["bots_krw"] == pytest.approx(7567560.0)           # attributed to the bot
    assert t["bots_cards_krw"] == 0.0                          # but its card says nothing
    assert t["unreported_bot_positions_krw"] == pytest.approx(7567560.0)


def test_unreported_bot_positions_is_zero_when_every_card_reports(gen):
    totals = kis_totals(us=[kr_row("SPY", 7, 5434.38)])
    strategies = [bot("quant40", [held("SPY", 7)], value=5434.38)]
    holdings = gen.kis_holdings_map(totals)
    manual = gen.build_manual_sleeve(holdings, strategies, FX)
    accounts = gen.build_accounts(totals, {"exchange_rate": FX}, stale_toss(), strategies, _Now())
    t = gen.build_totals(accounts, strategies, manual, holdings, FX)
    assert t["unreported_bot_positions_krw"] == 0.0


# ── manual-sleeve history persistence ──────────────────────────────────────── #

def test_history_upserts_one_row_per_date_atomically(gen, tmp_path):
    path = str(tmp_path / "manual_sleeve_history.jsonl")
    gen.append_manual_history({"date": "2026-08-15", "value_krw": 100.0}, path)
    gen.append_manual_history({"date": "2026-08-16", "value_krw": 200.0}, path)
    gen.append_manual_history({"date": "2026-08-16", "value_krw": 250.0}, path)   # same-day republish
    rows = gen.load_manual_history(path)
    assert [(r["date"], r["value_krw"]) for r in rows] == [("2026-08-15", 100.0), ("2026-08-16", 250.0)]
    assert not (tmp_path / "manual_sleeve_history.jsonl.tmp").exists()


def test_history_survives_a_corrupt_line_and_a_missing_file(gen, tmp_path):
    assert gen.load_manual_history(str(tmp_path / "nope.jsonl")) == []
    path = tmp_path / "h.jsonl"
    path.write_text('{"date": "2026-08-14", "value_krw": 1.0}\n{ truncated\n\n'
                    '{"date": "2026-08-15", "value_krw": 2.0}\n')
    assert [r["date"] for r in gen.load_manual_history(str(path))] == ["2026-08-14", "2026-08-15"]


def test_history_creates_the_directory_when_missing(gen, tmp_path):
    path = str(tmp_path / "strategy_results" / "manual_sleeve_history.jsonl")
    gen.append_manual_history({"date": "2026-08-16", "value_krw": 1.0}, path)
    assert json.loads(Path(path).read_text().strip())["value_krw"] == 1.0
