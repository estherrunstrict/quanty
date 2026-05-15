# -*- coding: utf-8 -*-
"""
Uranium VB (Volatility Breakthrough) Auto-Trader
=================================================
Combined VB + Momentum strategy for URNM + URA ETFs.
Uses KIS API for US overseas stock trading.

Strategy (from optimized backtest — CAGR 30.5%, Sharpe 1.10):
  - VB Entry: price > prev_close + k * prev_range (adaptive k)
  - 3-tier regime: BULL (1.4x size), NEUTRAL (0.7x), BEAR (0x)
  - Multi-day hold with trailing ATR stop (max 10 days)
  - Asset management: 2.5% risk/trade, 8% heat limit, DD scaling
  - Exit: trailing stop, max hold, regime bear, RSI overbought

Backtest: CAGR 30.5%, MDD -19.5%, Sharpe 1.10, Win Rate 51%
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
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
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'dailyprice'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'order'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_daily_chartprice'))

import kis_auth as ka
import order
import inquire_balance
import inquire_psamount

from discord_notifier import DiscordNotifier

# --- Logger ---
logger = logging.getLogger('UraniumVB')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(os.path.join(project_root, 'uranium_vb.log'))
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

# --- State file ---
STATE_FILE = os.path.join(project_root, 'uranium_vb_state.json')

# --- Strategy Parameters (optimized from 729-combo grid search) ---
TICKERS = ["URA", "URNM"]
EXCHANGE_MAP = {"URA": "AMEX", "URNM": "AMEX"}

# VB parameters
K_BASE = 0.3
K_MIN = 0.25
K_MAX = 0.7
NOISE_LOOKBACK = 20

# Regime
REGIME_FAST = 10
REGIME_MID = 30
REGIME_SLOW = 60

# Holding & stops
ATR_PERIOD = 14
ATR_STOP_INIT = 1.2
ATR_TRAIL = 2.5
MAX_HOLD_DAYS = 10
MIN_HOLD_DAYS = 1

# Asset management
RISK_PER_TRADE = 0.025      # 2.5% risk per trade
HEAT_LIMIT = 0.08           # 8% max portfolio heat
MAX_POSITION = 0.60         # 60% max single position
DD_SCALE_START = 0.08       # scale down after 8% DD
DD_FLOOR = 0.30             # min 30% of normal size
REGIME_BULL_MULT = 1.3      # 1.3x in bull
REGIME_NEUTRAL_MULT = 0.7   # 0.7x in neutral
MAX_LEVERAGE = 1.3           # allow up to 1.3x in bull


def load_config():
    with open(os.path.join(project_root, 'config.yaml'), 'r') as f:
        return yaml.safe_load(f)


def load_allocation_override():
    """Read allocation multiplier from SecondLevelBot. Returns 1.0 if stale or unavailable."""
    override_path = os.path.join(project_root, 'allocation_override.json')
    try:
        if os.path.exists(override_path):
            with open(override_path) as f:
                data = json.load(f)
            ts = data.get('timestamp', '')
            if ts:
                from datetime import timezone
                override_time = datetime.fromisoformat(ts)
                now = datetime.now(override_time.tzinfo or timezone.utc)
                age_hours = (now - override_time).total_seconds() / 3600
                if age_hours > 2:
                    logger.warning(f"Second Level override is {age_hours:.1f}h old — using default 1.0")
                    return 1.0
            mult = data.get('allocation_multiplier', 1.0)
            regime = data.get('regime', 'NEUTRAL')
            score = data.get('cycle_score', 0)
            logger.info(f"Second Level override: {regime} (score {score:+d}) → allocation {mult:.0%}")
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
        'open_positions': {},  # {ticker: {entry_date, entry_price, shares, stop_price, peak_price}}
        'peak_equity': 0,
        'trade_history': [],
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# --- Market Data ---
def fetch_ohlc(ticker, days=90):
    """Fetch OHLC data from Yahoo Finance."""
    try:
        df = yf.download(ticker, period=f"{days}d", auto_adjust=True, progress=False)
        if df.empty:
            logger.error(f"No data for {ticker}")
            return None
        # yfinance >=0.2.x / 1.x returns MultiIndex columns even for single tickers.
        # Flatten to simple column names so downstream .iloc[-1] returns a scalar.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df[['Open', 'High', 'Low', 'Close']]
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return None


# --- Indicators ---
def calc_atr(ohlc, period=14):
    h, l, c = ohlc['High'], ohlc['Low'], ohlc['Close']
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_noise_ratio(ohlc, lb=20):
    c = ohlc['Close']
    direction = (c - c.shift(lb)).abs()
    path = c.diff().abs().rolling(lb).sum()
    eff = direction / path.replace(0, np.nan)
    return 1 - eff


def calc_regime(close, fast=10, mid=30, slow=60):
    """Returns 2=bull, 1=neutral, 0=bear for the latest bar."""
    if len(close) < slow + 5:
        return 1
    ma_f = close.rolling(fast).mean()
    ma_m = close.rolling(mid).mean()
    ma_s = close.rolling(slow).mean()
    bull = (ma_f.iloc[-1] > ma_m.iloc[-1]) and (ma_m.iloc[-1] > ma_s.iloc[-1]) and (close.iloc[-1] > ma_s.iloc[-1])
    bear = (close.iloc[-1] < ma_s.iloc[-1]) or ((ma_f.iloc[-1] < ma_m.iloc[-1]) and (ma_m.iloc[-1] < ma_s.iloc[-1]))
    if bull:
        return 2
    elif bear:
        return 0
    return 1


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period).mean()
    al = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).iloc[-1]


# --- KIS API helpers ---
def get_holdings(my_acct, my_prod):
    """Get current US stock holdings. Query both NASD and AMEX."""
    holdings = {}
    for excg in ["NASD", "AMEX"]:
      try:
        output1, _ = inquire_balance.inquire_balance(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd=excg, tr_crcy_cd="USD", env_dv="real"
        )
        if output1 is not None and not output1.empty:
            for _, row in output1.iterrows():
                t = row['ovrs_pdno']
                if t in TICKERS:
                    holdings[t] = {
                        'quantity': int(row['ovrs_cblc_qty']),
                        'avg_price': float(row.get('frcr_pchs_amt1', 0)) / max(int(row['ovrs_cblc_qty']), 1),
                        'value': float(row.get('ovrs_stck_evlu_amt', 0)),
                    }
      except Exception as e:
        logger.error(f"Failed to get holdings ({excg}): {e}")
    return holdings


def get_cash_usd(my_acct, my_prod):
    """Get available USD cash for trading."""
    try:
        df = inquire_psamount.inquire_psamount(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd="AMEX", item_cd="URA",
            ovrs_ord_unpr="0", env_dv="real"
        )
        if df is not None and not df.empty:
            return float(df.iloc[0]['ord_psbl_frcr_amt'])
    except Exception as e:
        logger.error(f"Failed to get cash: {e}")
    return 0.0


def place_order(side, my_acct, my_prod, ticker, quantity, price):
    """Place buy/sell order via KIS API."""
    logger.info(f"Placing {side.upper()}: {ticker} x {quantity} @ ~${price:.2f}")
    try:
        order_price = str(round(price * (1.03 if side == 'buy' else 0.95), 2))
        exchange = EXCHANGE_MAP.get(ticker, "AMEX")
        df_order = order.order(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd=exchange, pdno=ticker,
            ord_qty=str(int(quantity)), ovrs_ord_unpr=order_price,
            ord_dv=side, ctac_tlno="", mgco_aptm_odno="",
            ord_svr_dvsn_cd="0", ord_dvsn="00", env_dv="real"
        )
        logger.info(f"{side.upper()} result: {df_order}")
        return df_order
    except Exception as e:
        logger.error(f"{side.upper()} failed: {e}")
        return None


# --- Main Strategy ---
def main():
    logger.info("=" * 60)
    logger.info("Uranium VB Strategy Auto-Trader - Start")
    logger.info("=" * 60)

    config = load_config()
    webhook_url = config.get("DISCORD_WEBHOOK_URL")
    notifier = DiscordNotifier(webhook_url, "Uranium VB Strategy")

    # Capital budget
    uranium_config = config.get('uranium_vb', {})
    base_budget = uranium_config.get('budget_usd', 5000.0)
    sl_mult = load_allocation_override()
    budget_usd = base_budget * sl_mult
    logger.info(f"Budget: ${base_budget:,.0f} × {sl_mult:.0%} = ${budget_usd:,.0f}")
    paper_trading = uranium_config.get('paper_trading', True)

    # Auth
    try:
        ka.auth(svr="prod")
        logger.info("KIS API auth OK")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        notifier.send_error("Uranium VB Auth Failed", str(e))
        return

    acct = ka.getTREnv()
    my_acct, my_prod = acct.my_acct, acct.my_prod

    state = load_state()
    open_positions = state.get('open_positions', {})

    # Fetch market data
    logger.info("Fetching market data...")
    ohlc_data = {}
    for t in TICKERS:
        df = fetch_ohlc(t, days=120)
        if df is not None and len(df) > REGIME_SLOW + 5:
            ohlc_data[t] = df
            logger.info(f"  {t}: {len(df)} bars, latest close ${df['Close'].iloc[-1]:.2f}")

    if not ohlc_data:
        logger.error("No market data available")
        notifier.send_error("No Market Data", "Failed to fetch OHLC for all tickers")
        return

    # Get account state
    holdings = get_holdings(my_acct, my_prod)
    cash = get_cash_usd(my_acct, my_prod)
    portfolio_value = cash + sum(h['value'] for h in holdings.values())
    capped_value = min(portfolio_value, budget_usd) if budget_usd > 0 else portfolio_value

    logger.info(f"Portfolio: ${portfolio_value:,.2f}, Budget: ${capped_value:,.2f}, Cash: ${cash:,.2f}")
    for t, h in holdings.items():
        logger.info(f"  Holding {t}: {h['quantity']} shares @ ${h['avg_price']:.2f}")

    # Track peak equity for DD scaling
    peak_eq = max(state.get('peak_equity', capped_value), capped_value)
    cur_dd = (capped_value - peak_eq) / peak_eq if peak_eq > 0 else 0

    # DD scaling — clamp to [DD_FLOOR, 1.0] so deep drawdowns never inflate position size
    if cur_dd < -DD_SCALE_START:
        dd_depth = abs(cur_dd) - DD_SCALE_START
        dd_range = 1.0 - DD_SCALE_START - DD_FLOOR
        dd_scale = max(DD_FLOOR, min(1.0, 1.0 - dd_depth / dd_range)) if dd_range > 0 else DD_FLOOR
    else:
        dd_scale = 1.0

    logger.info(f"DD: {cur_dd:.1%}, DD Scale: {dd_scale:.2f}")

    trades_executed = []
    now = datetime.now()

    # --- EXIT LOGIC ---
    for t in list(open_positions.keys()):
        if t not in ohlc_data:
            continue

        pos = open_positions[t]
        df = ohlc_data[t]
        current_price = float(df['Close'].iloc[-1])
        today_low = float(df['Low'].iloc[-1])
        atr_val = float(calc_atr(df, ATR_PERIOD).iloc[-1])
        regime = calc_regime(df['Close'], REGIME_FAST, REGIME_MID, REGIME_SLOW)
        rsi_val = calc_rsi(df['Close'], 14)

        entry_date = datetime.fromisoformat(pos['entry_date'])
        hold_days = (now - entry_date).days

        # Update peak price and trailing stop
        if current_price > pos.get('peak_price', pos['entry_price']):
            pos['peak_price'] = current_price
            new_stop = current_price - ATR_TRAIL * atr_val
            if new_stop > pos['stop_price']:
                pos['stop_price'] = new_stop

        exit_price = None
        reason = ""

        # 1. Stop hit
        if today_low <= pos['stop_price']:
            exit_price = pos['stop_price']
            reason = "stop"
        # 2. Max hold
        elif hold_days >= MAX_HOLD_DAYS:
            exit_price = current_price
            reason = "max_hold"
        # 3. Regime turned bear
        elif regime == 0 and hold_days >= MIN_HOLD_DAYS:
            exit_price = current_price
            reason = "regime_bear"
        # 4. RSI overbought
        elif rsi_val > 80 and hold_days >= 3:
            exit_price = current_price
            reason = "rsi_exit"

        if exit_price is not None:
            shares = pos['shares']
            pnl = (exit_price - pos['entry_price']) * shares
            logger.info(f"EXIT {t}: {shares} shares @ ${exit_price:.2f} ({reason}), PnL: ${pnl:+,.2f}")

            if not paper_trading:
                # Sell via API
                actual_holdings = holdings.get(t, {}).get('quantity', 0)
                if actual_holdings > 0:
                    sell_qty = min(shares, actual_holdings)
                    place_order("sell", my_acct, my_prod, t, sell_qty, exit_price)

            trades_executed.append({
                'action': 'SELL', 'ticker': t, 'quantity': shares,
                'price': exit_price, 'value': shares * exit_price,
                'reason': f'Exit: {reason}, PnL: ${pnl:+,.2f}'
            })

            # Record trade
            state.setdefault('trade_history', []).append({
                'ticker': t, 'entry_date': pos['entry_date'],
                'entry_price': pos['entry_price'], 'exit_date': now.isoformat(),
                'exit_price': exit_price, 'shares': shares, 'pnl': pnl, 'reason': reason,
            })

            del open_positions[t]

    # --- ENTRY LOGIC ---
    # Always refresh cash before entry loop — stale cash guard can over-commit buying power
    if not paper_trading:
        if trades_executed:
            time.sleep(5)
        cash = get_cash_usd(my_acct, my_prod)

    heat = sum(
        pos.get('risk_amount', 0) / capped_value
        for pos in open_positions.values()
    ) if capped_value > 0 else 0

    for t in TICKERS:
        if t in open_positions:
            continue
        if t not in ohlc_data:
            continue

        df = ohlc_data[t]
        if len(df) < 2:
            continue

        current = df.iloc[-1]
        prev = df.iloc[-2]
        atr_val = float(calc_atr(df, ATR_PERIOD).iloc[-2])
        regime = calc_regime(df['Close'].iloc[:-1], REGIME_FAST, REGIME_MID, REGIME_SLOW)
        rsi_val = calc_rsi(df['Close'].iloc[:-1], 14)

        if pd.isna(atr_val) or atr_val <= 0:
            continue

        # No entries in bear
        if regime == 0:
            logger.info(f"  {t}: BEAR regime, skip entry")
            continue

        # Adaptive k
        noise = calc_noise_ratio(df.iloc[:-1], NOISE_LOOKBACK)
        k_val = float(noise.iloc[-1]) if not pd.isna(noise.iloc[-1]) else K_BASE
        k_val = np.clip(k_val, K_MIN, K_MAX)

        # Tighter k in strong bull with low RSI
        if regime == 2 and rsi_val < 45:
            k_val *= 0.85

        prev_range = prev['High'] - prev['Low']
        if prev_range <= 0:
            continue

        breakout_level = prev['Close'] + k_val * prev_range
        current_high = current['High']
        current_close = float(current['Close'])

        logger.info(f"  {t}: regime={regime}, k={k_val:.2f}, breakout=${breakout_level:.2f}, "
                     f"high=${current_high:.2f}, RSI={rsi_val:.0f}")

        if current_high >= breakout_level:
            entry_price = breakout_level

            # Regime sizing
            regime_mult = REGIME_BULL_MULT if regime == 2 else REGIME_NEUTRAL_MULT

            # Position sizing
            stop_price = entry_price - ATR_STOP_INIT * atr_val
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                continue

            risk_budget = capped_value * RISK_PER_TRADE * dd_scale * regime_mult

            # Heat check
            if heat + risk_budget / capped_value > HEAT_LIMIT:
                risk_budget = max(0, (HEAT_LIMIT - heat) * capped_value)
                if risk_budget <= 0:
                    logger.info(f"  {t}: heat limit reached, skip")
                    continue

            shares = int(risk_budget / risk_per_share)
            if shares <= 0:
                continue

            # Max position check
            max_val = capped_value * MAX_POSITION
            if regime == 2:
                max_val *= MAX_LEVERAGE
            if shares * entry_price > max_val:
                shares = int(max_val / entry_price)
            if shares * entry_price > cash * 0.95:
                shares = int(cash * 0.95 / entry_price)
            if shares <= 0:
                continue

            logger.info(f"ENTRY {t}: {shares} shares @ ${entry_price:.2f}, "
                         f"stop=${stop_price:.2f}, risk=${risk_budget:.0f}")

            if not paper_trading:
                result = place_order("buy", my_acct, my_prod, t, shares, entry_price)
                if result is None:
                    logger.error(f"BUY order failed for {t}, skipping position")
                    continue

            open_positions[t] = {
                'entry_date': now.isoformat(),
                'entry_price': entry_price,
                'shares': shares,
                'stop_price': stop_price,
                'peak_price': entry_price,
                'risk_amount': risk_budget,
            }
            heat += risk_budget / capped_value

            trades_executed.append({
                'action': 'BUY', 'ticker': t, 'quantity': shares,
                'price': entry_price, 'value': shares * entry_price,
                'reason': f'VB breakout (regime={["BEAR","NEUTRAL","BULL"][regime]}, k={k_val:.2f})'
            })

    # --- Save state ---
    state['open_positions'] = open_positions
    state['peak_equity'] = peak_eq
    save_state(state)

    # --- Build regime info ---
    regime_map = {}
    for t in TICKERS:
        if t in ohlc_data:
            r = calc_regime(ohlc_data[t]['Close'], REGIME_FAST, REGIME_MID, REGIME_SLOW)
            regime_map[t] = ['BEAR', 'NEUTRAL', 'BULL'][r]

    # Re-fetch holdings post-trade so the result JSON reflects actual state, not the pre-session snapshot
    if trades_executed and not paper_trading:
        try:
            time.sleep(3)
            holdings = get_holdings(my_acct, my_prod)
        except Exception as e:
            logger.warning(f"Post-trade holdings re-fetch failed, using pre-trade snapshot: {e}")

    # --- Write strategy result JSON (consumed by portfolio_summary.py) ---
    holdings_list = []
    for t, h in holdings.items():
        current_price = float(ohlc_data[t]['Close'].iloc[-1]) if t in ohlc_data else h['avg_price']
        profit = (current_price - h['avg_price']) * h['quantity']
        profit_rate = ((current_price / h['avg_price']) - 1) * 100 if h['avg_price'] > 0 else 0
        holdings_list.append({
            'ticker': t,
            'quantity': h['quantity'],
            'value': h['value'],
            'avg_price': h['avg_price'],
            'current_price': current_price,
            'profit': profit,
            'profit_rate': profit_rate,
            'currency': 'USD',
        })

    stock_value = sum(h['value'] for h in holdings.values())
    total_profit = sum(h.get('profit', 0) for h in holdings_list)

    result_data = {
        'strategy_name': 'URANIUM_VB',
        'timestamp': now.isoformat(),
        'allocation_mode': 'fixed',
        'capital_allocation': budget_usd,
        'allocated_capital': budget_usd,
        'total_value': capped_value,
        'cash_balance': cash,
        'total_profit': total_profit,
        'holdings': holdings_list,
        'session_summary': {
            'orders_executed': len(trades_executed),
            'regime': regime_map,
            'drawdown': cur_dd,
            'dd_scale': dd_scale,
            'heat': heat,
            'heat_limit': HEAT_LIMIT,
            'paper_trading': paper_trading,
            'open_positions_count': len(open_positions),
        },
    }

    results_dir = os.path.join(project_root, 'strategy_results')
    os.makedirs(results_dir, exist_ok=True)
    result_file = os.path.join(results_dir, 'uranium_vb_result.json')
    try:
        tmp_result = result_file + '.tmp'
        with open(tmp_result, 'w') as f:
            json.dump(result_data, f, indent=2, default=str)
        os.replace(tmp_result, result_file)
        logger.info(f"Strategy result written to {result_file}")
    except Exception as e:
        logger.error(f"Failed to write strategy result: {e}")

    # --- Discord notifications ---
    mode = "PAPER" if paper_trading else "LIVE"

    if trades_executed:
        notifier.send_trade_execution(trades_executed)

    # Build status report
    pos_text = "None (all cash)"
    if open_positions:
        lines = []
        for t, pos in open_positions.items():
            current_price = float(ohlc_data[t]['Close'].iloc[-1]) if t in ohlc_data else pos['entry_price']
            pnl = (current_price - pos['entry_price']) * pos['shares']
            lines.append(f"**{t}**: {pos['shares']}sh @ ${pos['entry_price']:.2f} "
                         f"(stop ${pos['stop_price']:.2f}, PnL ${pnl:+,.0f})")
        pos_text = "\n".join(lines)

    regime_text = "  ".join(f"{t}: {r}" for t, r in regime_map.items())

    notifier._send(
        title=f"Uranium VB [{mode}] — Session Complete",
        fields=[
            {"name": "Portfolio", "value": f"${capped_value:,.2f} (DD: {cur_dd:.1%})", "inline": True},
            {"name": "DD Scale", "value": f"{dd_scale:.0%}", "inline": True},
            {"name": "Heat", "value": f"{heat:.1%} / {HEAT_LIMIT:.0%}", "inline": True},
            {"name": "Positions", "value": pos_text, "inline": False},
            {"name": "Regime", "value": regime_text, "inline": False},
            {"name": "Trades Today", "value": str(len(trades_executed)), "inline": True},
            {"name": "Mode", "value": mode, "inline": True},
        ],
        color=notifier.COLORS['info'],
        footer=f"Uranium VB | {now.strftime('%Y-%m-%d %H:%M')} ET"
    )

    logger.info("=" * 60)
    logger.info("Uranium VB Strategy Auto-Trader - Complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
