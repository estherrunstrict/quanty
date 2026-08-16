#!/usr/bin/env python3
"""Generate dashboard_data.json from live dashboard_server API + KIS account query."""

import json
import os
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DASHBOARD_DIR, "docs", "data")
API_URL = "http://localhost:8077/api/data"
TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DEPOSITS_FILE = os.path.join(DASHBOARD_DIR, "deposits.json")

sys.path.insert(0, DASHBOARD_DIR)
sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm"))
sys.path.insert(0, TRADING_DIR)  # for dashboard_equity (shared with dashboard_server)
from dashboard_equity import et_today, pin_equity_endpoints

# Toss Securities snapshot: written on Jae's Mac by
# automation_oracle/scripts/toss_snapshot.py and scp'd here. Nothing on this
# server talks to Toss (the Open API IP allowlist covers the Mac only), so this
# file is the account's only representation and it can legitimately go stale.
TOSS_SNAPSHOT_FILE = os.path.join(TRADING_DIR, "strategy_results", "toss_snapshot.json")
TOSS_MAX_AGE_HOURS = 30

# NMF2 (신마법공식 2.0) sleeve — a real automated bot trading a ~W1M slice of the
# Toss account. Its ledger is the ONLY record of which Toss positions are the
# bot's; without it every Toss share looks hands-on. Read STRICTLY read-only:
# this file is the bot's own live state and the dashboard must never write it.
# We read it here (rather than shipping it in the Mac-side Toss snapshot)
# because the ledger already lives on THIS host next to the generator, so a
# direct read needs no new transport, no Mac job, and no snapshot schema bump.
NMF2_LEDGER_FILE = "/home/ubuntu/toss-nmf2-bot/toss_nmf2_bot/state/ledger.json"
# Hands-on / Manual and NMF2 sleeve history (reporting-side only — no bot reads
# these; they live beside the other reporting artefacts, never in the bot's dir).
MANUAL_HISTORY_FILE = os.path.join(TRADING_DIR, "strategy_results", "manual_sleeve_history.jsonl")
NMF2_HISTORY_FILE = os.path.join(TRADING_DIR, "strategy_results", "nmf2_history.jsonl")
# |investments - (bots + manual + cash)| above this share of investments flips
# totals.reconciliation_warning. The split is built by partitioning the account
# rows themselves, so a non-zero gap means an INPUT is missing (KIS query failed,
# a bot claims more shares than the account holds), not a rounding drift.
RECONCILIATION_WARN_PCT = 1.0


def load_toss_account(path=None, now=None, max_age_hours=TOSS_MAX_AGE_HOURS, fx_rate=None):
    """Read the Mac-side Toss snapshot into the `accounts.toss` contract.

    Returns ALWAYS — missing file, unreadable JSON, bad schema and stale data all
    resolve to a zeroed, honestly-flagged dict rather than an exception or a
    missing key. A publish must never fail because the Mac was asleep, and the
    hero must never quietly drop the Toss sleeve from the total: a zero with
    stale=true is a tile the front-end greys out, a missing key is a silent lie.

    `as_of` is deliberately None whenever stale=true (the healthcheck asserts
    "as_of fresher than max_age only when stale is false"); the snapshot's own
    timestamp is preserved under `last_seen_at` for the UI.

    When `fx_rate` is given and the snapshot carries native currency buckets, the
    USD sleeve is re-converted at the dashboard's own KIS rate so the Toss tile
    can't disagree with the KIS tile over FX. Otherwise the snapshot's own
    fallback rate stands.
    """
    def _empty(note, last_seen=None):
        return {"total_krw": 0.0, "cash_krw": 0.0, "holdings_krw": 0.0,
                "as_of": None, "stale": True, "note": note,
                "last_seen_at": last_seen}

    path = path or TOSS_SNAPSHOT_FILE
    if not os.path.exists(path):
        return _empty("no snapshot at {} (Mac job never ran?)".format(path))
    try:
        with open(path) as f:
            snap = json.load(f)
    except Exception as e:
        return _empty("unreadable snapshot: {}".format(e))
    if not isinstance(snap, dict):
        return _empty("snapshot is not an object")

    raw_as_of = snap.get("as_of")
    try:
        stamp = datetime.fromisoformat(str(raw_as_of).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _empty("unparseable as_of {!r}".format(raw_as_of))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=KST)          # snapshots are written in KST
    now = now or datetime.now(KST)
    age_h = (now - stamp).total_seconds() / 3600.0
    if age_h > max_age_hours or age_h < -1:        # future stamps are broken clocks
        return _empty("snapshot {:.1f}h old (limit {}h)".format(age_h, max_age_hours),
                      last_seen=raw_as_of)

    native = snap.get("native") or {}
    holdings_krw = float(snap.get("holdings_krw") or 0)
    cash_krw = float(snap.get("cash_krw") or 0)
    if fx_rate and fx_rate > 0 and native:
        # Re-price the USD sleeve at the dashboard's rate.
        holdings_krw = float(native.get("holdings_krw") or 0) + float(native.get("holdings_usd") or 0) * fx_rate
        cash_krw = float(native.get("cash_krw") or 0) + float(native.get("cash_usd") or 0) * fx_rate
    total_krw = holdings_krw + cash_krw
    return {
        "total_krw": round(total_krw, 2),
        "cash_krw": round(cash_krw, 2),
        "holdings_krw": round(holdings_krw, 2),
        "as_of": raw_as_of,
        "stale": False,
        "age_hours": round(age_h, 2),
        "holdings_count": len(snap.get("holdings") or []),
        "source": snap.get("source") or "unknown",
    }


