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


def get_portfolio():
    """Query KIS API for real account totals + Upbit."""
    try:
        from query_account_total import get_account_totals
        totals = get_account_totals()

        kr = totals["kr"]
        us = totals["us"]
        krw_cash = totals["krw_cash"]       # Shared KRW cash — count once
        usd_cash = totals["usd_cash"]       # USD cash in USD

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

        # Total KIS assets = KR stocks + KRW cash (once) + US stocks (KRW) + USD cash (KRW)
        kis_total = (kr["stock_value"] + krw_cash
                     + us["stock_value"] * exchange_rate
                     + usd_cash * exchange_rate)

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
            "upbit_krw": round(upbit_equity),
            "exchange_rate": exchange_rate,
        }
    except Exception as e:
        print("  Portfolio query failed: {}".format(e))
        import traceback
        traceback.print_exc()
        return {}


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

    # Get real account totals from KIS API
    portfolio = get_portfolio()
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
