# -*- coding: utf-8 -*-
"""
Claude AI Trading Bot - Strategy Executor
==========================================

8th strategy bot. Uses Claude Trading Bot's market regime analysis
and AI-curated stock watchlist to execute trades via KIS API.

Strategy:
- AI-curated watchlist (EQIX, expandable)
- Buy: Regime BULLISH/NEUTRAL + price > MA200 + positive 3-month momentum
- Sell: Regime BEARISH/DANGER OR price < MA200 OR stop-loss (-10%) OR take-profit (+25%)
- Rebalance: Monthly (first trading day)
- Risk: Equal-weight across watchlist, capped by budget

Runs at 9:30 ET + 120s delay (after Quant40, JD, MDM, Hybrid VB).

Usage:
    python ClaudeTradingBotAutoTrade.py
    python ClaudeTradingBotAutoTrade.py --paper   # paper trading mode
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

# --- Position Tracking ---
from strategy_positions import get_tracker

# --- Logger ---
logger = logging.getLogger('ClaudeTradingBot')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(os.path.join(project_root, 'claude_trading_bot.log'))
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

STATE_FILE = os.path.join(project_root, 'claude_trading_bot_state.json')
REGIME_FILE = os.path.join(project_root, 'claude-trading-bot', 'state', 'latest_regime.json')


# =============================================================================
# WATCHLIST - AI-Curated Stocks
# =============================================================================

# Exchange mapping for KIS API
EXCHANGE_MAP = {
    'EQIX': 'NASD',    # Equinix - NASDAQ
    'DLR':  'NYSE',    # Digital Realty - NYSE
    'AMT':  'NYSE',    # American Tower - NYSE
    'CCI':  'NYSE',    # Crown Castle - NYSE
    'MSFT': 'NASD',    # Microsoft - NASDAQ
    'GOOGL': 'NASD',   # Alphabet - NASDAQ
    'NVDA': 'NASD',    # NVIDIA - NASDAQ
    'AMZN': 'NASD',    # Amazon - NASDAQ
    'AAPL': 'NASD',    # Apple - NASDAQ
    'META': 'NASD',    # Meta - NASDAQ
    'AVGO': 'NASD',    # Broadcom - NASDAQ
    'NOW':  'NYSE',    # ServiceNow - NYSE
}


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
                    logger.warning("Second Level override stale - using default 1.0")
                    return 1.0
            mult = data.get('allocation_multiplier', 1.0)
            regime = data.get('regime', 'NEUTRAL')
            logger.info(f"Second Level override: {regime} -> allocation {mult:.0%}")
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
        'current_positions': {},
        'trade_history': [],
        'watchlist_version': 0,
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def load_regime():
    """Load latest regime from Claude Trading Bot analysis."""
    if os.path.exists(REGIME_FILE):
        try:
            with open(REGIME_FILE, 'r') as f:
                data = json.load(f)
            # Check freshness (must be from today or yesterday)
            ts = data.get('timestamp', '')
            if ts:
                regime_time = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                age_hours = (datetime.now(regime_time.tzinfo) - regime_time).total_seconds() / 3600
                if age_hours > 24:
                    logger.warning(f"Regime data is {age_hours:.0f}h old - defaulting to CAUTIOUS (no buys)")
                    return 'CAUTIOUS', data
            regime = data.get('regime', 'CAUTIOUS')
            logger.info(f"Loaded regime from Claude Bot: {regime}")
            return regime, data
        except Exception as e:
            logger.error(f"Failed to load regime: {e}")
    logger.warning("No regime data available - defaulting to CAUTIOUS (no buys)")
    return 'CAUTIOUS', {}


# =============================================================================
# MARKET DATA & SIGNALS
# =============================================================================

def fetch_price_data(ticker, days=400):
    """Fetch daily OHLCV data via yfinance. 400 calendar days ~ 280 trading days for MA200."""
    try:
        df = yf.download(ticker, period=f"{days}d", auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return None


def compute_signal(ticker, df, regime):
    """
    Compute buy/sell/hold signal for a single stock.

    Buy conditions (ALL must be true):
      1. Regime is BULLISH or NEUTRAL (or RECOVERY)
      2. Price > 200-day MA (uptrend)
      3. 3-month momentum > 0% (positive return)

    Sell conditions (ANY triggers sell):
      1. Regime is BEARISH or DANGER
      2. Price < 200-day MA (trend broken)
      3. Stop-loss: price dropped > 10% from entry (checked separately)

    Hold: everything else (CAUTIOUS regime with intact trend = hold)

    Returns: (action, strength, reason)
        action: 'BUY', 'SELL', 'HOLD'
        strength: 0.0 to 1.0
        reason: explanation string
    """
    if df is None or len(df) < 200:
        return 'HOLD', 0.0, f"{ticker}: insufficient data ({len(df) if df is not None else 0} days)"

    close = df['Close']
    current_price = float(close.iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])

    # 3-month (63 trading days) momentum
    if len(close) >= 63:
        momentum_3m = (current_price / float(close.iloc[-63]) - 1) * 100
    else:
        momentum_3m = 0.0

    # 1-month (21 trading days) momentum
    if len(close) >= 21:
        momentum_1m = (current_price / float(close.iloc[-21]) - 1) * 100
    else:
        momentum_1m = 0.0

    price_vs_ma200 = (current_price / ma200 - 1) * 100

    logger.info(f"  {ticker}: ${current_price:.2f} | MA200=${ma200:.2f} ({price_vs_ma200:+.1f}%) | "
                f"MA50=${ma50:.2f} | 3m={momentum_3m:+.1f}% | 1m={momentum_1m:+.1f}% | Regime={regime}")

    # --- SELL signals ---
    if regime in ('BEARISH', 'DANGER'):
        strength = 0.9 if regime == 'DANGER' else 0.7
        return 'SELL', strength, f"Regime {regime} - risk-off"

    if current_price < ma200:
        # Below 200-day MA = trend broken
        severity = abs(price_vs_ma200)
        strength = min(0.9, 0.5 + severity / 20)
        return 'SELL', strength, f"Below MA200 ({price_vs_ma200:+.1f}%) - trend broken"

    # --- BUY signals ---
    if regime in ('BULLISH', 'NEUTRAL', 'RECOVERY'):
        if current_price > ma200 and momentum_3m > 0:
            # Strength based on trend quality
            trend_score = min(1.0, price_vs_ma200 / 10)  # 0-1 based on distance above MA200
            momentum_score = min(1.0, momentum_3m / 20)   # 0-1 based on 3m return
            regime_bonus = 0.2 if regime == 'BULLISH' else 0.0
            strength = min(1.0, 0.4 + trend_score * 0.3 + momentum_score * 0.2 + regime_bonus)
            return 'BUY', strength, (f"Regime {regime} + above MA200 ({price_vs_ma200:+.1f}%) "
                                     f"+ momentum 3m={momentum_3m:+.1f}%")

    # --- HOLD ---
    return 'HOLD', 0.0, f"No clear signal (regime={regime}, vs MA200={price_vs_ma200:+.1f}%)"


def check_stop_loss_take_profit(ticker, avg_price, current_price, stop_loss_pct, take_profit_pct):
    """Check stop-loss and take-profit levels."""
    if avg_price <= 0:
        return None, 0.0, ""

    pnl_pct = (current_price / avg_price - 1) * 100

    if pnl_pct <= stop_loss_pct:
        return 'SELL', 0.95, f"STOP-LOSS triggered: {pnl_pct:+.1f}% (limit: {stop_loss_pct}%)"

    if pnl_pct >= take_profit_pct:
        return 'SELL', 0.6, f"TAKE-PROFIT triggered: {pnl_pct:+.1f}% (limit: +{take_profit_pct}%)"

    return None, 0.0, ""


# =============================================================================
# KIS API HELPERS
# =============================================================================

def get_holdings(my_acct, my_prod, watchlist_tickers):
    """Get current holdings for watchlist tickers."""
    holdings = {}
    checked_exchanges = set()

    for ticker in watchlist_tickers:
        excg = EXCHANGE_MAP.get(ticker, 'NASD')
        if excg in checked_exchanges:
            continue
        checked_exchanges.add(excg)

        try:
            output1, _ = inquire_balance.inquire_balance(
                cano=my_acct, acnt_prdt_cd=my_prod,
                ovrs_excg_cd=excg, tr_crcy_cd="USD", env_dv="real"
            )
            if output1 is not None and not output1.empty:
                for _, row in output1.iterrows():
                    t = row['ovrs_pdno']
                    if t in watchlist_tickers:
                        qty = int(row['ovrs_cblc_qty'])
                        if qty > 0:
                            holdings[t] = {
                                'quantity': qty,
                                'avg_price': float(row.get('frcr_pchs_amt1', 0)) / max(qty, 1),
                                'value': float(row.get('ovrs_stck_evlu_amt', 0)),
                            }
        except Exception as e:
            logger.error(f"Failed to get holdings ({excg}): {e}")
        time.sleep(0.5)

    return holdings


def get_cash_usd(my_acct, my_prod):
    """Get available USD cash."""
    try:
        df = inquire_psamount.inquire_psamount(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd="NASD", item_cd="EQIX",
            ovrs_ord_unpr="0", env_dv="real"
        )
        if df is not None and not df.empty:
            return float(df.iloc[0]['ord_psbl_frcr_amt'])
    except Exception as e:
        logger.error(f"Failed to get cash: {e}")
    return 0.0


def place_order(side, my_acct, my_prod, ticker, quantity, price):
    """Execute buy or sell order via KIS API."""
    logger.info(f"Placing {side.upper()}: {ticker} x {quantity} @ ~${price:.2f}")
    try:
        # Use limit order with 1% slippage allowance
        order_price = str(round(price * (1.01 if side == 'buy' else 0.99), 2))
        exchange = EXCHANGE_MAP.get(ticker, 'NASD')
        df_order = order.order(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd=exchange, pdno=ticker,
            ord_qty=str(int(quantity)), ovrs_ord_unpr=order_price,
            ord_dv=side, ctac_tlno="", mgco_aptm_odno="",
            ord_svr_dvsn_cd="0", ord_dvsn="00", env_dv="real"
        )
        if df_order is not None and not df_order.empty:
            logger.info(f"{side.upper()} confirmed for {ticker}: {df_order.to_dict()}")
            return True
        logger.error(f"{side.upper()} returned empty/None for {ticker} — order likely rejected")
        return False
    except Exception as e:
        logger.error(f"{side.upper()} failed for {ticker}: {e}")
        return False


# =============================================================================
# REBALANCE / TRADE LOGIC
# =============================================================================

def is_rebalance_day(state):
    """First trading day of the month (US Eastern)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    current_month = now.strftime('%Y-%m')
    last = state.get('last_rebalance_month')

    if last == current_month:
        return False

    if last is None:
        logger.info("First run - forcing initial rebalance")
        return True

    if now.weekday() >= 5:
        return False

    # Check if any earlier weekday this month
    for d in range(1, now.day):
        past = now.replace(day=d)
        if past.weekday() < 5:
            return False

    return True