def get_portfolio(totals=None):
    """Query KIS API for real account totals + Upbit.

    `totals` may be passed in to avoid a second KIS round-trip; if None,
    we call get_account_totals() ourselves.
    """
    try:
        if totals is None:
            from query_account_total import get_account_totals
            totals = get_account_totals()

        kr = totals["kr"]
        us = totals["us"]
        krw_cash = totals["krw_cash"]                                # prvs_rcdl_excc_amt — settled cash incl. today's KR pending
        usd_cash = totals["usd_cash"]                                # USD cash in USD
        unsettled_us_sell_krw = totals.get("unsettled_us_sell_krw", 0)  # US T+3 pending in KRW

        # Exchange rate from KIS
        exchange_rate = 1380
        try:
            import kis_auth as ka
            import inquire_psamount
            ka.auth(svr="prod")
            acct = ka.getTREnv()
            df = inquire_psamount.inquire_psamount(
                cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
                ovrs_excg_cd="NASD", item_cd="AAPL",
                ovrs_ord_unpr="0", env_dv="real"
            )
            if df is not None and not df.empty:
                rate = float(df.iloc[0].get("exrt", 0))
                if rate > 0:
                    exchange_rate = rate
        except Exception:
            pass

        # Upbit (BTC) — separate account
        upbit_equity = 0
        vb_state_file = os.path.join(TRADING_DIR, "upbit_vb_strategy_state.json")
        if os.path.exists(vb_state_file):
            with open(vb_state_file) as f:
                vb = json.load(f)
            eq = vb.get("equity_history", [])
            if eq:
                upbit_equity = eq[-1][1]

        # Total KIS assets = KR stocks + KRW cash (incl. pending KR settlements)
        #                  + US stocks (KRW) + USD cash (KRW)
        #                  + unsettled US sales (KRW, already converted by KIS)
        # The two "settlement-in-flight" terms (KR pending via prvs_rcdl_excc_amt,
        # US pending via ustl_sll_amt_smtl) were both being dropped — proceeds
        # had left stock_value but hadn't landed in cash yet — so the dashboard
        # was understating total assets by the day's settling-out trades.
        kis_total = (kr["stock_value"] + krw_cash
                     + us["stock_value"] * exchange_rate
                     + usd_cash * exchange_rate
                     + unsettled_us_sell_krw)

        total_value = kis_total + upbit_equity

        # Original deposit
        original_deposit = 75714217  # fallback
        if os.path.exists(DEPOSITS_FILE):
            with open(DEPOSITS_FILE) as f:
                dep = json.load(f)
                original_deposit = dep.get("kis_total_original_krw", 65726902) + dep.get("upbit_original_krw", 9987315)

        total_profit = total_value - original_deposit
        total_pct = (total_profit / original_deposit * 100) if original_deposit > 0 else 0

        return {
            "total_value_krw": round(total_value),
            "original_deposit_krw": original_deposit,
            "total_profit_krw": round(total_profit),
            "total_profit_pct": round(total_pct, 2),
            "cash_krw": round(krw_cash),
            "cash_usd": round(usd_cash, 2),
            "unsettled_us_sell_krw": round(unsettled_us_sell_krw),
            "upbit_krw": round(upbit_equity),
            "exchange_rate": exchange_rate,
        }
    except Exception as e:
        print("  Portfolio query failed: {}".format(e))
        import traceback
        traceback.print_exc()
        return {}


def get_account_totals_resilient(retries=4, delay=2.0, fetch=None, sleep=None):
    """get_account_totals(), retried when the KR holdings come back empty.

    The KIS balance query intermittently returns an empty page with no
    exception (the same dropout that zeroes korea_etf and the other KR-funded
    bots). The KR account in this portfolio is never legitimately empty, so an
    empty kr.holdings list is a transient miss — re-query before publishing so
    recovery, mark-to-market, and the price index all see the real positions.
    Returns the first response with non-empty KR holdings, else the last
    response after `retries` attempts.
    """
    import time as _t
    if sleep is None:
        sleep = _t.sleep
    if fetch is None:
        from query_account_total import get_account_totals
        fetch = get_account_totals
    totals = None
    for attempt in range(retries):
        totals = fetch()
        kr = ((totals or {}).get("kr") or {}).get("holdings") or []
        if kr:
            if attempt:
                print("  KR account recovered after {} empty attempt(s)".format(attempt))
            return totals
        if attempt < retries - 1:
            sleep(delay)
    print("  WARNING: KR account holdings empty after {} attempts (KR bots may show 0)".format(retries))
    return totals


def recover_missing_bot_holdings(api_data, totals):
    """Restore a bot's position from the authoritative account balance when its
    own result file reported zero holdings.

    Korea ETF Momentum scopes its KIS balance query to a single ticker
    (`get_holdings([state_target])`) and that call intermittently returns an
    empty page with NO exception — see the bot log flickering
    "Current holding: 139220 (1263 shares)" -> "None (0 shares)" -> back again
    on days with no rebalance and an unchanged target_qty. On a dropout the bot
    writes holdings=[] / total_profit=0, which zeroes the dashboard card even
    though the position is live.

    The account-level totals query (`get_account_totals`) is a SEPARATE,
    full-account call made at publish time; when it sees the bot's target
    ticker we recover the position from it. 139220 (TIGER 200 Construction) is
    KEM-exclusive (not in any other bot's universe), so the full account row is
    attributable to this bot. Guarded so we never recover a ticker another bot
    already claims (avoids double-counting), and only fires when the bot itself
    reported nothing. mark_to_market_strategies runs afterward and recomputes
    profit / total_pl_ytd / profit_rate from the restored holdings.
    """
    if not totals:
        return 0
    kr_acct = {h.get("ticker"): h for h in (totals.get("kr") or {}).get("holdings", []) or []
               if (h.get("quantity") or 0) > 0}
    # Tickers other bots already report holding — don't poach these.
    claimed = set()
    for s in api_data.get("strategies", []) or []:
        if s.get("id") == "korea_etf":
            continue
        for leg in (s, s.get("kr") or {}, s.get("us") or {}):
            for h in (leg.get("holdings") or []):
                if (h.get("quantity") or 0) > 0:
                    claimed.add(h.get("ticker"))

    recovered = 0
    for s in api_data.get("strategies", []) or []:
        if s.get("id") != "korea_etf":
            continue
        if any((h.get("quantity") or 0) > 0 for h in (s.get("holdings") or [])):
            continue  # bot reported a live position; nothing to recover
        tkr = (s.get("extra") or {}).get("target_ticker")
        acct = kr_acct.get(tkr)
        if not tkr or tkr == "CASH" or not acct or tkr in claimed:
            continue
        qty = acct["quantity"]
        val = float(acct.get("value") or 0)
        profit = float(acct.get("profit") or 0)
        cur = val / qty if qty else 0
        avg = (val - profit) / qty if qty else 0
        s["holdings"] = [{
            "ticker": tkr,
            "quantity": qty,
            "value": round(val, 2),
            "avg_price": round(avg, 4),
            "current_price": round(cur, 2),
            "profit": round(profit, 2),
            "profit_rate": acct.get("profit_rate", 0),
            "currency": "KRW",
            "recovered_from_account": True,
        }]
        s["value"] = round(val, 2)
        s["cost_basis"] = round(val - profit, 2)
        s["profit"] = round(profit, 2)
        recovered += 1
    return recovered


def _build_kis_price_index(totals):
    """ticker -> (current_price_native, currency) from KIS positions.

    KIS's `value` is mark-to-market (qty * current_price), so we recover
    the price as value/qty. This is the live price KIS used at the moment
    the inquire_balance call returned — typically <1s old.
    """
    idx = {}
    for h in (totals.get("kr") or {}).get("holdings", []) or []:
        qty = h.get("quantity") or 0
        val = h.get("value") or 0
        if qty > 0:
            idx[h["ticker"]] = (val / qty, "KRW")
    for h in (totals.get("us") or {}).get("holdings", []) or []:
        qty = h.get("quantity") or 0
        val = h.get("value") or 0
        if qty > 0:
            idx[h["ticker"]] = (val / qty, "USD")
    return idx


