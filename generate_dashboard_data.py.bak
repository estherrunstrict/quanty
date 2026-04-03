#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate dashboard_data.json for GitHub Pages dashboard.
Reads all strategy result JSONs and compiles into a single dashboard file.

Run daily at 23:30 KST after all markets close.
"""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml

KST = ZoneInfo("Asia/Seoul")

# Paths
TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DASHBOARD_DIR, "docs", "data")
CONFIG_FILE = os.path.join(TRADING_DIR, "config.yaml")

# KIS API paths for live queries
import sys
sys.path.append(os.path.join(TRADING_DIR, "open-trading-api", "examples_llm"))
sys.path.append(os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "domestic_stock", "inquire_balance"))

STRATEGY_RESULT_FILES = {
    "quant40": os.path.join(TRADING_DIR, "strategy_results", "quant40_result.json"),
    "jd_strategy": os.path.join(TRADING_DIR, "strategy_results", "jd_strategy_result.json"),
    "modified_dual_momentum": os.path.join(TRADING_DIR, "strategy_results", "modified_dual_momentum_result.json"),
    "hybrid_vb_us": os.path.join(TRADING_DIR, "strategy_results", "hybrid_vb_us_result.json"),
    "hybrid_vb_kr": os.path.join(TRADING_DIR, "strategy_results", "hybrid_vb_kr_result.json"),
    "korea_momentum": os.path.join(TRADING_DIR, "strategy_results", "korea_etf_momentum_result.json"),
}

STATE_FILES = {
    "korea_momentum": os.path.join(TRADING_DIR, "korea_etf_momentum_state.json"),
    "hybrid_vb": os.path.join(TRADING_DIR, "hybrid_vb_state.json"),
    "btc_vb": os.path.join(TRADING_DIR, "upbit_jd_strategy_state.json"),
}

EXCHANGE_RATE = 1380  # KRW/USD fallback


def load_json(path):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def build_strategy_entry(name, market, currency, budget, result_json, extra_params=None):
    """Build a single strategy entry for the dashboard."""
    entry = {
        "name": name,
        "market": market,
        "currency": currency,
        "budget": budget,
        "total_value": budget,
        "cash": budget,
        "profit_pct": 0.0,
        "status": "NO DATA",
        "regime": {},
        "holdings": [],
        "params": extra_params or {},
    }

    if result_json is None:
        return entry

    # Holdings value = sum of individual holding values (always strategy-specific)
    holdings_value = sum(h.get("value", 0) for h in result_json.get("holdings", []))

    # Total profit = from result JSON (includes both realized + unrealized P/L)
    total_profit = result_json.get("total_profit", 0)

    # Strategy value = budget + profit (what you'd have if you liquidated now)
    # Cash = budget portion not currently in positions
    if budget > 0:
        entry["total_value"] = budget + total_profit
        # Estimate cash: the part of budget not in holdings
        entry["cash"] = max(0, budget + total_profit - holdings_value)
    else:
        entry["total_value"] = holdings_value
        entry["cash"] = 0

    entry["profit_pct"] = round(total_profit / budget * 100, 2) if budget > 0 else 0

    # Holdings — include profit field for P/L calculation
    holdings = []
    for h in result_json.get("holdings", []):
        if h.get("quantity", 0) > 0:
            holdings.append({
                "ticker": h.get("ticker", ""),
                "quantity": h.get("quantity", 0),
                "profit_rate": h.get("profit_rate", 0),
                "profit": h.get("profit", 0),
                "value": h.get("value", 0),
            })
    entry["holdings"] = holdings

    # Session summary
    session = result_json.get("session_summary", {})
    regime = session.get("regime", {})
    entry["regime"] = regime if isinstance(regime, dict) else {}

    # Status
    n_pos = session.get("open_positions_count", len(holdings))
    if n_pos > 0:
        entry["status"] = f"{n_pos} position{'s' if n_pos > 1 else ''}"
    else:
        entry["status"] = "CASH"

    return entry


def get_live_kr_holdings():
    """Query KIS API for live Korea holdings with real-time P/L."""
    try:
        import kis_auth as ka
        import inquire_balance as kr_balance

        ka.auth(svr="prod")
        acct = ka.getTREnv()
        output1, _ = kr_balance.inquire_balance(
            env_dv="real", cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            afhr_flpr_yn="N", inqr_dvsn="02", unpr_dvsn="01",
            fund_sttl_icld_yn="N", fncg_amt_auto_rdpt_yn="N", prcs_dvsn="00"
        )
        holdings = {}
        if output1 is not None and not output1.empty:
            for _, row in output1.iterrows():
                t = row['pdno']
                qty = int(row['hldg_qty'])
                if qty > 0:
                    holdings[t] = {
                        'ticker': t,
                        'quantity': qty,
                        'value': float(row['evlu_amt']),
                        'avg_price': float(row.get('pchs_avg_pric', 0)),
                        'profit': float(row.get('evlu_pfls_amt', 0)),
                        'profit_rate': float(row.get('evlu_pfls_rt', 0)),
                    }
        return holdings
    except Exception as e:
        print(f"KIS live query failed: {e}")
        return None


def build_korea_momentum_entry(config):
    """Build Korea ETF Momentum entry with LIVE KIS data for accurate P/L."""
    budget = config.get("capital_management", {}).get("korea_etf_momentum", {}).get("budget_krw", 10000000)
    kr_tickers = ["139220", "144600", "132030"]

    # Try live KIS query first (most accurate, real-time P/L)
    live = get_live_kr_holdings()
    if live:
        total_profit = 0
        holdings_list = []
        for t in kr_tickers:
            if t in live:
                h = live[t]
                total_profit += h['profit']
                holdings_list.append({
                    'ticker': t, 'quantity': h['quantity'],
                    'profit_rate': h['profit_rate'],
                })

        state = load_json(STATE_FILES.get("korea_momentum"))
        target = state.get("target_ticker", "") if state else ""

        return {
            "name": "Korea ETF Momentum",
            "market": "KR",
            "currency": "KRW",
            "budget": budget,
            "total_value": budget + total_profit,
            "cash": max(0, budget + total_profit - sum(live[t]['value'] for t in kr_tickers if t in live)),
            "profit_pct": round(total_profit / budget * 100, 2) if budget > 0 else 0,
            "status": f"HOLD {target}" if target and target != "CASH" else "CASH",
            "regime": {},
            "holdings": holdings_list,
            "params": {"strategy": "dual_momentum", "rebalance": "monthly"},
        }

    # Fallback: result JSON
    result = load_json(STRATEGY_RESULT_FILES.get("korea_momentum"))
    if result:
        entry = build_strategy_entry(
            "Korea ETF Momentum", "KR", "KRW", budget, result,
            {"strategy": "dual_momentum", "rebalance": "monthly"}
        )
        session = result.get("session_summary", {})
        target = session.get("target_ticker")
        if target and target != "CASH":
            entry["status"] = f"HOLD {target}"
        else:
            entry["status"] = "CASH"
        return entry

    # Fallback: state file only (no P/L)
    state = load_json(STATE_FILES.get("korea_momentum"))
    entry = {
        "name": "Korea ETF Momentum",
        "market": "KR",
        "currency": "KRW",
        "budget": budget,
        "total_value": budget,
        "cash": 0,
        "profit_pct": 0.0,
        "status": "NO DATA",
        "regime": {},
        "holdings": [],
        "params": {"strategy": "dual_momentum", "rebalance": "monthly"},
    }
    if state:
        target = state.get("target_ticker")
        if target and target != "CASH":
            entry["status"] = f"HOLD {target}"
        else:
            entry["status"] = "CASH"
    return entry


def get_upbit_balance():
    """Query Upbit account for KRW cash and BTC holdings."""
    try:
        import pyupbit
        env = _load_env()
        access = env.get("UPBIT_ACCESS_KEY")
        secret = env.get("UPBIT_SECRET_KEY")
        if not access or not secret:
            return 0, 0, 0  # krw, btc_krw_value, btc_qty

        upbit = pyupbit.Upbit(access, secret)
        krw = float(upbit.get_balance("KRW") or 0)
        btc_qty = float(upbit.get_balance("BTC") or 0)
        btc_price = float(pyupbit.get_current_price("KRW-BTC") or 0)
        btc_krw = btc_qty * btc_price
        return krw, btc_krw, btc_qty
    except Exception as e:
        print(f"Upbit query failed: {e}")
        return 0, 0, 0


def _load_env():
    env_path = os.path.join(TRADING_DIR, ".env")
    env = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    env[key.strip()] = val.strip()
    return env


def build_btc_vb_entry(config):
    """Build BTC VB entry from Upbit account balance."""
    state = load_json(STATE_FILES.get("btc_vb"))

    # Query actual Upbit account balance
    krw_cash, btc_value, btc_qty = get_upbit_balance()
    total_value = krw_cash + btc_value
    # Budget = total account value (user deposits/withdraws to adjust)
    budget = total_value if total_value > 0 else 0

    entry = {
        "name": "BTC VB",
        "market": "KR",
        "currency": "KRW",
        "budget": round(budget),
        "total_value": round(total_value),
        "cash": round(krw_cash),
        "profit_pct": 0.0,
        "status": "CASH",
        "regime": {},
        "holdings": [],
        "params": {"strategy": "upbit_vb", "k_long": 0.4},
    }

    # BTC position
    if btc_qty > 0 and btc_value > 0:
        entry["holdings"] = [{
            "ticker": "BTC",
            "quantity": round(btc_qty, 8),
            "profit_rate": 0,  # No avg price available from simple balance query
        }]
        entry["status"] = "HOLDING BTC"
    else:
        entry["status"] = "CASH"

    if state:
        regime = state.get("market_regime", "neutral")
        entry["regime"] = {"BTC": regime.upper()} if regime else {}
        market_state = state.get("current_state", "normal").upper()
        if btc_qty <= 0:
            entry["status"] = market_state

    return entry


def generate_dashboard_data():
    # Refresh realized P/L from KIS transaction history (live query)
    try:
        from refresh_realized_pl import refresh
        refresh()
    except Exception as e:
        print(f"Realized P/L refresh failed: {e}")

    config = load_config()
    cap_mgmt = config.get("capital_management", {})

    strategies = []

    # Load per-strategy realized P/L from KIS transaction history
    realized_pl_file = os.path.join(TRADING_DIR, "strategy_results", "realized_pl_2026.json")
    realized_by_strategy = load_json(realized_pl_file) or {}

    # Korea ETF Momentum (no result JSON — uses state file)
    strategies.append(build_korea_momentum_entry(config))

    # Hybrid VB KR
    hybrid_kr_result = load_json(STRATEGY_RESULT_FILES.get("hybrid_vb_kr"))
    budget_kr = cap_mgmt.get("hybrid_vb_kr", {}).get("budget_krw", 5000000)
    strategies.append(build_strategy_entry(
        "Hybrid VB (KR)", "KR", "KRW", budget_kr, hybrid_kr_result,
        {"strategy": "VB + Vol-Managed", "vol_managed": True}
    ))

    # Hybrid VB US
    hybrid_us_result = load_json(STRATEGY_RESULT_FILES.get("hybrid_vb_us"))
    budget_us = cap_mgmt.get("hybrid_vb_us", {}).get("budget_usd", 5000)
    strategies.append(build_strategy_entry(
        "Hybrid VB (US)", "US", "USD", budget_us, hybrid_us_result,
        {"strategy": "VB Only", "vol_managed": False}
    ))

    # Quant40
    q40_result = load_json(STRATEGY_RESULT_FILES.get("quant40"))
    budget_q40 = cap_mgmt.get("quant40", {}).get("budget_usd", 25165)
    strategies.append(build_strategy_entry(
        "Quant40", "US", "USD", budget_q40, q40_result,
        {"strategy": "quant_replacing_40"}
    ))

    # JD Strategy
    jd_result = load_json(STRATEGY_RESULT_FILES.get("jd_strategy"))
    budget_jd = cap_mgmt.get("jd_strategy", {}).get("budget_usd", 2796)
    strategies.append(build_strategy_entry(
        "JD Strategy", "US", "USD", budget_jd, jd_result,
        {"strategy": "jd_investment"}
    ))

    # Modified Dual Momentum (변형 듀얼모멘텀)
    mdm_result = load_json(STRATEGY_RESULT_FILES.get("modified_dual_momentum"))
    budget_mdm = cap_mgmt.get("modified_dual_momentum", {}).get("budget_usd", 5000)
    strategies.append(build_strategy_entry(
        "Modified Dual Momentum", "US", "USD", budget_mdm, mdm_result,
        {"strategy": "dual_momentum_modified", "offensive": "SPY vs EFA", "defensive": "8 bond ETFs"}
    ))

    # BTC VB
    strategies.append(build_btc_vb_entry(config))

    # Query live KIS account data (needed for both per-strategy and account-level P/L)
    acct = None
    try:
        from query_account_total import get_account_totals
        acct = get_account_totals()
    except Exception as e:
        print(f"KIS account query failed: {e}")

    # Per-strategy P/L: realized from KIS trades, unrealized from KIS holdings
    # Use KIS evlu_pfls_amt for KR (in KRW), and holdings profit for US (in USD, keep as USD)
    # Don't try to convert — just show each in its own currency and sum only at account level

    # Get live holdings unrealized from KIS
    kr_holdings_profit = {}  # {ticker: profit_krw}
    us_holdings_profit = {}  # {ticker: profit_usd}
    if acct:
        try:
            for h in acct['kr']['holdings']:
                kr_holdings_profit[h['ticker']] = h['profit']
        except Exception:
            pass
        try:
            for h in acct['us']['holdings']:
                us_holdings_profit[h['ticker']] = h['profit']
        except Exception:
            pass

    # Map tickers to strategies (same as refresh_realized_pl.py)
    TICKER_STRAT = {
        'SPY': 'Quant40', 'IEF': 'Quant40', 'SH': 'Quant40',
        'NVDA': 'JD Strategy', 'AAPL': 'JD Strategy', 'MSFT': 'JD Strategy',
        'GOOGL': 'JD Strategy', 'AMZN': 'JD Strategy',
        'EFA': 'Modified Dual Momentum', 'SHY': 'Modified Dual Momentum',
        'TLT': 'Modified Dual Momentum', 'TIP': 'Modified Dual Momentum',
        'LQD': 'Modified Dual Momentum', 'HYG': 'Modified Dual Momentum',
        'BWX': 'Modified Dual Momentum', 'EMB': 'Modified Dual Momentum',
        'GLD': 'Hybrid VB (US)', 'SLV': 'Hybrid VB (US)', 'GLTR': 'Hybrid VB (US)',
        'GDX': 'Hybrid VB (US)', 'USO': 'Hybrid VB (US)', 'URA': 'Hybrid VB (US)',
        'FTGC': 'Hybrid VB (US)', 'CPER': 'Hybrid VB (US)', 'DBA': 'Hybrid VB (US)',
        'SIL': 'Hybrid VB (US)',
        '139220': 'Korea ETF Momentum', '144600': 'Korea ETF Momentum',
        '132030': 'Korea ETF Momentum',
        '069500': 'Hybrid VB (KR)', '229200': 'Hybrid VB (KR)',
        '305720': 'Hybrid VB (KR)', '091170': 'Hybrid VB (KR)',
        '364690': 'Hybrid VB (KR)',
    }

    # Aggregate unrealized per strategy from live KIS data
    strat_unrealized_krw = {}
    for ticker, profit_krw in kr_holdings_profit.items():
        strat = TICKER_STRAT.get(ticker, 'Unknown')
        strat_unrealized_krw[strat] = strat_unrealized_krw.get(strat, 0) + profit_krw

    for ticker, profit_usd in us_holdings_profit.items():
        strat = TICKER_STRAT.get(ticker, 'Unknown')
        # Use KIS settlement rate: get from the realized trades data if available
        # Fallback: use EXCHANGE_RATE
        strat_unrealized_krw[strat] = strat_unrealized_krw.get(strat, 0) + profit_usd * EXCHANGE_RATE

    for s in strategies:
        name = s["name"]
        realized_krw = realized_by_strategy.get(name, 0)
        unrealized_krw = strat_unrealized_krw.get(name, 0)

        s["realized_pl_krw"] = round(realized_krw)
        s["unrealized_pl_krw"] = round(unrealized_krw)
        s["total_pl_krw"] = round(realized_krw + unrealized_krw)

        budget_krw = s["budget"] * EXCHANGE_RATE if s["currency"] == "USD" else s["budget"]
        if budget_krw > 0:
            s["total_pl_pct"] = round(s["total_pl_krw"] / budget_krw * 100, 2)
        else:
            s["total_pl_pct"] = 0

    # Account-level totals: KIS 계좌총자산
    # = KR stocks + US stocks (KRW) + KRW cash (ONCE) + USD cash (KRW)
    # KRW 예수금 is SHARED — must not double-count between KR and US
    try:
        if acct is None:
            from query_account_total import get_account_totals
            acct = get_account_totals()
        kr_tot_evlu = acct['kr']['kr_tot_evlu']   # 예수금 + KR stocks
        us_stock_usd = acct['us']['stock_value']
        usd_cash = acct['usd_cash']
        kr_cash_real = acct['kr']['cash']        # 예수금

        # 계좌총자산: KIS app uses its own FX rate (higher than market rate)
        # We derive the effective rate from: app_total - kr_portion = US portion in KRW
        # Then: US_KRW / US_USD = effective_rate
        # But we don't have the app total at runtime. Use yfinance for live FX rate:
        try:
            import yfinance as yf
            fx = yf.download('KRW=X', period='5d', progress=False)
            if isinstance(fx.columns, pd.MultiIndex):
                fx.columns = fx.columns.get_level_values(0)
            live_rate = float(fx['Close'].iloc[-1])
        except Exception:
            live_rate = EXCHANGE_RATE

        us_cash_real = usd_cash
        kis_total_assets = kr_tot_evlu + (us_stock_usd + usd_cash) * live_rate
    except Exception as e:
        print(f"KIS account query failed: {e}")
        kr_cash_real = 0
        us_cash_real = 0
        kis_total_assets = sum(
            s["total_value"] if s["currency"] == "KRW" else s["total_value"] * EXCHANGE_RATE
            for s in strategies
        )

    # Original deposit from deposits.json
    deposit_file = os.path.join(DASHBOARD_DIR, "deposits.json")
    deposits = load_json(deposit_file) or {"kis_total_original_krw": kis_total_assets}

    original_krw = deposits.get("kis_total_original_krw", kis_total_assets)

    # Realized P/L: read from live KIS query (generated by each dashboard update)
    realized_pl_file = os.path.join(TRADING_DIR, "strategy_results", "realized_pl_2026.json")
    realized_data = load_json(realized_pl_file) or {}
    realized_loss = realized_data.get("_total", deposits.get("kis_realized_loss_krw", 0))

    # Total P/L = realized (from deposits.json, updated periodically) + unrealized (live from API)
    # Unrealized is always in KRW from KR API; US unrealized needs FX conversion
    try:
        kr_unrealized = acct['kr']['unrealized_pl']
        # For US unrealized: convert USD P/L to KRW using the same rate we used for total
        us_unrealized_usd = acct['us']['unrealized_pl']
        # Use the rate implied by KIS app: we know original and realized, so
        # current_total = original + realized + unrealized
        # We just need unrealized in KRW — use a reasonable live rate
        us_unrealized_krw = us_unrealized_usd * (live_rate if 'live_rate' in dir() else EXCHANGE_RATE)
        total_unrealized = kr_unrealized + us_unrealized_krw
    except Exception:
        total_unrealized = 0

    total_pl_krw = realized_loss + int(total_unrealized)
    total_value_krw = original_krw + total_pl_krw
    total_pl_pct = (total_pl_krw / original_krw * 100) if original_krw > 0 else 0

    now = datetime.now(KST)
    dashboard = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "exchange_rate": EXCHANGE_RATE,
        "portfolio": {
            "total_value_krw": round(total_value_krw),
            "original_deposit_krw": round(original_krw),
            "total_profit_krw": round(total_pl_krw),
            "total_profit_pct": round(total_pl_pct, 2),
            "cash_krw": round(kr_cash_real),
            "cash_usd": round(us_cash_real),
        },
        "strategies": strategies,
    }

    # Write dashboard_data.json
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "dashboard_data.json")
    with open(out_path, 'w') as f:
        json.dump(dashboard, f, indent=2, ensure_ascii=False, default=str)
    print(f"Written: {out_path}")

    # Append to equity_history.json
    history_path = os.path.join(DATA_DIR, "equity_history.json")
    history = load_json(history_path) or {"dates": [], "strategies": {}}

    today = now.strftime("%Y-%m-%d")
    if today not in history["dates"]:
        history["dates"].append(today)

        for s in strategies:
            key = s["name"].lower().replace(" ", "_").replace("(", "").replace(")", "")
            if key not in history["strategies"]:
                # Initialize with 100 base
                history["strategies"][key] = [100.0] * (len(history["dates"]) - 1) + [100.0]
            else:
                # Calculate normalized value
                base_val = s["budget"] if s["budget"] > 0 else s["total_value"]
                if base_val > 0:
                    normalized = (s["total_value"] / base_val) * 100
                else:
                    normalized = history["strategies"][key][-1] if history["strategies"][key] else 100.0
                history["strategies"][key].append(round(normalized, 2))

        with open(history_path, 'w') as f:
            json.dump(history, f, indent=2)
        print(f"Appended equity history for {today}")
    else:
        print(f"Equity history already has {today}, skipping")


if __name__ == "__main__":
    generate_dashboard_data()
