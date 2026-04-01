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

STRATEGY_RESULT_FILES = {
    "quant40": os.path.join(TRADING_DIR, "strategy_results", "quant40_result.json"),
    "jd_strategy": os.path.join(TRADING_DIR, "strategy_results", "jd_strategy_result.json"),
    "uranium_vb": os.path.join(TRADING_DIR, "strategy_results", "uranium_vb_result.json"),
    "hybrid_vb_us": os.path.join(TRADING_DIR, "strategy_results", "hybrid_vb_us_result.json"),
    "hybrid_vb_kr": os.path.join(TRADING_DIR, "strategy_results", "hybrid_vb_kr_result.json"),
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

    # Strategy-level value: use allocated_capital (budget-capped) not account-level total_value
    allocated = result_json.get("allocated_capital", budget)
    raw_value = result_json.get("total_value", budget)
    # If total_value is way larger than budget, it's the account total — use allocated instead
    if budget > 0 and raw_value > budget * 3:
        entry["total_value"] = allocated
    else:
        entry["total_value"] = raw_value

    # Cash: compute strategy's share (total cash * budget/account_value), or use allocated - holdings
    raw_cash = result_json.get("cash_balance", 0)
    holdings_value = sum(h.get("value", 0) for h in result_json.get("holdings", []))
    if budget > 0 and raw_cash > budget * 3:
        # Account-level cash — estimate strategy's cash as budget - holdings
        entry["cash"] = max(0, allocated - holdings_value)
    else:
        entry["cash"] = raw_cash

    total_profit = result_json.get("total_profit", 0)
    entry["profit_pct"] = round(total_profit / budget * 100, 2) if budget > 0 else 0

    # Holdings
    holdings = []
    for h in result_json.get("holdings", []):
        if h.get("quantity", 0) > 0:
            holdings.append({
                "ticker": h.get("ticker", ""),
                "quantity": h.get("quantity", 0),
                "profit_rate": h.get("profit_rate", 0),
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


def build_korea_momentum_entry(config):
    """Build Korea ETF Momentum entry from state file."""
    budget = config.get("capital_management", {}).get("korea_etf_momentum", {}).get("budget_krw", 10000000)
    state = load_json(STATE_FILES["korea_momentum"])

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
        target_name = state.get("target_name", "")
        if target and target != "CASH":
            entry["status"] = f"HOLD {target}"
            entry["holdings"] = [{"ticker": target, "quantity": 1, "profit_rate": 0}]
        else:
            entry["status"] = "CASH"

    return entry


def build_btc_vb_entry(config):
    """Build BTC VB entry from state."""
    entry = {
        "name": "BTC VB",
        "market": "KR",
        "currency": "KRW",
        "budget": 0,
        "total_value": 0,
        "cash": 0,
        "profit_pct": 0.0,
        "status": "CASH",
        "regime": {},
        "holdings": [],
        "params": {"strategy": "upbit_vb", "k_long": 0.4},
    }
    # BTC VB state is complex — just show basic status
    return entry


def generate_dashboard_data():
    config = load_config()
    cap_mgmt = config.get("capital_management", {})

    strategies = []

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

    # Uranium VB
    uvb_result = load_json(STRATEGY_RESULT_FILES.get("uranium_vb"))
    budget_uvb = cap_mgmt.get("uranium_vb", {}).get("budget_usd", 5000)
    strategies.append(build_strategy_entry(
        "Uranium VB", "US", "USD", budget_uvb, uvb_result,
        {"strategy": "vb_momentum"}
    ))

    # BTC VB
    strategies.append(build_btc_vb_entry(config))

    # Portfolio totals
    total_krw = 0
    total_usd = 0
    cash_krw = 0
    cash_usd = 0

    for s in strategies:
        if s["currency"] == "KRW":
            total_krw += s["total_value"]
            cash_krw += s["cash"]
        else:
            total_usd += s["total_value"]
            cash_usd += s["cash"]

    total_value_krw = total_krw + total_usd * EXCHANGE_RATE
    total_budget_krw = sum(
        s["budget"] if s["currency"] == "KRW" else s["budget"] * EXCHANGE_RATE
        for s in strategies
    )
    total_profit_pct = ((total_value_krw / total_budget_krw) - 1) * 100 if total_budget_krw > 0 else 0

    now = datetime.now(KST)
    dashboard = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "exchange_rate": EXCHANGE_RATE,
        "portfolio": {
            "total_value_krw": round(total_value_krw),
            "total_value_usd": round(total_usd),
            "total_profit_pct": round(total_profit_pct, 1),
            "cash_krw": round(cash_krw),
            "cash_usd": round(cash_usd),
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