def _mark_holdings(holdings, price_idx, skipped):
    """Re-price each holding in-place using KIS current prices. Returns the
    bot's new unrealized P&L (sum of per-holding profits).

    Updates value, profit, current_price, AND profit_rate so the dashboard's
    per-holding row (price + %) shows the live mark, not the bot's morning
    snapshot. Without the profit_rate update the HTML renderer falls through
    to a stale value from the bot's result file — and when that stale value
    happens to be 0 (no movement at write time) the % is omitted entirely
    because `hld.profit_rate ? ...` treats 0 as falsy. See incident
    2026-05-03 (Hybrid VB KR holdings showed pl but no % for 091170).
    """
    new_unrealized = 0.0
    for h in holdings or []:
        ticker = h.get("ticker")
        qty = h.get("quantity") or 0
        if not ticker or qty <= 0 or ticker not in price_idx:
            if ticker:
                skipped.add(ticker)
            new_unrealized += h.get("profit") or 0
            continue
        cur_price, _ = price_idx[ticker]
        h["value"] = round(cur_price * qty, 2)
        avg = h.get("avg_price")
        if avg is not None and avg > 0:
            h["profit"] = round((cur_price - avg) * qty, 2)
            h["profit_rate"] = round((cur_price - avg) / avg * 100, 2)
        h["current_price"] = cur_price
        h["live_price"] = cur_price
        h["mark_to_market"] = True
        new_unrealized += h.get("profit") or 0
    return new_unrealized


def mark_to_market_strategies(api_data, totals):
    """Mutate api_data so each bot's holdings reflect current KIS prices.

    Recomputes unrealized_profit / profit / total_pl_ytd from the updated
    holdings. Realized fields (realized_profit_ytd) are NOT touched —
    those are YTD-from-2026-01-01 by design and come from the aggregator.

    btc_vb is skipped because dashboard_server already does live
    mark-to-market via pyupbit when a position is held.
    """
    price_idx = _build_kis_price_index(totals)
    if not price_idx:
        return {"marked": 0, "skipped": [], "note": "no KIS prices"}

    marked = 0
    skipped = set()

    for s in api_data.get("strategies", []) or []:
        sid = s.get("id")
        if sid == "btc_vb":
            continue
        if sid == "hybrid_vb":
            for leg in ("kr", "us"):
                ld = s.get(leg) or {}
                if not ld.get("holdings"):
                    continue
                before = sum((h.get("profit") or 0) for h in ld["holdings"])
                new_un = _mark_holdings(ld["holdings"], price_idx, skipped)
                marked += sum(1 for h in ld["holdings"] if h.get("mark_to_market"))
                ld["unrealized_profit"] = round(new_un, 2)
                ld["profit"] = ld["unrealized_profit"]
                real = ld.get("realized_profit_ytd") or 0
                ld["total_pl_ytd"] = round(real + new_un, 2)
                ld["unrealized_drift_at_mtm"] = round(new_un - before, 2)
                # Re-sum value and re-derive the rate from the marked holdings.
                # _mark_holdings refreshes each holding's value/profit but the
                # leg-level value and profit_rate are otherwise left at their
                # stale pre-mark figures — which makes value-cost != profit and
                # flips KR Profit % positive against a negative Total P/L.
                ld["value"] = round(sum((h.get("value") or 0) for h in ld["holdings"]), 2)
                leg_budget = s.get("budget_kr") if leg == "kr" else s.get("budget_us")
                if leg_budget and leg_budget > 0:
                    ld["profit_rate_ytd_pct"] = round(ld["total_pl_ytd"] / leg_budget * 100, 2)
                s[leg] = ld
            continue

        if not s.get("holdings"):
            continue
        before = sum((h.get("profit") or 0) for h in s["holdings"])
        new_un = _mark_holdings(s["holdings"], price_idx, skipped)
        marked += sum(1 for h in s["holdings"] if h.get("mark_to_market"))
        s["unrealized_profit"] = round(new_un, 2)
        s["profit"] = s["unrealized_profit"]
        real = s.get("realized_profit_ytd") or 0
        s["total_pl_ytd"] = round(real + new_un, 2)
        budget = s.get("budget") or 0
        if budget and budget > 0:
            s["profit_rate_ytd_pct"] = round(s["total_pl_ytd"] / budget * 100, 2)
        s["unrealized_drift_at_mtm"] = round(new_un - before, 2)

    return {"marked": marked, "skipped": sorted(skipped)}


# ═════════════════════════════════════════════════════════════════════════════
# Hands-on / Manual sleeve  +  accounts / totals contract
#
# Jae runs most of his money by hand, in front of the bots. Everything below
# exists so the published dashboard answers "what is my WHOLE investment
# status", not just "what did the bots do". The split is built by PARTITIONING
# the broker account rows — every share is either bot-claimed or hands-on, and
# every won is either in a position or in cash — so bots + manual + cash is the
# account total by construction, and `reconciliation_gap_krw` is a real alarm
# rather than a rounding readout.
#
# All the builders below are pure functions over already-fetched payloads so
# they unit-test with fixtures (tests/test_dashboard/test_manual_sleeve.py).
# ═════════════════════════════════════════════════════════════════════════════

def kis_holdings_map(totals):
    """{ticker: {qty, value_native, currency, name, profit}} for the WHOLE KIS account.

    PURE — takes the already-fetched get_account_totals() payload. KIS's `value`
    is mark-to-market (qty x current price), so slicing a position by quantity
    is a straight pro-rate of both value and profit.
    """
    out = {}
    for leg, cur in (("kr", "KRW"), ("us", "USD")):
        for h in ((totals or {}).get(leg) or {}).get("holdings", []) or []:
            ticker = h.get("ticker")
            qty = float(h.get("quantity") or 0)
            if not ticker or qty <= 0 or ticker in out:
                continue                      # KR/US tickers never collide; first wins
            out[ticker] = {
                "qty": qty,
                "value_native": float(h.get("value") or 0),
                "currency": cur,
                "name": h.get("name") or ticker,
                "profit": float(h.get("profit") or 0),
            }
    return out


