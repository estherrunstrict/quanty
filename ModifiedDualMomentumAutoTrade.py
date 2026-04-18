# -*- coding: utf-8 -*-
"""
Modified Dual Momentum Auto-Trader (변형 듀얼모멘텀)
=====================================================
Based on 강환국's modified dual momentum strategy.

Offensive: SPY vs EFA — buy whichever has higher 12-month return.
Defensive: If SPY 12m return < 0, switch to bond rotation:
           Top 3 of 8 bond ETFs by 6-month return.
           Any of the 3 with 6m return < 0 → that portion goes to cash.

Rebalance: Monthly (first trading day of each month).
Backtest: CAGR 18.3%, MDD -16.3% (53 years, original study).

Usage:
  python ModifiedDualMomentumAutoTrade.py
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
import yaml
import logging
import os
import time
import json
import yfinance as yf

# --- Path setup ---
current_dir = os.path.dirname(__file__)
project_root = os.path.abspath(current_dir)
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_psamount'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'order'))

import kis_auth as ka
import order
import inquire_balance
import inquire_psamount

from discord_notifier import DiscordNotifier

# --- Logger ---
logger = logging.getLogger('ModifiedDualMomentum')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(os.path.join(project_root, 'modified_dual_momentum.log'))
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

STATE_FILE = os.path.join(project_root, 'modified_dual_momentum_state.json')

# --- ETF Universe ---
OFFENSIVE_ETFS = {
    'SPY': {'name': 'S&P 500', 'exchange': 'AMEX'},
    'EFA': {'name': 'MSCI EAFE (Global ex-US)', 'exchange': 'AMEX'},
}

DEFENSIVE_ETFS = {
    'SHY': {'name': 'US Short-Term Treasury', 'exchange': 'AMEX'},
    'IEF': {'name': 'US Intermediate Treasury', 'exchange': 'AMEX'},
    'TLT': {'name': 'US Long-Term Treasury', 'exchange': 'AMEX'},
    'TIP': {'name': 'US TIPS', 'exchange': 'AMEX'},
    'LQD': {'name': 'US Corporate Bond', 'exchange': 'AMEX'},
    'HYG': {'name': 'US High Yield', 'exchange': 'AMEX'},
    'BWX': {'name': 'International Treasury', 'exchange': 'AMEX'},
    'EMB': {'name': 'Emerging Market Bond', 'exchange': 'AMEX'},
}

ALL_TICKERS = list(OFFENSIVE_ETFS.keys()) + list(DEFENSIVE_ETFS.keys())
EXCHANGE_MAP = {}
for d in [OFFENSIVE_ETFS, DEFENSIVE_ETFS]:
    for t, info in d.items():
        EXCHANGE_MAP[t] = info['exchange']


# =============================================================================
# CONFIG / STATE
# =============================================================================

def _load_env_file():
    env_path = os.path.join(project_root, '.env')
    env = {}
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    env[key.strip()] = val.strip()
    return env


def load_config():
    with open(os.path.join(project_root, 'config.yaml'), 'r') as f:
        return yaml.safe_load(f)


def load_allocation_override():
    """Read allocation multiplier from SecondLevelBot."""
    override_path = os.path.join(project_root, 'allocation_override.json')
    try:
        if os.path.exists(override_path):
            with open(override_path) as f:
                data = json.load(f)
            ts = data.get('timestamp', '')
            if ts:
                from zoneinfo import ZoneInfo
                override_time = datetime.fromisoformat(ts)
                now = datetime.now(override_time.tzinfo or ZoneInfo("America/New_York"))
                if (now - override_time).total_seconds() / 3600 > 2:
                    logger.warning("Second Level override stale — using default 1.0")
                    return 1.0
            mult = data.get('allocation_multiplier', 1.0)
            regime = data.get('regime', 'NEUTRAL')
            logger.info(f"Second Level override: {regime} → allocation {mult:.0%}")
            return mult
    except Exception as e:
        logger.warning(f"Failed to read allocation override: {e}")
    return 1.0


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"State file error: {e}")
    return {
        'last_rebalance_month': None,
        'current_positions': {},  # {ticker: {shares, value}}
        'mode': None,  # 'offensive' or 'defensive'
        'trade_history': [],
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# =============================================================================
# MARKET DATA
# =============================================================================

def fetch_monthly_prices(ticker, months=14):
    """Fetch monthly close prices via yfinance."""
    try:
        days = months * 30 + 30
        df = yf.download(ticker, period=f"{days}d", auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        monthly = df['Close'].resample('ME').last().dropna()
        return monthly
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return None


# =============================================================================
# STRATEGY LOGIC
# =============================================================================

def compute_targets(monthly_prices):
    """
    Modified Dual Momentum signal.

    Returns: (mode, targets)
      mode: 'offensive' or 'defensive'
      targets: {ticker: weight} where weights sum to 1.0 (or less if cash)
    """
    # Check SPY 12-month return
    spy_prices = monthly_prices.get('SPY')
    efa_prices = monthly_prices.get('EFA')

    if spy_prices is None or len(spy_prices) < 13:
        logger.error("Insufficient SPY data")
        return 'defensive', {'CASH': 1.0}

    spy_12m = (spy_prices.iloc[-1] / spy_prices.iloc[-13]) - 1
    logger.info(f"SPY 12m return: {spy_12m * 100:+.1f}%")

    # OFFENSIVE: SPY 12m return > 0
    if spy_12m > 0:
        if efa_prices is not None and len(efa_prices) >= 13:
            efa_12m = (efa_prices.iloc[-1] / efa_prices.iloc[-13]) - 1
            logger.info(f"EFA 12m return: {efa_12m * 100:+.1f}%")

            if spy_12m >= efa_12m:
                logger.info(f"OFFENSIVE: SPY wins ({spy_12m * 100:+.1f}% vs {efa_12m * 100:+.1f}%)")
                return 'offensive', {'SPY': 1.0}
            else:
                logger.info(f"OFFENSIVE: EFA wins ({efa_12m * 100:+.1f}% vs {spy_12m * 100:+.1f}%)")
                return 'offensive', {'EFA': 1.0}
        else:
            return 'offensive', {'SPY': 1.0}

    # DEFENSIVE: SPY 12m return <= 0
    logger.info("SPY 12m return <= 0 → DEFENSIVE mode (bond rotation)")

    # Rank 8 bond ETFs by 6-month return, pick top 3
    bond_returns = {}
    for ticker in DEFENSIVE_ETFS:
        prices = monthly_prices.get(ticker)
        if prices is not None and len(prices) >= 7:
            ret_6m = (prices.iloc[-1] / prices.iloc[-7]) - 1
            bond_returns[ticker] = ret_6m
            logger.info(f"  {ticker} ({DEFENSIVE_ETFS[ticker]['name']}): 6m return {ret_6m * 100:+.1f}%")

    if not bond_returns:
        return 'defensive', {'CASH': 1.0}

    # Sort by return, take top 3
    sorted_bonds = sorted(bond_returns.items(), key=lambda x: x[1], reverse=True)
    top3 = sorted_bonds[:3]

    targets = {}
    cash_weight = 0.0
    for ticker, ret in top3:
        if ret > 0:
            targets[ticker] = 1.0 / 3.0
        else:
            # 6m return <= 0 → this portion goes to cash
            cash_weight += 1.0 / 3.0
            logger.info(f"  {ticker} has negative 6m return → portion goes to CASH")

    if cash_weight > 0:
        targets['CASH'] = cash_weight

    logger.info(f"DEFENSIVE targets: {targets}")
    return 'defensive', targets


# =============================================================================
# KIS API HELPERS
# =============================================================================

def get_holdings(my_acct, my_prod):
    """Get current holdings across AMEX and NASD."""
    holdings = {}
    for excg in ['AMEX', 'NASD']:
        try:
            output1, _ = inquire_balance.inquire_balance(
                cano=my_acct, acnt_prdt_cd=my_prod,
                ovrs_excg_cd=excg, tr_crcy_cd="USD", env_dv="real"
            )
            if output1 is not None and not output1.empty:
                for _, row in output1.iterrows():
                    t = row['ovrs_pdno']
                    if t in ALL_TICKERS:
                        qty = int(row['ovrs_cblc_qty'])
                        holdings[t] = {
                            'quantity': qty,
                            'avg_price': float(row.get('frcr_pchs_amt1', 0)) / max(qty, 1),
                            'value': float(row.get('ovrs_stck_evlu_amt', 0)),
                        }
        except Exception as e:
            logger.error(f"Failed to get holdings ({excg}): {e}")
    return holdings


def get_cash_usd(my_acct, my_prod):
    try:
        df = inquire_psamount.inquire_psamount(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd="AMEX", item_cd="SPY",
            ovrs_ord_unpr="0", env_dv="real"
        )
        if df is not None and not df.empty:
            return float(df.iloc[0]['ord_psbl_frcr_amt'])
    except Exception as e:
        logger.error(f"Failed to get cash: {e}")
    return 0.0


def place_order(side, my_acct, my_prod, ticker, quantity, price):
    logger.info(f"Placing {side.upper()}: {ticker} x {quantity} @ ~${price:.2f}")
    try:
        order_price = str(round(price * (1.01 if side == 'buy' else 0.99), 2))
        exchange = EXCHANGE_MAP.get(ticker, 'AMEX')
        df_order = order.order(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd=exchange, pdno=ticker,
            ord_qty=str(int(quantity)), ovrs_ord_unpr=order_price,
            ord_dv=side, ctac_tlno="", mgco_aptm_odno="",
            ord_svr_dvsn_cd="0", ord_dvsn="00", env_dv="real"
        )
        if df_order is not None and not df_order.empty:
            logger.info(f"{side.upper()} confirmed: {df_order.to_dict()}")
            return True
        logger.error(f"{side.upper()} returned empty/None — order likely rejected")
        return False
    except Exception as e:
        logger.error(f"{side.upper()} failed: {e}")
        return False


# =============================================================================
# REBALANCE LOGIC
# =============================================================================

def is_rebalance_day(state):
    """First trading day of the month (US market)."""
    now = datetime.now()
    current_month = now.strftime('%Y-%m')
    last = state.get('last_rebalance_month')

    if last == current_month:
        return False

    # First run ever
    if last is None:
        logger.info("First run — forcing initial rebalance")
        return True

    # Check if it's a weekday
    if now.weekday() >= 5:
        return False

    # Check if any earlier weekday this month
    for d in range(1, now.day):
        past = now.replace(day=d)
        if past.weekday() < 5:
            return False

    return True


def execute_rebalance(targets, my_acct, my_prod, holdings, cash, budget, paper_trading):
    """
    Rebalance to target weights.
    targets: {ticker: weight} (CASH weight means hold cash)
    """
    portfolio_value = cash + sum(h['value'] for h in holdings.values())
    capped = min(portfolio_value, budget) if budget > 0 else portfolio_value

    trades = []

    # Step 1: SELL positions not in targets
    for ticker, h in holdings.items():
        if ticker not in targets and h['quantity'] > 0:
            logger.info(f"SELL {ticker}: {h['quantity']} shares (not in target)")
            if not paper_trading:
                price = h['value'] / h['quantity'] if h['quantity'] > 0 else 0
                success = place_order("sell", my_acct, my_prod, ticker, h['quantity'], price)
                if not success:
                    logger.error(f"Failed to sell {ticker}")
                    continue
            trades.append({
                'action': 'SELL', 'ticker': ticker,
                'quantity': h['quantity'], 'value': h['value'],
            })

    # Step 2: Wait for sells to settle
    if trades and not paper_trading:
        time.sleep(5)
        cash = get_cash_usd(my_acct, my_prod)

    # Step 3: Reduce over-allocated positions
    for ticker, target_weight in targets.items():
        if ticker == 'CASH':
            continue
        if ticker in holdings:
            current_value = holdings[ticker]['value']
            target_value = capped * target_weight
            if current_value > target_value * 1.05:
                # Over-allocated — sell excess
                excess_value = current_value - target_value
                price = current_value / holdings[ticker]['quantity']
                sell_qty = int(excess_value / price)
                if sell_qty > 0:
                    logger.info(f"TRIM {ticker}: sell {sell_qty} shares (${excess_value:,.0f} excess)")
                    if not paper_trading:
                        place_order("sell", my_acct, my_prod, ticker, sell_qty, price)
                    trades.append({
                        'action': 'SELL', 'ticker': ticker,
                        'quantity': sell_qty, 'value': sell_qty * price,
                    })

    # Refresh cash after trims
    if trades and not paper_trading:
        time.sleep(5)
        cash = get_cash_usd(my_acct, my_prod)

    # Step 4: BUY under-allocated positions
    for ticker, target_weight in targets.items():
        if ticker == 'CASH':
            continue
        target_value = capped * target_weight
        current_value = holdings.get(ticker, {}).get('value', 0)
        if current_value < target_value * 0.95:
            buy_value = target_value - current_value
            # Get current price
            try:
                df = yf.download(ticker, period='5d', auto_adjust=True, progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                price = float(df['Close'].iloc[-1])
            except Exception:
                logger.error(f"Can't get price for {ticker}")
                continue

            buy_qty = int(min(buy_value, cash * 0.95) / price)
            if buy_qty > 0:
                logger.info(f"BUY {ticker}: {buy_qty} shares @ ~${price:.2f} (target ${target_value:,.0f})")
                if not paper_trading:
                    success = place_order("buy", my_acct, my_prod, ticker, buy_qty, price)
                    if not success:
                        logger.error(f"Failed to buy {ticker}")
                        continue
                cost = buy_qty * price
                cash -= cost
                trades.append({
                    'action': 'BUY', 'ticker': ticker,
                    'quantity': buy_qty, 'price': price,
                    'value': cost,
                })

    return trades


# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("=" * 60)
    logger.info("Modified Dual Momentum Auto-Trader - Start")
    logger.info("=" * 60)

    config = load_config()
    webhook_url = (config.get("DISCORD_WEBHOOK_URL")
                   or os.environ.get("DISCORD_WEBHOOK_URL")
                   or _load_env_file().get("DISCORD_WEBHOOK_URL"))
    notifier = DiscordNotifier(webhook_url, "Modified Dual Momentum")

    # Budget — read from capital_management (canonical), fallback to per-strategy config
    cap_mgmt = config.get('capital_management', {})
    mdm_config = config.get('modified_dual_momentum', {})
    base_budget = cap_mgmt.get('modified_dual_momentum', {}).get('budget_usd', 0) or mdm_config.get('budget_usd', 5000.0)
    sl_mult = load_allocation_override()
    budget = base_budget * sl_mult
    paper_trading = mdm_config.get('paper_trading', False)
    logger.info(f"Budget: ${base_budget:,.0f} × {sl_mult:.0%} = ${budget:,.0f}")

    # Auth
    try:
        ka.auth(svr="prod")
        logger.info("KIS API auth OK")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        notifier.send_error("Modified Dual Momentum Auth Failed", str(e))
        return

    acct = ka.getTREnv()
    my_acct, my_prod = acct.my_acct, acct.my_prod

    state = load_state()

    # Fetch monthly prices for all ETFs
    logger.info("Fetching monthly price data...")
    monthly_prices = {}
    for ticker in ALL_TICKERS:
        prices = fetch_monthly_prices(ticker, months=14)
        if prices is not None:
            monthly_prices[ticker] = prices
            logger.info(f"  {ticker}: {len(prices)} months")
        time.sleep(0.3)

    # Compute targets
    mode, targets = compute_targets(monthly_prices)
    logger.info(f"Mode: {mode.upper()}")
    logger.info(f"Targets: {targets}")

    # Get account state
    holdings = get_holdings(my_acct, my_prod)
    cash = get_cash_usd(my_acct, my_prod)
    portfolio_value = cash + sum(h['value'] for h in holdings.values())

    logger.info(f"Portfolio: ${portfolio_value:,.2f}, Cash: ${cash:,.2f}")
    for t, h in holdings.items():
        logger.info(f"  {t}: {h['quantity']} shares (${h['value']:,.2f})")

    # Check rebalance
    rebalance = is_rebalance_day(state)
    trades = []

    if rebalance:
        logger.info("=== REBALANCE DAY ===")
        trades = execute_rebalance(targets, my_acct, my_prod, holdings, cash, budget, paper_trading)

        # Only mark rebalance complete if at least one trade succeeded (or paper mode)
        if trades or paper_trading:
            state['last_rebalance_month'] = datetime.now().strftime('%Y-%m')
            state['mode'] = mode
            state['current_positions'] = {t: w for t, w in targets.items() if t != 'CASH'}
        else:
            logger.warning("No trades executed — will retry rebalance on next run")
    else:
        logger.info("Not a rebalance day — reporting only")

    save_state(state)

    # Build target description
    target_lines = []
    for t, w in sorted(targets.items(), key=lambda x: x[1], reverse=True):
        if t == 'CASH':
            target_lines.append(f"CASH: {w * 100:.0f}%")
        else:
            name = OFFENSIVE_ETFS.get(t, DEFENSIVE_ETFS.get(t, {})).get('name', t)
            target_lines.append(f"{t} ({name}): {w * 100:.0f}%")

    # Write strategy result JSON
    results_dir = os.path.join(project_root, 'strategy_results')
    os.makedirs(results_dir, exist_ok=True)

    # Refresh holdings post-trade
    if trades and not paper_trading:
        time.sleep(3)
        holdings = get_holdings(my_acct, my_prod)
        cash = get_cash_usd(my_acct, my_prod)
        portfolio_value = cash + sum(h['value'] for h in holdings.values())

    holdings_list = []
    total_profit = 0
    for t, h in holdings.items():
        holding_profit = h['value'] - (h['avg_price'] * h['quantity']) if h['quantity'] > 0 else 0
        total_profit += holding_profit
        holdings_list.append({
            'ticker': t, 'quantity': h['quantity'], 'value': h['value'],
            'avg_price': h['avg_price'], 'profit': round(holding_profit, 2),
            'currency': 'USD',
        })

    result_data = {
        'strategy_name': 'MODIFIED_DUAL_MOMENTUM',
        'timestamp': datetime.now().isoformat(),
        'allocation_mode': 'fixed',
        'capital_allocation': budget,
        'allocated_capital': budget,
        'total_value': portfolio_value,
        'cash_balance': cash,
        'total_profit': round(total_profit, 2),
        'holdings': holdings_list,
        'session_summary': {
            'mode': mode,
            'targets': targets,
            'rebalance_day': rebalance,
            'orders_executed': len(trades),
            'paper_trading': paper_trading,
        },
    }

    result_file = os.path.join(results_dir, 'modified_dual_momentum_result.json')
    try:
        tmp = result_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(result_data, f, indent=2, default=str)
        os.replace(tmp, result_file)
    except Exception as e:
        logger.error(f"Failed to write result: {e}")

    # Discord
    mode_emoji = "\U0001f7e2" if mode == 'offensive' else "\U0001f7e1"
    notifier._send(
        title=f"{mode_emoji} Modified Dual Momentum — {mode.upper()}",
        fields=[
            {"name": "Portfolio", "value": f"${portfolio_value:,.2f}", "inline": True},
            {"name": "Cash", "value": f"${cash:,.2f}", "inline": True},
            {"name": "Rebalance", "value": "Yes" if rebalance else "No (report only)", "inline": True},
            {"name": "Targets", "value": "\n".join(target_lines), "inline": False},
            {"name": "Trades", "value": str(len(trades)) if trades else "None", "inline": True},
            {"name": "Mode", "value": "PAPER" if paper_trading else "LIVE", "inline": True},
        ],
        color=notifier.COLORS['success'] if mode == 'offensive' else notifier.COLORS['warning'],
        footer=f"Modified Dual Momentum | {datetime.now().strftime('%Y-%m-%d %H:%M')} ET"
    )

    if trades:
        notifier.send_trade_execution(trades)

    logger.info("=" * 60)
    logger.info("Modified Dual Momentum Auto-Trader - Complete")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