def execute_trades(signals, my_acct, my_prod, holdings, cash, budget, paper_trading):
    """
    Execute trades based on computed signals.

    Sells first, then buys with available cash.
    Equal-weight allocation across BUY signals.
    """
    trades = []

    # --- Phase 1: SELL ---
    sell_failed = False
    for ticker, (action, strength, reason) in signals.items():
        if action != 'SELL':
            continue
        if ticker not in holdings or holdings[ticker]['quantity'] <= 0:
            continue

        h = holdings[ticker]
        logger.info(f"SELL {ticker}: {h['quantity']} shares - {reason}")

        if not paper_trading:
            # Fetch fresh price for accurate limit order
            try:
                df_fresh = yf.download(ticker, period='5d', auto_adjust=True, progress=False)
                if isinstance(df_fresh.columns, pd.MultiIndex):
                    df_fresh.columns = df_fresh.columns.get_level_values(0)
                price = float(df_fresh['Close'].iloc[-1])
            except Exception:
                price = h['value'] / h['quantity'] if h['quantity'] > 0 else 0
            success = place_order("sell", my_acct, my_prod, ticker, h['quantity'], price)
            if not success:
                logger.error(f"Failed to sell {ticker}")
                sell_failed = True
                continue
        else:
            price = h['value'] / h['quantity'] if h['quantity'] > 0 else 0

        trades.append({
            'action': 'SELL', 'ticker': ticker,
            'quantity': h['quantity'], 'price': price, 'value': h['value'],
            'reason': reason, 'strength': strength,
        })

    # Wait for sells to settle and refresh holdings
    if trades and not paper_trading:
        time.sleep(5)
        cash = get_cash_usd(my_acct, my_prod)
        # Refresh holdings so buy filter sees updated state
        holdings = get_holdings(my_acct, my_prod, list(signals.keys()))

    # Skip buys if any sell failed (avoid double exposure)
    if sell_failed:
        logger.warning("Skipping buy phase - sell(s) failed, avoiding double exposure")
        return trades

    # --- Phase 2: BUY ---
    buy_tickers = [t for t, (a, s, r) in signals.items()
                   if a == 'BUY' and s >= 0.5 and t not in holdings]

    if buy_tickers and cash > 0:
        # Equal-weight allocation across buy candidates
        num_buys = len(buy_tickers)
        per_stock_budget = min(cash * 0.95, budget) / num_buys

        for ticker in buy_tickers:
            try:
                df = yf.download(ticker, period='5d', auto_adjust=True, progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                price = float(df['Close'].iloc[-1])
            except Exception:
                logger.error(f"Can't get price for {ticker}")
                continue

            buy_qty = int(per_stock_budget / price)
            if buy_qty <= 0:
                logger.warning(f"Insufficient budget for {ticker} (need ${price:.2f}, have ${per_stock_budget:.2f})")
                continue

            _, strength, reason = signals[ticker]
            logger.info(f"BUY {ticker}: {buy_qty} shares @ ~${price:.2f} - {reason}")

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
                'value': cost, 'reason': reason, 'strength': strength,
            })

    return trades


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude AI Trading Bot")
    parser.add_argument('--paper', action='store_true', help='Paper trading mode')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Claude AI Trading Bot - Strategy Executor - Start")
    logger.info("=" * 60)

    config = load_config()
    webhook_url = (config.get("DISCORD_WEBHOOK_URL")
                   or os.environ.get("DISCORD_WEBHOOK_URL")
                   or _load_env_file().get("DISCORD_WEBHOOK_URL"))
    notifier = DiscordNotifier(webhook_url, "Claude AI Trading Bot")

    # Strategy config
    strat_config = config.get('claude_trading_bot', {})
    watchlist = strat_config.get('watchlist', ['EQIX'])
    stop_loss_pct = strat_config.get('stop_loss_pct', -10.0)
    take_profit_pct = strat_config.get('take_profit_pct', 25.0)
    paper_trading = args.paper or strat_config.get('paper_trading', False)

    # Budget
    cap_mgmt = config.get('capital_management', {}).get('claude_trading_bot', {})
    base_budget = cap_mgmt.get('budget_usd', 3000.0)
    sl_mult = load_allocation_override()
    budget = base_budget * sl_mult
    logger.info(f"Budget: ${base_budget:,.0f} x {sl_mult:.0%} = ${budget:,.0f}")
    logger.info(f"Watchlist: {watchlist}")
    logger.info(f"Mode: {'PAPER' if paper_trading else 'LIVE'}")

    # Auth
    try:
        ka.auth(svr="prod")
        logger.info("KIS API auth OK")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        notifier.send_error("Claude Trading Bot Auth Failed", str(e))
        return

    acct = ka.getTREnv()
    my_acct, my_prod = acct.my_acct, acct.my_prod

    state = load_state()
    position_tracker = get_tracker()

    # --- Step 1: Load regime from Claude Trading Bot analysis ---
    regime, regime_data = load_regime()
    logger.info(f"Market Regime: {regime}")

    # --- Step 2: Fetch price data and compute signals ---
    logger.info("Fetching price data for watchlist...")
    signals = {}
    price_data = {}

    for ticker in watchlist:
        df = fetch_price_data(ticker, days=400)
        price_data[ticker] = df
        action, strength, reason = compute_signal(ticker, df, regime)
        signals[ticker] = (action, strength, reason)
        time.sleep(0.3)

    # --- Step 3: Get current positions ---
    holdings = get_holdings(my_acct, my_prod, watchlist)
    cash = get_cash_usd(my_acct, my_prod)
    portfolio_value = cash + sum(h['value'] for h in holdings.values())

    logger.info(f"Portfolio: ${portfolio_value:,.2f}, Cash: ${cash:,.2f}")
    for t, h in holdings.items():
        logger.info(f"  {t}: {h['quantity']} shares (${h['value']:,.2f}, avg=${h['avg_price']:.2f})")

    # --- Step 4: Check stop-loss / take-profit for existing positions ---
    for ticker, h in holdings.items():
        df = price_data.get(ticker)
        if df is not None and not df.empty:
            current_price = float(df['Close'].iloc[-1])
            sl_action, sl_strength, sl_reason = check_stop_loss_take_profit(
                ticker, h['avg_price'], current_price, stop_loss_pct, take_profit_pct
            )
            if sl_action == 'SELL':
                # Override signal with stop-loss/take-profit
                logger.warning(f"  {ticker}: {sl_reason}")
                signals[ticker] = (sl_action, sl_strength, sl_reason)

    # --- Step 5: Execute trades ---
    rebalance = is_rebalance_day(state)
    trades = []

    if rebalance:
        logger.info("=== REBALANCE DAY ===")
        trades = execute_trades(signals, my_acct, my_prod, holdings, cash, budget, paper_trading)
        if trades or paper_trading:
            state['last_rebalance_month'] = datetime.now().strftime('%Y-%m')
        else:
            logger.warning("No trades executed - will retry on next run")
    else:
        # Non-rebalance day: only execute SELL signals (stop-loss, regime change, trend break)
        sell_signals = {t: s for t, s in signals.items() if s[0] == 'SELL'}
        if sell_signals:
            logger.info("=== NON-REBALANCE DAY: Processing sell signals only ===")
            trades = execute_trades(sell_signals, my_acct, my_prod, holdings, cash, budget, paper_trading)
        else:
            logger.info("Not a rebalance day and no sell signals - reporting only")

    # Update state
    state['current_positions'] = {t: {'action': a, 'strength': s, 'reason': r}
                                  for t, (a, s, r) in signals.items()}
    state['trade_history'].extend([
        {**t, 'timestamp': datetime.now().isoformat()} for t in trades
    ])
    # Keep only last 100 trades in history
    state['trade_history'] = state['trade_history'][-100:]
    save_state(state)

    # --- Step 6: Write strategy result JSON (for portfolio_summary.py) ---
    if trades and not paper_trading:
        time.sleep(3)
        holdings = get_holdings(my_acct, my_prod, watchlist)
        cash = get_cash_usd(my_acct, my_prod)
        portfolio_value = cash + sum(h['value'] for h in holdings.values())

    results_dir = os.path.join(project_root, 'strategy_results')
    os.makedirs(results_dir, exist_ok=True)

    holdings_list = []
    total_profit = 0
    for t, h in holdings.items():
        current_price = float(price_data[t]['Close'].iloc[-1]) if t in price_data and not price_data[t].empty else h['avg_price']
        holding_profit = (current_price - h['avg_price']) * h['quantity'] if h['quantity'] > 0 else 0
        total_profit += holding_profit
        holdings_list.append({
            'ticker': t, 'quantity': h['quantity'], 'value': h['value'],
            'avg_price': h['avg_price'], 'current_price': current_price,
            'profit': round(holding_profit, 2), 'currency': 'USD',
        })

    result_data = {
        'strategy_name': 'CLAUDE_AI_BOT',
        'timestamp': datetime.now().isoformat(),
        'allocation_mode': 'fixed',
        'capital_allocation': budget,
        'allocated_capital': budget,
        'total_value': portfolio_value,
        'cash_balance': cash,
        'total_profit': round(total_profit, 2),
        'holdings': holdings_list,
        'session_summary': {
            'regime': regime,
            'watchlist': watchlist,
            'signals': {t: {'action': a, 'strength': s, 'reason': r}
                        for t, (a, s, r) in signals.items()},
            'rebalance_day': rebalance,
            'orders_executed': len(trades),
            'paper_trading': paper_trading,
        },
    }

    result_file = os.path.join(results_dir, 'claude_trading_bot_result.json')
    try:
        tmp = result_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(result_data, f, indent=2, default=str)
        os.replace(tmp, result_file)
    except Exception as e:
        logger.error(f"Failed to write result: {e}")

    # Update position tracker (for dual-strategy coordination)
    for t, h in holdings.items():
        # register_position handles both new and existing entries
        position_tracker.register_position("CLAUDE_AI_BOT", t, h['quantity'], h['avg_price'])

    # --- Step 7: Discord notification ---
    signal_lines = []
    for t, (action, strength, reason) in signals.items():
        emoji = {"BUY": "\U0001f7e2", "SELL": "\U0001f534", "HOLD": "\u26aa"}
        held = f" [{holdings[t]['quantity']} shares]" if t in holdings else ""
        signal_lines.append(f"{emoji.get(action, '?')} {t}: {action} ({strength:.0%}){held}")

    trade_lines = []
    for t in trades:
        trade_lines.append(f"{t['action']} {t['ticker']} x{t['quantity']} - {t['reason']}")

    regime_emoji = {
        'BULLISH': '\U0001f7e2', 'RECOVERY': '\U0001f7e2',
        'NEUTRAL': '\U0001f7e1',
        'CAUTIOUS': '\U0001f7e0', 'BEARISH': '\U0001f534', 'DANGER': '\U0001f534',
    }

    notifier._send(
        title=f"{regime_emoji.get(regime, '\u26aa')} Claude AI Bot - {regime}",
        fields=[
            {"name": "Portfolio", "value": f"${portfolio_value:,.2f}", "inline": True},
            {"name": "Cash", "value": f"${cash:,.2f}", "inline": True},
            {"name": "Regime", "value": regime, "inline": True},
            {"name": "Signals", "value": "\n".join(signal_lines) or "No watchlist", "inline": False},
            {"name": "Rebalance", "value": "Yes" if rebalance else "No (daily check)", "inline": True},
            {"name": "Trades", "value": "\n".join(trade_lines) if trade_lines else "None", "inline": False},
            {"name": "Mode", "value": "PAPER" if paper_trading else "LIVE", "inline": True},
        ],
        color=notifier.COLORS.get('success', 0x00FF00) if regime in ('BULLISH', 'RECOVERY')
              else notifier.COLORS.get('warning', 0xFFFF00),
        footer=f"Claude AI Trading Bot | {datetime.now().strftime('%Y-%m-%d %H:%M')} ET"
    )

    if trades:
        notifier.send_trade_execution(trades)

    logger.info("=" * 60)
    logger.info("Claude AI Trading Bot - Strategy Executor - Complete")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