def collect_bot_claims(strategies, sources=("holdings", "open_positions")):
    """{ticker: qty} claimed by the bots, read off the dashboard's own cards. PURE.

    Three claim sources per card, because a bot's *reported* holdings can drop
    out while the position is live — the same intermittent KIS balance-page miss
    that zeroes korea_etf. On 2026-08-16 Hybrid VB's KR leg reported no holdings
    at all while it actually held 091170/132030/144600 (~W9.35M):
      1. card `holdings`
      2. `kr` / `us` leg holdings (hybrid_vb)
      3. `open_positions` — the bot's own position ledger, already published by
         dashboard_server; authoritative exactly when 1 and 2 have dropped out.
    Without source 3 that W9.35M is reported to Jae as HIS hands-on money.

    Within one card the sources overlap (hybrid's top-level `holdings` IS its US
    leg), so we take the MAX per ticker rather than summing. Across cards we sum
    — and the caller caps the sum at the account quantity, which absorbs two
    cards claiming the same name (korea_etf's account-recovered 132030 vs
    hybrid_vb's live open position in it).

    `sources` narrows which of the three count, so the caller can measure the
    dropout itself: claims(all) - claims(("holdings",)) is exactly the value of
    live bot positions their own cards forgot to report.

    Cards carrying an explicit non-KIS `account` (NMF2's "toss") are skipped: the
    result is matched against the KIS holdings map, and Toss and KIS share
    Korea's 6-digit ticker space, so an unguarded match would hand the bot KIS
    shares it does not own — inflating `bots_krw` and opening a reconciliation
    gap. Those cards are attributed against their own account instead.
    """
    claims = {}
    for s in strategies or []:
        if not isinstance(s, dict) or s.get("id") == "manual":
            continue
        if s.get("account") not in (None, "", "kis"):
            continue
        per = {}

        def _take_holdings(rows):
            for h in rows or []:
                if not isinstance(h, dict):
                    continue
                ticker = h.get("ticker")
                qty = float(h.get("quantity") or h.get("qty") or 0)
                if ticker and qty > 0:
                    per[ticker] = max(per.get(ticker, 0.0), qty)

        def _take_positions(book):
            for ticker, pos in (book or {}).items():
                if not isinstance(pos, dict):
                    continue
                if "shares" in pos or "quantity" in pos:
                    qty = float(pos.get("shares") or pos.get("quantity") or 0)
                    if ticker and qty > 0:
                        per[ticker] = max(per.get(ticker, 0.0), qty)
                else:
                    _take_positions(pos)          # {"kr": {...}, "us": {...}}

        if "holdings" in sources:
            _take_holdings(s.get("holdings"))
            for leg in ("kr", "us"):
                leg_data = s.get(leg)
                if isinstance(leg_data, dict):
                    _take_holdings(leg_data.get("holdings"))
        if "open_positions" in sources and isinstance(s.get("open_positions"), dict):
            _take_positions(s["open_positions"])

        for ticker, qty in per.items():
            claims[ticker] = claims.get(ticker, 0.0) + qty
    return claims


def _attributed_krw(kis_holdings, claims, fx):
    """KRW value of the account shares `claims` covers (capped at what is held)."""
    total = 0.0
    for ticker, acct in (kis_holdings or {}).items():
        qty = float(acct.get("qty") or 0)
        claimed = min(float(claims.get(ticker, 0.0)), qty)
        if qty <= 0 or claimed <= 0:
            continue
        value_native = float(acct.get("value_native") or 0) * (claimed / qty)
        total += value_native if acct.get("currency") == "KRW" else value_native * fx
    return total


def load_nmf2_ledger(path=None):
    """The NMF2 bot's ledger, READ-ONLY. Returns {} on any problem, never raises.

    Degrading to {} is deliberate: a publish must not fail — and must not shift
    money between sleeves — because the bot rotated its state file mid-read. An
    empty ledger yields no nmf2 card, and every Toss share falls back to the
    hands-on sleeve exactly as it did before this bot had a card.
    """
    try:
        with open(path or NMF2_LEDGER_FILE) as f:
            led = json.load(f)
    except Exception:
        return {}
    if not isinstance(led, dict) or not isinstance(led.get("positions"), dict):
        return {}
    return led


def build_nmf2_card(ledger, toss_holdings, fx_rate=None):
    """The NMF2 bot as a first-class strategy card. PURE — no I/O.

    The ledger knows WHAT the bot owns (qty + avg_price); it does not know what
    those shares are worth today. The Mac-side Toss snapshot does. So the card is
    the intersection: for each ledger symbol, claim `min(ledger_qty, account_qty)`
    shares and value them by pro-rating the account's own mark.

    Two rules keep `totals.reconciliation_gap_krw` at zero:
      1. Never value more shares than the account reports — the same pro-rate the
         KIS sleeve uses, so the bot and the hands-on sleeve split one number.
      2. A ledger symbol absent from the snapshot contributes ZERO, not
         qty x avg_price. Cost basis is not market value, and money the account
         does not report cannot be added to a total built from account rows. Such
         symbols are named in `extra.unmatched_symbols` rather than swallowed.

    Ledger cash is reported for context but NOT added to `value` — Toss cash is
    already whole inside `totals.cash_krw`, and counting it here would book it
    twice.
    """
    positions = (ledger or {}).get("positions") or {}
    if not positions:
        return None
    fx = float(fx_rate or 0)
    by_symbol = {}
    for h in toss_holdings or []:
        if isinstance(h, dict):
            sym = h.get("symbol") or h.get("ticker")
            if sym and sym not in by_symbol:
                by_symbol[sym] = h

    rows, unmatched = [], []
    value_krw = cost_krw = 0.0
    for symbol, pos in sorted(positions.items()):
        if not isinstance(pos, dict):
            continue
        led_qty = float(pos.get("qty") or 0)
        if led_qty <= 0:
            continue
        acct = by_symbol.get(symbol)
        acct_qty = float((acct or {}).get("qty") or (acct or {}).get("quantity") or 0)
        if not acct or acct_qty <= 0:
            unmatched.append(symbol)
            continue
        claimed = min(led_qty, acct_qty)
        share = claimed / acct_qty
        cur = acct.get("currency") or "KRW"
        acct_native = float(acct.get("value_native") or acct.get("value_krw") or 0)
        native = acct_native * share
        krw = native if cur == "KRW" else (native * fx if fx > 0
                                           else float(acct.get("value_krw") or 0) * share)
        cost = claimed * float(pos.get("avg_price") or 0)
        value_krw += krw
        cost_krw += cost
        rows.append({
            "ticker": symbol,
            "name": acct.get("name") or symbol,
            "qty": round(claimed, 6),
            "quantity": round(claimed, 6),
            "currency": cur,
            "avg_price": round(float(pos.get("avg_price") or 0), 4),
            "current_price": round(native / claimed, 4) if claimed else 0.0,
            "value": round(native, 2),
            "value_krw": round(krw, 2),
            "profit": round(krw - cost, 2),
            "profit_rate": round((krw - cost) / cost * 100, 2) if cost > 0 else 0.0,
            "source": "toss",
            "ledger_qty": round(led_qty, 6),
        })

    rows.sort(key=lambda r: r.get("value_krw", 0), reverse=True)
    unrealized = value_krw - cost_krw
    budget = float((ledger or {}).get("budget_krw") or 0)
    return {
        "id": "nmf2",
        "name": "NMF2 (신마법공식 2.0)",
        "currency": "KRW",
        "mode": "live",
        # Marks this card as trading a NON-KIS account. collect_bot_claims reads
        # it to keep these KRX codes away from the KIS holdings map (Toss and KIS
        # share Korea's 6-digit ticker space, so an unguarded match would credit
        # the bot with KIS shares it does not own and open a reconciliation gap).
        "account": "toss",
        "value": round(value_krw, 2),
        "cost_basis": round(cost_krw, 2),
        "budget": round(budget, 2),
        "unrealized_profit": round(unrealized, 2),
        # No realized figure: the ledger keeps positions, not a closed-trade
        # book, and this sleeve has not sold since inception. 0.0 is the honest
        # value today, and total_pl_ytd stays equal to unrealized because of it.
        "realized_profit_ytd": 0.0,
        "realized_trades": 0,
        "total_pl_ytd": round(unrealized, 2),
        "profit_rate_ytd_pct": round(unrealized / cost_krw * 100, 2) if cost_krw > 0 else 0.0,
        "holdings": rows,
        "last_run": (ledger or {}).get("updated"),
        "extra": {
            "note": "신마법공식 2.0 + 계절성 — real-money KRW sleeve inside the Toss account, "
                    "run from Jae's Mac. Positions come from the bot's ledger; prices from the Toss snapshot.",
            "ticker_count": len(rows),
            "ledger_position_count": len(positions),
            "cash_krw": round(float((ledger or {}).get("cash_krw") or 0), 2),
            "budget_krw": round(budget, 2),
            "deployed_pct": round(cost_krw / budget * 100, 2) if budget > 0 else 0.0,
            "unmatched_symbols": unmatched,
            "ledger_updated": (ledger or {}).get("updated"),
            "caveat": "Value is marked from the Toss snapshot, so it is only as fresh as that "
                      "snapshot. Ledger cash is shown but excluded from `value` — Toss cash is "
                      "counted once, in totals.cash_krw.",
        },
    }


