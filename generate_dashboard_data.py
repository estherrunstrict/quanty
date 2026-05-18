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


def main():
    now = datetime.now(KST)
    print("[{}] Generating dashboard data...".format(now.strftime("%H:%M:%S KST")))

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
        from query_account_total import get_account_totals
        totals = get_account_totals()
    except Exception as e:
        print("  WARNING: KIS totals query failed; falling back to bot-state values: {}".format(e))

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

    # Merge
    output = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "portfolio": portfolio,
    }
    output.update(api_data)

    os.makedirs(DATA_DIR, exist_ok=True)

    data_file = os.path.join(DATA_DIR, "dashboard_data.json")
    with open(data_file, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("  Wrote {}".format(data_file))

    eq_series = api_data.get("equity_series", {})
    if eq_series:
        eq_file = os.path.join(DATA_DIR, "equity_history.json")
        with open(eq_file, "w") as f:
            json.dump(eq_series, f, indent=2)
        print("  Wrote {}".format(eq_file))

    print("[{}] Done.".format(datetime.now(KST).strftime("%H:%M:%S KST")))


if __name__ == "__main__":
    main()