def build_manual_sleeve(all_holdings, strategies, fx_rate, toss_holdings=None,
                        toss_claims=None):
    """The Hands-on / Manual sleeve, as a strategy-shaped card. PURE — no I/O.

    manual_qty = account_qty - SUM(bot-claimed qty), per ticker, valued at the
    mark the account itself reports and converted to KRW at the dashboard's FX
    rate. Zero and negative diffs are dropped (a bot claiming more shares than
    the account holds is a bot-side bug, never negative hands-on money).

    Cash is deliberately NOT in the sleeve — it is totals.cash_krw.

    `toss_holdings` (rows from the Mac-side Toss snapshot) get the SAME
    subtraction, via `toss_claims` ({symbol: qty} owned by Toss-account bots —
    today that is NMF2's ledger). Whatever the bots do not claim is hands-on.
    Including Toss here is what makes Task 5's reconciliation meaningful:
    otherwise ~W187M of real money belongs to no bucket at all and the gap check
    fires on every publish. And because bot and hands-on shares are carved out of
    the same account row, the two can never both count the same share.
    """
    fx = float(fx_rate or 0)
    claims = collect_bot_claims(strategies)
    rows = []
    kis_krw = 0.0
    kis_unrealized_krw = 0.0

    for ticker, acct in sorted((all_holdings or {}).items()):
        qty = float(acct.get("qty") or 0)
        if qty <= 0:
            continue
        claimed = min(float(claims.get(ticker, 0.0)), qty)
        manual_qty = qty - claimed
        if manual_qty <= 1e-9:
            continue
        share = manual_qty / qty
        cur = acct.get("currency") or "KRW"
        value_native = float(acct.get("value_native") or 0) * share
        profit_native = float(acct.get("profit") or 0) * share
        value_krw = value_native if cur == "KRW" else value_native * fx
        profit_krw = profit_native if cur == "KRW" else profit_native * fx
        price = value_native / manual_qty if manual_qty else 0.0
        kis_krw += value_krw
        kis_unrealized_krw += profit_krw
        rows.append({
            "ticker": ticker,
            "name": acct.get("name") or ticker,
            "qty": round(manual_qty, 6),
            "quantity": round(manual_qty, 6),          # generic card renderer
            "currency": cur,
            "current_price": round(price, 4),
            "value": round(value_native, 2),
            "value_krw": round(value_krw, 2),
            "profit": round(profit_native, 2),
            "source": "kis",
            "bot_claimed_qty": round(claimed, 6),
        })

    toss_krw = 0.0
    toss_rows = 0
    toss_bot_claimed_krw = 0.0
    for h in toss_holdings or []:
        if not isinstance(h, dict):
            continue
        acct_qty = float(h.get("qty") or h.get("quantity") or 0)
        cur = h.get("currency") or "KRW"
        acct_native = float(h.get("value_native") or h.get("value_krw") or 0)
        # Re-price at the DASHBOARD's FX rate, not the snapshot's own fallback
        # rate. accounts.toss.total_krw is already re-priced that way; leaving
        # the sleeve on the snapshot's rate opens a reconciliation gap the size
        # of the FX drift (W286k at a 3-won difference on a $98k US sleeve).
        if fx > 0:
            acct_krw = acct_native if cur == "KRW" else acct_native * fx
        else:
            acct_krw = float(h.get("value_krw") or acct_native)
        if acct_qty <= 0 or acct_krw <= 0:
            continue
        ticker = h.get("symbol") or h.get("ticker") or "?"
        claimed = min(float((toss_claims or {}).get(ticker, 0.0)), acct_qty)
        manual_qty = acct_qty - claimed
        toss_bot_claimed_krw += acct_krw * (claimed / acct_qty)
        if manual_qty <= 1e-9:
            continue
        share = manual_qty / acct_qty
        value_native = acct_native * share
        value_krw = acct_krw * share
        toss_krw += value_krw
        toss_rows += 1
        rows.append({
            "ticker": ticker,
            "name": h.get("name") or ticker,
            "qty": round(manual_qty, 6),
            "quantity": round(manual_qty, 6),
            "currency": cur,
            "current_price": round(value_native / manual_qty, 4) if manual_qty else 0.0,
            "value": round(value_native, 2),
            "value_krw": round(value_krw, 2),
            "source": "toss",
            "bot_claimed_qty": round(claimed, 6),
        })

    rows.sort(key=lambda r: r.get("value_krw", 0), reverse=True)
    total_krw = kis_krw + toss_krw
    return {
        "id": "manual",
        "name": "Hands-on / Manual",
        "currency": "KRW",
        "mode": "manual",
        "value": round(total_krw, 2),
        "holdings": rows,
        # unrealized_profit / realized_profit_ytd are deliberately ABSENT: the
        # Toss snapshot carries no cost basis, so a P/L here would be partial,
        # and both the hero strip and get_portfolio() sum those keys across
        # strategies to build the headline "Total P&L". A half-covered number
        # folded into that headline is worse than no number.
        "extra": {
            "note": "Broker positions attributed to no bot: KIS and Toss holdings minus every bot's claimed shares.",
            "ticker_count": len(rows),
            "kis_krw": round(kis_krw, 2),
            "kis_ticker_count": sum(1 for r in rows if r.get("source") == "kis"),
            "kis_unrealized_krw": round(kis_unrealized_krw, 2),
            "toss_krw": round(toss_krw, 2),
            "toss_ticker_count": toss_rows,
            # What the Toss-account bots (NMF2) took out of this sleeve. Kept
            # here so the carve-out is auditable from the published JSON alone.
            "toss_bot_claimed_krw": round(toss_bot_claimed_krw, 2),
            "caveat": "The NMF2 sleeve inside Toss is now its own bot card and is excluded here. "
                      "No cost basis exists for the remaining Toss rows, so no P/L is reported for this sleeve.",
        },
    }


def build_accounts(totals, portfolio, toss, strategies, now):
    """`accounts` block: KIS / Upbit / Toss, each honestly flagged. PURE.

    Never raises and never omits a key — a missing account is a zeroed tile with
    stale=true, because a silently dropped sleeve understates Jae's net worth
    without saying so.
    """
    fx = float((portfolio or {}).get("exchange_rate") or 0) or 1380.0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if totals:
        kr = totals.get("kr") or {}
        us = totals.get("us") or {}
        krw_cash = float(totals.get("krw_cash") or 0)
        usd_cash = float(totals.get("usd_cash") or 0)
        unsettled = float(totals.get("unsettled_us_sell_krw") or 0)
        kr_stock = float(kr.get("stock_value") or 0)
        us_stock_krw = float(us.get("stock_value") or 0) * fx
        # Settlement-in-flight (KR pending inside prvs_rcdl_excc_amt, US T+3 via
        # ustl_sll_amt_smtl) is money out of the positions and not yet in cash.
        # It is counted as cash so the split can't lose it.
        cash_krw = krw_cash + usd_cash * fx + unsettled
        kis = {
            "total_krw": round(kr_stock + us_stock_krw + cash_krw, 2),
            "cash_krw": round(cash_krw, 2),
            "stock_krw": round(kr_stock + us_stock_krw, 2),
            "as_of": stamp,
            "stale": False,
            "kr_stock_krw": round(kr_stock, 2),
            "us_stock_krw": round(us_stock_krw, 2),
            "krw_cash_krw": round(krw_cash, 2),
            "usd_cash_usd": round(usd_cash, 2),
            "unsettled_us_sell_krw": round(unsettled, 2),
            "holdings_count": len(kr.get("holdings") or []) + len(us.get("holdings") or []),
        }
    else:
        kis = {"total_krw": 0.0, "cash_krw": 0.0, "stock_krw": 0.0, "as_of": None,
               "stale": True, "note": "KIS account query failed at publish time"}

    btc = next((s for s in (strategies or []) if s.get("id") == "btc_vb"), {}) or {}
    upbit_total = float((portfolio or {}).get("upbit_krw") or btc.get("value") or 0)
    holding = bool(btc.get("is_holding"))
    upbit = {
        "total_krw": round(upbit_total, 2),
        # Flat bot => the whole sleeve is idle KRW. Calling that "deployed by a
        # bot" would overstate how much of the portfolio is actually at work.
        "cash_krw": 0.0 if holding else round(upbit_total, 2),
        "position_krw": round(upbit_total, 2) if holding else 0.0,
        "as_of": btc.get("last_run") or stamp,
        "stale": upbit_total <= 0,
        "is_holding": holding,
    }

    return {"kis": kis, "upbit": upbit, "toss": toss or {}}


def build_totals(accounts, strategies, manual_card, kis_holdings, fx_rate):
    """`totals` block — the Whole-Investment hero's numbers. PURE.

    investments_krw = KIS + Upbit + Toss.
    bots / manual / cash partition the SAME money:
      bots   = every account share a bot claims, at the account's own mark
               (+ the Upbit sleeve when the BTC bot is actually in a position,
                + Toss-account bots' marked value — NMF2, whose shares the
                  hands-on sleeve gave up in the very same carve-out)
      manual = the hands-on sleeve (KIS and Toss leftovers)
      cash   = KIS cash + settlement-in-flight + Toss cash + idle Upbit
    so the gap is zero unless an input went missing. `bots_cards_krw` is the
    naive "sum the bot cards" figure kept alongside for diagnosis: the
    difference is exactly the value of live bot positions whose card dropped
    its holdings (W8.1M of Hybrid VB KR on 2026-08-16).
    """
    fx = float(fx_rate or 0)
    kis = accounts.get("kis") or {}
    upbit = accounts.get("upbit") or {}
    toss = accounts.get("toss") or {}

    investments = (float(kis.get("total_krw") or 0)
                   + float(upbit.get("total_krw") or 0)
                   + float(toss.get("total_krw") or 0))

    bots_kis_krw = _attributed_krw(kis_holdings, collect_bot_claims(strategies), fx)
    reported_kis_krw = _attributed_krw(
        kis_holdings, collect_bot_claims(strategies, sources=("holdings",)), fx)
    # Bots trading a non-KIS account carry their own already-KRW marked value
    # (build_nmf2_card pro-rates it out of the Toss snapshot rows the hands-on
    # sleeve then skips), so they are added directly rather than re-attributed.
    upbit_bot_krw = float(upbit.get("position_krw") or 0)
    toss_bots_krw = sum(float(s.get("value") or 0) for s in (strategies or [])
                        if isinstance(s, dict) and s.get("account") == "toss")
    bots_krw = bots_kis_krw + upbit_bot_krw + toss_bots_krw
    manual_krw = float((manual_card or {}).get("value") or 0)
    cash_krw = (float(kis.get("cash_krw") or 0)
                + float(toss.get("cash_krw") or 0)
                + float(upbit.get("cash_krw") or 0))

    bots_cards_krw = 0.0
    for s in strategies or []:
        if s.get("id") == "manual":
            continue
        if s.get("currency") == "MULTI":
            for leg, cur in (("kr", "KRW"), ("us", "USD")):
                val = float(((s.get(leg) or {}).get("value")) or 0)
                bots_cards_krw += val if cur == "KRW" else val * fx
        else:
            val = float(s.get("value") or 0)
            bots_cards_krw += val if s.get("currency") == "KRW" else val * fx

    gap = investments - (bots_krw + manual_krw + cash_krw)
    gap_pct = abs(gap) / investments * 100 if investments > 0 else 0.0
    denom = bots_krw + manual_krw + cash_krw
    pct = (lambda v: round(v / denom * 100, 2)) if denom > 0 else (lambda v: 0.0)

    out = {
        "investments_krw": round(investments, 2),
        "bots_krw": round(bots_krw, 2),
        "manual_krw": round(manual_krw, 2),
        "cash_krw": round(cash_krw, 2),
        "split_pct": {"bots": pct(bots_krw), "manual": pct(manual_krw), "cash": pct(cash_krw)},
        "reconciliation_gap_krw": round(gap, 2),
        "reconciliation_gap_pct": round(gap_pct, 4),
        "reconciliation_warning": gap_pct > RECONCILIATION_WARN_PCT,
        "bots_cards_krw": round(bots_cards_krw, 2),
        # Value of live bot positions their OWN card no longer reports, rescued
        # from open_positions. Would otherwise be shown to Jae as his own money.
        "unreported_bot_positions_krw": round(bots_kis_krw - reported_kis_krw, 2),
        "bots_breakdown": {
            "kis_krw": round(bots_kis_krw, 2),
            "upbit_krw": round(upbit_bot_krw, 2),
            "toss_krw": round(toss_bots_krw, 2),
        },
        "manual_breakdown": {
            "kis_krw": round(float(((manual_card or {}).get("extra") or {}).get("kis_krw") or 0), 2),
            "toss_krw": round(float(((manual_card or {}).get("extra") or {}).get("toss_krw") or 0), 2),
        },
        "cash_breakdown": {
            "kis_krw": round(float(kis.get("cash_krw") or 0), 2),
            "toss_krw": round(float(toss.get("cash_krw") or 0), 2),
            "upbit_idle_krw": round(float(upbit.get("cash_krw") or 0), 2),
        },
        "fx_rate": fx,
    }
    return out


def load_manual_history(path=None):
    """Rows of {date, ...} from a date-keyed sleeve JSONL (never raises).

    Generic over `path`: serves both the manual sleeve and NMF2's own history.
    """
    rows = []
    try:
        with open(path or MANUAL_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("date"):
                    rows.append(row)
    except (IOError, OSError):
        return []
    rows.sort(key=lambda r: r["date"])
    return rows


def append_manual_history(row, path=None):
    """Upsert one row per date, atomically (.tmp + os.replace).

    Same durability contract as dashboard_server's equity snapshot: the reader
    (next publish, healthcheck) either sees the old file or the new one, never a
    half-written line. Publishing twice in a day replaces that day's row.
    """
    path = path or MANUAL_HISTORY_FILE
    rows = [r for r in load_manual_history(path) if r.get("date") != row.get("date")]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, path)
    return rows


def main(dry_run=False):
    """Build and publish dashboard_data.json.

    dry_run writes to /tmp and skips the manual-history append, so the numbers
    can be eyeballed on the server before anything the public site reads is
    touched.
    """
    now = datetime.now(KST)
    print("[{}] Generating dashboard data{}...".format(
        now.strftime("%H:%M:%S KST"), " (DRY RUN)" if dry_run else ""))

    # Fetch live data from dashboard server API
    try:
        raw = urllib.request.urlopen(API_URL, timeout=30).read()
        api_data = json.loads(raw)
        print("  Loaded {} strategies from API".format(len(api_data.get("strategies", []))))
    except Exception as e:
        print("  ERROR: Could not fetch from {}: {}".format(API_URL, e))
        return

    # Pull KIS account positions ONCE — used both for the portfolio summary
    # and to mark-to-market each bot's holdings.
    totals = None
    try:
        totals = get_account_totals_resilient()
    except Exception as e:
        print("  WARNING: KIS totals query failed; falling back to bot-state values: {}".format(e))

    # Recover any bot position that its own result file dropped (intermittent
    # single-ticker balance-query dropout) from the authoritative account
    # balance, BEFORE mark-to-market re-prices it.
    if totals:
        rec = recover_missing_bot_holdings(api_data, totals)
        if rec:
            print("  Recovered {} bot position(s) from account balance".format(rec))

    # Live mark-to-market: replace bot-saved holding values with current KIS
    # prices so the dashboard shows price moves up to publish time, not bot's
    # last-run snapshot. Realized P&L is untouched (YTD from 2026-01-01).
    if totals:
        mtm = mark_to_market_strategies(api_data, totals)
        if mtm.get("skipped"):
            print("  Mark-to-market: re-priced {} holdings; no KIS price for {}".format(
                mtm.get("marked", 0), ", ".join(mtm["skipped"])))
        else:
            print("  Mark-to-market: re-priced {} holdings".format(mtm.get("marked", 0)))
        api_data["mark_to_market_at"] = now.strftime("%Y-%m-%d %H:%M KST")

    # Get real account totals from KIS API (reuse the totals we already pulled)
    portfolio = get_portfolio(totals=totals)
    if portfolio:
        print("  Account total: W{:,.0f}".format(portfolio.get("total_value_krw", 0)))
    else:
        print("  WARNING: Portfolio query failed, using empty portfolio")

    # Override portfolio total_profit_krw to match the dashboard's Total P/L
    # (sum of per-bot unrealized + realized YTD). The raw account-level
    # total_value - original_deposit mixes in FX drift and top-ups, so
    # Discord was showing a number different from the dashboard strip.
    if portfolio:
        rate = portfolio.get("exchange_rate", 1380)
        unreal_krw = unreal_usd = 0.0
        real_krw = real_usd = 0.0
        for s in api_data.get("strategies", []):
            cur = s.get("currency")
            if cur == "KRW":
                unreal_krw += s.get("unrealized_profit") or s.get("profit") or 0
                real_krw   += s.get("realized_profit_ytd") or 0
            elif cur == "USD":
                unreal_usd += s.get("unrealized_profit") or s.get("profit") or 0
                real_usd   += s.get("realized_profit_ytd") or 0
            elif cur == "MULTI":
                kr = s.get("kr") or {}
                us = s.get("us") or {}
                unreal_krw += kr.get("unrealized_profit") or kr.get("profit") or 0
                unreal_usd += us.get("unrealized_profit") or us.get("profit") or 0
                real_krw   += kr.get("realized_profit_ytd") or 0
                real_usd   += us.get("realized_profit_ytd") or 0
        unreal_combined = unreal_krw + unreal_usd * rate
        real_combined   = real_krw   + real_usd   * rate
        total_pnl = unreal_combined + real_combined
        original = portfolio.get("original_deposit_krw") or 1
        portfolio["unrealized_krw"]    = round(unreal_combined)
        portfolio["realized_krw"]      = round(real_combined)
        portfolio["unrealized_native"] = {"krw": round(unreal_krw), "usd": round(unreal_usd, 2)}
        portfolio["realized_native"]   = {"krw": round(real_krw),   "usd": round(real_usd, 2)}
        portfolio["total_profit_krw"]  = round(total_pnl)
        portfolio["total_profit_pct"]  = round(total_pnl / original * 100, 2) if original > 0 else 0
        print("  Matched dashboard P/L: realized W{:+,.0f} + unrealized W{:+,.0f} = W{:+,.0f}".format(
            real_combined, unreal_combined, total_pnl))

    # Re-pin the comparison-chart endpoints to the marked-to-market cards so the
    # published chart can't disagree with the published bot cards.
    pin_equity_endpoints(
        api_data.get("equity_series") or {},
        api_data.get("strategies") or [],
        (portfolio or {}).get("exchange_rate", 1380),
        et_today(),
    )

    # Merge
    output = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "portfolio": portfolio,
    }
    output.update(api_data)

    fx = (portfolio or {}).get("exchange_rate") or 1380

    # Toss Securities account (Mac-side read-only snapshot). setdefault so this
    # merges with whatever else populates `accounts`, in any order.
    toss = load_toss_account(fx_rate=fx)
    output.setdefault("accounts", {})["toss"] = toss
    print("  Toss: {}".format(
        "STALE/absent — {}".format(toss.get("note")) if toss["stale"]
        else "W{:,.0f} ({} holdings, as of {})".format(
            toss["total_krw"], toss.get("holdings_count", 0), toss["as_of"])))

    # ── Hands-on / Manual sleeve + whole-investment totals ───────────────────
    # Everything below runs AFTER pin_equity_endpoints on purpose: the manual
    # card is not a bot, so it must not have its chart endpoint pinned to a
    # total_pl_ytd it does not have.
    toss_rows = []
    if not toss.get("stale"):
        try:
            with open(TOSS_SNAPSHOT_FILE) as f:
                toss_rows = json.load(f).get("holdings") or []
        except Exception as e:
            print("  WARNING: Toss holdings unreadable for the manual sleeve: {}".format(e))

    kis_holdings = kis_holdings_map(totals)
    strategies = output.get("strategies") or []

    # NMF2 first: it claims its ledger's shares out of the Toss rows so the
    # hands-on sleeve below can only ever see what is left. One carve-out, two
    # sleeves — which is why the reconciliation gap cannot move.
    nmf2 = build_nmf2_card(load_nmf2_ledger(), toss_rows, fx)
    toss_claims = {r["ticker"]: r["qty"] for r in (nmf2 or {}).get("holdings", [])}
    if nmf2:
        nx = nmf2["extra"]
        print("  NMF2: W{:,.0f} ({}/{} ledger positions marked, cost W{:,.0f}, "
              "P/L W{:+,.0f}, budget W{:,.0f})".format(
                  nmf2["value"], nx["ticker_count"], nx["ledger_position_count"],
                  nmf2["cost_basis"], nmf2["total_pl_ytd"], nmf2["budget"]))
        if nx["unmatched_symbols"]:
            print("  NOTE: NMF2 ledger symbols absent from the Toss snapshot "
                  "(valued at 0, NOT counted): {}".format(", ".join(nx["unmatched_symbols"])))
    else:
        print("  NMF2: no ledger positions readable — no card; "
              "its Toss shares stay in the hands-on sleeve")

    manual = build_manual_sleeve(kis_holdings, strategies, fx, toss_rows, toss_claims)
    strategies = [s for s in strategies if s.get("id") not in ("manual", "nmf2")]
    if nmf2:
        strategies.append(nmf2)
    strategies.append(manual)                     # hands-on always last in the grid
    output["strategies"] = strategies
    mx = manual["extra"]
    print("  Hands-on / Manual: W{:,.0f} ({} tickers — KIS W{:,.0f} / {}, Toss W{:,.0f} / {})".format(
        manual["value"], mx["ticker_count"], mx["kis_krw"], mx["kis_ticker_count"],
        mx["toss_krw"], mx["toss_ticker_count"]))

    # Manual history: one row per publish date, atomically upserted. The series
    # is a VALUE series (the Toss half has no cost basis, so a P/L series would
    # be half-covered); the comparison chart is a P/L chart and deliberately
    # does NOT plot it — the manual card's own sparkline does.
    hist_row = {"date": et_today(), "value_krw": manual["value"],
                "kis_krw": mx["kis_krw"], "toss_krw": mx["toss_krw"],
                "ticker_count": mx["ticker_count"],
                "toss_stale": bool(toss.get("stale"))}
    if dry_run:
        history = [r for r in load_manual_history() if r.get("date") != hist_row["date"]] + [hist_row]
    else:
        try:
            history = append_manual_history(hist_row)
        except Exception as e:
            print("  WARNING: could not persist manual sleeve history: {}".format(e))
            history = [hist_row]
    eq = output.setdefault("equity_series", {})
    eq["manual"] = [[r["date"], r.get("value_krw", 0)] for r in history]

    # NMF2's comparison-chart series. It is built here rather than by
    # pin_equity_endpoints because that helper only extends series that already
    # exist, and dashboard_server has never snapshotted this bot — the Toss
    # sleeve is invisible to the KIS-side equity loop. The chart is a Total P/L
    # chart, so this series is P/L (W66k scale), not the W915k value: dropping a
    # value series into a P/L chart would dwarf every other bot's line.
    if nmf2:
        nmf2_row = {"date": et_today(), "total_pl_krw": nmf2["total_pl_ytd"],
                    "value_krw": nmf2["value"], "cost_krw": nmf2["cost_basis"],
                    "ticker_count": nmf2["extra"]["ticker_count"],
                    "toss_stale": bool(toss.get("stale"))}
        if dry_run:
            nmf2_hist = [r for r in load_manual_history(NMF2_HISTORY_FILE)
                         if r.get("date") != nmf2_row["date"]] + [nmf2_row]
        else:
            try:
                nmf2_hist = append_manual_history(nmf2_row, NMF2_HISTORY_FILE)
            except Exception as e:
                print("  WARNING: could not persist NMF2 history: {}".format(e))
                nmf2_hist = [nmf2_row]
        eq["nmf2"] = [[r["date"], r.get("total_pl_krw", 0)] for r in nmf2_hist]
        if len(eq["nmf2"]) < 2:
            print("  NOTE: NMF2 has {} history point(s) — the comparison chart shows "
                  "'insufficient data' until the next publish".format(len(eq["nmf2"])))

    # build_accounts re-emits the very `toss` dict the Toss ingest produced, so
    # this update adds kis/upbit without ever rewriting Task 4's contract.
    accounts = build_accounts(totals, portfolio, toss, strategies, now)
    output.setdefault("accounts", {}).update(accounts)
    totals_block = build_totals(output["accounts"], strategies, manual, kis_holdings, fx)
    output["totals"] = totals_block

    print("  Accounts: KIS W{:,.0f} + Upbit W{:,.0f} + Toss W{:,.0f}".format(
        accounts["kis"]["total_krw"], accounts["upbit"]["total_krw"],
        (accounts.get("toss") or {}).get("total_krw", 0)))
    print("  TOTAL INVESTMENTS W{:,.0f} = bots W{:,.0f} ({}%) + hands-on W{:,.0f} ({}%) + cash W{:,.0f} ({}%)".format(
        totals_block["investments_krw"], totals_block["bots_krw"], totals_block["split_pct"]["bots"],
        totals_block["manual_krw"], totals_block["split_pct"]["manual"],
        totals_block["cash_krw"], totals_block["split_pct"]["cash"]))
    if totals_block["unreported_bot_positions_krw"]:
        print("  NOTE: W{:+,.0f} of live bot positions are missing from their own cards "
              "(rescued via open_positions, so NOT counted as hands-on)".format(
                  totals_block["unreported_bot_positions_krw"]))
    if totals_block["reconciliation_warning"]:
        print("  WARNING: reconciliation gap W{:+,.0f} ({:.2f}% of investments) exceeds {}%".format(
            totals_block["reconciliation_gap_krw"], totals_block["reconciliation_gap_pct"],
            RECONCILIATION_WARN_PCT))
    else:
        print("  Reconciliation gap W{:+,.0f} ({:.3f}%) — OK".format(
            totals_block["reconciliation_gap_krw"], totals_block["reconciliation_gap_pct"]))

    out_dir = "/tmp/dashboard_dryrun" if dry_run else DATA_DIR
    os.makedirs(out_dir, exist_ok=True)

    data_file = os.path.join(out_dir, "dashboard_data.json")
    with open(data_file, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("  Wrote {}".format(data_file))

    eq_series = output.get("equity_series", {})
    if eq_series:
        eq_file = os.path.join(out_dir, "equity_history.json")
        with open(eq_file, "w") as f:
            json.dump(eq_series, f, indent=2)
        print("  Wrote {}".format(eq_file))

    print("[{}] Done.".format(datetime.now(KST).strftime("%H:%M:%S KST")))


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
