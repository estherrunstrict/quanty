# -*- coding: utf-8 -*-
"""
Hybrid VB + Vol-Managed ETF Auto-Trader
========================================
Strategy: Larry Williams Volatility Breakout (entry) + Moreira & Muir (2017)
          volatility-managed position sizing.

Universe:
  US (10): GLD, SLV, GLTR, GDX, USO, URA, FTGC, CPER, DBA, SIL (commodity-optimized)
  KR (7):  069500, 229200, 144600, 132030, 305720, 091170, 364690

Entry: price > prev_close + k * ATR(14), adaptive k via noise ratio
Sizing: target_vol / realized_vol(20d), capped at 2x, layered on regime mult
Exit:  trailing ATR stop (2.5x), max 10 days, regime bear, RSI > 80
Risk:  2.5% per trade, 8% heat limit, drawdown scaling (30%-100%)

Usage:
  python HybridVBAutoTrade.py --market kr   # Korea session (09:01 KST)
  python HybridVBAutoTrade.py --market us   # US session (09:31 ET)
"""

import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

# KIS API paths — both domestic and overseas
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm'))
# Overseas (US)
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'inquire_psamount'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'overseas_stock', 'order'))
# Domestic (Korea)
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_psbl_order'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'order_cash'))

import kis_auth as ka
from discord_notifier import DiscordNotifier

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

STATE_FILE = os.path.join(project_root, 'hybrid_vb_state.json')

# --- KRX Market Holidays ---
KRX_HOLIDAYS = {
    2025: {
        '0101', '0128', '0129', '0130', '0301', '0501', '0505', '0506',
        '0606', '0815', '1003', '1006', '1007', '1008', '1009', '1225', '1231',
    },
    2026: {
        '0101', '0127', '0128', '0129', '0301', '0501', '0505',
        '0606', '0815', '0924', '0925', '0928', '1009', '1225', '1231',
    },
}

REGIME_LABELS = ['BEAR', 'NEUTRAL', 'BULL']

# --- Logger (set up in main()) ---
logger = logging.getLogger('HybridVB')


# =============================================================================
# CONFIG / STATE
# =============================================================================

def _load_env_file():
    """Load key=value pairs from .env file."""
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


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            # Ensure both market keys exist
            for mkt in ('kr', 'us'):
                state.setdefault(mkt, {
                    'open_positions': {},
                    'peak_equity': 0,
                    'trade_history': [],
                })
            return state
        except Exception as e:
            logger.error(f"State file error: {e}")
    return {
        'kr': {'open_positions': {}, 'peak_equity': 0, 'trade_history': []},
        'us': {'open_positions': {}, 'peak_equity': 0, 'trade_history': []},
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def load_allocation_override():
    """Read allocation multiplier from SecondLevelBot. Returns 1.0 if stale or unavailable."""
    override_path = os.path.join(project_root, 'allocation_override.json')
    try:
        if os.path.exists(override_path):
            with open(override_path) as f:
                data = json.load(f)
            # Staleness check: reject if older than 2 hours
            ts = data.get('timestamp', '')
            if ts:
                override_time = datetime.fromisoformat(ts)
                now = datetime.now(override_time.tzinfo or KST)
                age_hours = (now - override_time).total_seconds() / 3600
                if age_hours > 2:
                    logger.warning(f"Second Level override is {age_hours:.1f}h old — using default 1.0")
                    return 1.0, 'NEUTRAL', 0
            mult = data.get('allocation_multiplier', 1.0)
            regime = data.get('regime', 'NEUTRAL')
            score = data.get('cycle_score', 0)
            logger.info(f"Second Level override: {regime} (score {score:+d}) → allocation {mult:.0%}")
            return mult, regime, score
    except Exception as e:
        logger.warning(f"Failed to read allocation override: {e}")
    return 1.0, 'NEUTRAL', 0


def is_krx_holiday(date: datetime) -> bool:
    holidays = KRX_HOLIDAYS.get(date.year, set())
    return date.strftime('%m%d') in holidays


# =============================================================================
# INDICATORS (from UraniumVBAutoTrade.py)
# =============================================================================

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
    ag = gain.ewm(alpha=1 / period, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).iloc[-1]


# =============================================================================
# NEW: VOLATILITY-MANAGED SIZING (Moreira & Muir, 2017)
# =============================================================================

def calc_vol_size_multiplier(close, target_vol=0.15, lookback=20, cap=2.0):
    """
    Scale position inversely to realized volatility.
    Returns multiplier in [0, cap]. Returns 1.0 if insufficient data.
    """
    if len(close) < lookback + 2:
        return 1.0
    log_returns = np.log(close / close.shift(1)).dropna()
    if len(log_returns) < lookback:
        return 1.0
    realized_vol = log_returns.iloc[-lookback:].std() * np.sqrt(252)
    if realized_vol <= 0 or np.isnan(realized_vol):
        return 1.0
    mult = target_vol / realized_vol
    return float(np.clip(mult, 0.0, cap))


# =============================================================================
# MARKET DATA
# =============================================================================

def fetch_ohlc(ticker, days=120):
    """Fetch OHLC data from Yahoo Finance."""
    try:
        df = yf.download(ticker, period=f"{days}d", auto_adjust=True, progress=False)
        if df.empty:
            logger.error(f"No data for {ticker}")
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df[['Open', 'High', 'Low', 'Close']]
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return None


# =============================================================================
# VB SIGNAL & POSITION SIZING
# =============================================================================

def check_vb_signal(df, params):
    """
    Check volatility breakout signal.
    Returns (signal: bool, breakout_level: float, k_val: float, atr_val: float).
    """
    if len(df) < 3:
        return False, 0, 0, 0

    prev = df.iloc[-2]
    current = df.iloc[-1]
    atr_val = float(calc_atr(df, params['atr_period']).iloc[-2])

    if pd.isna(atr_val) or atr_val <= 0:
        return False, 0, 0, 0

    # Adaptive k
    noise = calc_noise_ratio(df.iloc[:-1], params['noise_lookback'])
    k_val = float(noise.iloc[-1]) if not pd.isna(noise.iloc[-1]) else params['k_base']
    k_val = float(np.clip(k_val, params['k_min'], params['k_max']))

    # Regime for k tightening
    regime = calc_regime(df['Close'].iloc[:-1], params['regime_fast'], params['regime_mid'], params['regime_slow'])
    rsi_val = calc_rsi(df['Close'].iloc[:-1], 14)
    if regime == 2 and rsi_val < 45:
        k_val *= 0.85

    prev_range = prev['High'] - prev['Low']
    if prev_range <= 0:
        return False, 0, k_val, atr_val

    breakout_level = prev['Close'] + k_val * prev_range
    signal = current['High'] >= breakout_level

    return signal, float(breakout_level), k_val, atr_val


def compute_position_size(entry_price, atr_val, portfolio_value, regime, vol_mult,
                          params, current_heat, dd_scale, cash):
    """
    Returns (shares, stop_price, risk_amount).
    Integrates regime mult + vol-managed sizing + heat limit + DD scaling.
    vol_mult: pre-computed vol multiplier (1.0 if vol-managed is disabled).
    """
    stop_price = entry_price - params['atr_stop_init'] * atr_val
    risk_per_share = entry_price - stop_price
    if risk_per_share <= 0:
        return 0, 0.0, 0.0

    # Regime multiplier
    if regime == 2:
        regime_mult = params['regime_bull_mult']
    elif regime == 1:
        regime_mult = params['regime_neutral_mult']
    else:
        return 0, 0.0, 0.0  # No entry in bear

    # Risk budget
    risk_budget = portfolio_value * params['risk_per_trade'] * dd_scale * regime_mult * vol_mult

    # Heat check
    if current_heat + risk_budget / portfolio_value > params['heat_limit']:
        risk_budget = max(0, (params['heat_limit'] - current_heat) * portfolio_value)
        if risk_budget <= 0:
            return 0, 0.0, 0.0

    shares = int(risk_budget / risk_per_share)
    if shares <= 0:
        return 0, 0.0, 0.0

    # Max position check
    max_val = portfolio_value * params['max_position']
    if regime == 2:
        max_val *= params.get('max_leverage', 1.3)
    if shares * entry_price > max_val:
        shares = int(max_val / entry_price)
    if shares * entry_price > cash * 0.95:
        shares = int(cash * 0.95 / entry_price)
    if shares <= 0:
        return 0, 0.0, 0.0

    return shares, stop_price, risk_budget


def check_exit_conditions(pos, df, params, now):
    """
    Returns (reason: str | None, exit_price: float).
    Mutates pos in-place to update stop/peak.
    """
    current_price = float(df['Close'].iloc[-1])
    today_low = float(df['Low'].iloc[-1])
    atr_val = float(calc_atr(df, params['atr_period']).iloc[-1])
    regime = calc_regime(df['Close'], params['regime_fast'], params['regime_mid'], params['regime_slow'])
    rsi_val = calc_rsi(df['Close'], 14)

    entry_date = datetime.fromisoformat(pos['entry_date'])
    if entry_date.tzinfo:
        now_aware = now if now.tzinfo else now.replace(tzinfo=entry_date.tzinfo)
    else:
        now_aware = now.replace(tzinfo=None) if now.tzinfo else now
    hold_days = (now_aware - entry_date).days

    # Update trailing stop
    if current_price > pos.get('peak_price', pos['entry_price']):
        pos['peak_price'] = current_price
        new_stop = current_price - params['atr_trail'] * atr_val
        if new_stop > pos['stop_price']:
            pos['stop_price'] = new_stop

    # 1. Stop hit
    if today_low <= pos['stop_price']:
        return 'stop', pos['stop_price']
    # 2. Max hold
    if hold_days >= params['max_hold_days']:
        return 'max_hold', current_price
    # 3. Regime bear
    if regime == 0 and hold_days >= params.get('min_hold_days', 1):
        return 'regime_bear', current_price
    # 4. RSI overbought
    if rsi_val > params.get('rsi_exit_threshold', 80) and hold_days >= 3:
        return 'rsi_exit', current_price

    return None, current_price


# =============================================================================
# KIS API HELPERS — US
# =============================================================================

def get_holdings_us(my_acct, my_prod, tickers, exchange_map):
    """Get US stock/ETF holdings across exchanges."""
    holdings = {}
    # Query each exchange that has tickers
    exchanges_to_query = set(exchange_map.get(t, 'AMEX') for t in tickers)
    for excg in exchanges_to_query:
        try:
            import inquire_balance as us_balance
            output1, _ = us_balance.inquire_balance(
                cano=my_acct, acnt_prdt_cd=my_prod,
                ovrs_excg_cd=excg, tr_crcy_cd="USD", env_dv="real"
            )
            if output1 is not None and not output1.empty:
                for _, row in output1.iterrows():
                    t = row['ovrs_pdno']
                    if t in tickers:
                        qty = int(row['ovrs_cblc_qty'])
                        holdings[t] = {
                            'quantity': qty,
                            'avg_price': float(row.get('frcr_pchs_amt1', 0)) / max(qty, 1),
                            'value': float(row.get('ovrs_stck_evlu_amt', 0)),
                        }
        except Exception as e:
            logger.error(f"Failed to get US holdings ({excg}): {e}")
    return holdings


def get_cash_us(my_acct, my_prod):
    """Get available USD cash."""
    try:
        import inquire_psamount
        df = inquire_psamount.inquire_psamount(
            cano=my_acct, acnt_prdt_cd=my_prod,
            ovrs_excg_cd="NASD", item_cd="SPY",
            ovrs_ord_unpr="0", env_dv="real"
        )
        if df is not None and not df.empty:
            return float(df.iloc[0]['ord_psbl_frcr_amt'])
    except Exception as e:
        logger.error(f"Failed to get US cash: {e}")
    return 0.0


def place_order_us(side, my_acct, my_prod, ticker, exchange, quantity, price):
    """Place US order via KIS overseas API."""
    logger.info(f"Placing {side.upper()}: {ticker} x {quantity} @ ~${price:.2f}")
    try:
        import order as us_order
        order_price = str(round(price * (1.01 if side == 'buy' else 0.99), 2))
        df_order = us_order.order(
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
# KIS API HELPERS — KOREA
# =============================================================================

def get_holdings_kr(my_acct, my_prod, tickers):
    """Get Korean ETF holdings."""
    holdings = {}
    try:
        from domestic_stock.inquire_balance import inquire_balance as kr_inquire
        output1, _ = kr_inquire.inquire_balance(
            env_dv="real", cano=my_acct, acnt_prdt_cd=my_prod,
            afhr_flpr_yn="N", inqr_dvsn="02", unpr_dvsn="01",
            fund_sttl_icld_yn="N", fncg_amt_auto_rdpt_yn="N", prcs_dvsn="00"
        )
        if output1 is not None and not output1.empty:
            for _, row in output1.iterrows():
                t = row['pdno']
                if t in tickers:
                    holdings[t] = {
                        'quantity': int(row['hldg_qty']),
                        'value': float(row['evlu_amt']),
                        'avg_price': float(row.get('pchs_avg_pric', 0)),
                        'profit': float(row.get('evlu_pfls_amt', 0)),
                        'profit_rate': float(row.get('evlu_pfls_rt', 0)),
                    }
    except Exception as e:
        logger.error(f"Failed to get KR holdings: {e}")
    return holdings


def get_cash_kr(my_acct, my_prod, sample_ticker='069500'):
    """Get available KRW buying power."""
    try:
        import inquire_psbl_order
        df_cash = inquire_psbl_order.inquire_psbl_order(
            env_dv="real", cano=my_acct, acnt_prdt_cd=my_prod,
            pdno=sample_ticker, ord_unpr="0", ord_dvsn="01",
            cma_evlu_amt_icld_yn="N", ovrs_icld_yn="Y"
        )
        if df_cash is not None and not df_cash.empty:
            return float(df_cash.iloc[0]['nrcvb_buy_amt'])
    except Exception as e:
        logger.error(f"Failed to get KR cash: {e}")
    return 0.0


def place_order_kr(side, my_acct, my_prod, ticker, quantity):
    """Place Korean market order via KIS domestic API."""
    logger.info(f"Placing {side.upper()}: {ticker} x {quantity}")
    try:
        from domestic_stock.order_cash.order_cash import order_cash
        result = order_cash(
            env_dv="real", ord_dv=side,
            cano=my_acct, acnt_prdt_cd=my_prod,
            pdno=ticker, ord_dvsn="01",  # Market order
            ord_qty=str(int(quantity)), ord_unpr="0",
            excg_id_dvsn_cd="KRX"
        )
        if result is None:
            logger.error(f"{side.upper()} failed: None response")
            return False
        # order_cash may return a DataFrame or a dict
        if isinstance(result, pd.DataFrame):
            if result.empty:
                logger.error(f"{side.upper()} failed: empty DataFrame")
                return False
            if 'rt_cd' in result.columns:
                rt_cd = str(result['rt_cd'].iloc[0])
                if rt_cd != '0':
                    msg1 = result['msg1'].iloc[0] if 'msg1' in result.columns else 'N/A'
                    logger.error(f"{side.upper()} API error: rt_cd={rt_cd}, msg={msg1}")
                    return False
            logger.info(f"{side.upper()} confirmed: {result.to_dict()}")
            return True
        elif isinstance(result, dict):
            rt_cd = str(result.get('rt_cd', ''))
            if rt_cd == '0':
                logger.info(f"{side.upper()} confirmed: rt_cd=0")
                return True
            logger.error(f"{side.upper()} API error: {result}")
            return False
        logger.warning(f"{side.upper()} unexpected result type: {type(result)} — {result}")
        return False
    except Exception as e:
        logger.error(f"{side.upper()} failed: {e}")
        return False


# =============================================================================
# SESSION RUNNERS
# =============================================================================

def _run_session(market, tickers, exchange_map, get_holdings_fn, get_cash_fn,
                 place_order_fn, config, state, notifier):
    """
    Unified trading session for both markets.
    market: 'kr' or 'us'
    """
    params = config.get('hybrid_vb_strategy', {})
    paper_trading = params.get('paper_trading', True)
    # Read budget from capital_management (canonical source), fallback to hybrid_vb_strategy
    cap_mgmt = config.get('capital_management', {})
    if market == 'us':
        base_budget = cap_mgmt.get('hybrid_vb_us', {}).get('budget_usd', 0) or params.get('budget_usd', 0)
    else:
        base_budget = cap_mgmt.get('hybrid_vb_kr', {}).get('budget_krw', 0) or params.get('budget_krw', 0)
    currency = 'USD' if market == 'us' else 'KRW'
    currency_sym = '$' if market == 'us' else '₩'
    now = datetime.now(ET if market == 'us' else KST)
    today = now.strftime("%Y-%m-%d")

    # Double-run guard
    last_run_key = f"last_run_{market}"
    if state.get(last_run_key) == today:
        logger.warning(f"Already ran {market.upper()} session today ({today}). Skipping.")
        return
    state[last_run_key] = today

    # Second Level Bot allocation override
    sl_mult, sl_regime, sl_score = load_allocation_override()
    budget = base_budget * sl_mult
    logger.info(f"Budget: {currency_sym}{base_budget:,.0f} × {sl_mult:.0%} ({sl_regime}) = {currency_sym}{budget:,.0f}")

    # Per-market vol-managed toggle (backtest showed KR benefits, US doesn't)
    use_vol_managed = params.get(f'vol_managed_{market}', market == 'kr')

    mkt_state = state[market]
    open_positions = mkt_state.get('open_positions', {})

    # --- Fetch OHLC data ---
    logger.info("Fetching market data...")
    ohlc_data = {}
    for t in tickers:
        yf_ticker = t if market == 'us' else f"{t}.KS"
        df = fetch_ohlc(yf_ticker, days=120)
        if df is not None and len(df) > params.get('regime_slow', 60) + 5:
            ohlc_data[t] = df
            logger.info(f"  {t}: {len(df)} bars, close={df['Close'].iloc[-1]:,.2f}")
        time.sleep(0.3)

    if not ohlc_data:
        logger.error("No market data available")
        notifier.send_error("No Market Data", f"[{market.upper()}] Failed to fetch OHLC")
        return

    # --- Account state ---
    try:
        ka.auth(svr="prod")
        logger.info("KIS API auth OK")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        notifier.send_error(f"Hybrid VB [{market.upper()}] Auth Failed", str(e))
        return

    acct = ka.getTREnv()
    my_acct, my_prod = acct.my_acct, acct.my_prod

    if market == 'us':
        holdings = get_holdings_fn(my_acct, my_prod, tickers, exchange_map)
    else:
        holdings = get_holdings_fn(my_acct, my_prod, tickers)
    cash = get_cash_fn(my_acct, my_prod)
    portfolio_value = cash + sum(h['value'] for h in holdings.values())
    capped_value = min(portfolio_value, budget) if budget > 0 else portfolio_value

    logger.info(f"Portfolio: {currency_sym}{portfolio_value:,.0f}, Budget: {currency_sym}{capped_value:,.0f}, Cash: {currency_sym}{cash:,.0f}")

    # DD scaling
    peak_eq = max(mkt_state.get('peak_equity', capped_value), capped_value)
    cur_dd = (capped_value - peak_eq) / peak_eq if peak_eq > 0 else 0
    dd_scale_start = params.get('dd_scale_start', 0.08)
    dd_floor = params.get('dd_floor', 0.30)
    if cur_dd < -dd_scale_start:
        dd_depth = abs(cur_dd) - dd_scale_start
        dd_range = 1.0 - dd_scale_start - dd_floor
        dd_scale = max(dd_floor, min(1.0, 1.0 - dd_depth / dd_range)) if dd_range > 0 else dd_floor
    else:
        dd_scale = 1.0

    logger.info(f"DD: {cur_dd:.1%}, DD Scale: {dd_scale:.2f}")

    trades_executed = []

    # --- EXIT LOOP ---
    for t in list(open_positions.keys()):
        if t not in ohlc_data:
            continue
        pos = open_positions[t]
        df = ohlc_data[t]

        reason, exit_price = check_exit_conditions(pos, df, params, now)
        if reason is None:
            continue

        shares = pos['shares']
        pnl = (exit_price - pos['entry_price']) * shares
        logger.info(f"EXIT {t}: {shares} shares @ {currency_sym}{exit_price:,.2f} ({reason}), PnL: {currency_sym}{pnl:+,.0f}")

        sell_ok = True
        if not paper_trading:
            if market == 'us':
                excg = exchange_map.get(t, 'AMEX') if isinstance(exchange_map, dict) else 'AMEX'
                actual_qty = holdings.get(t, {}).get('quantity', 0)
                if actual_qty > 0:
                    sell_ok = place_order_fn("sell", my_acct, my_prod, t, excg, min(shares, actual_qty), exit_price)
            else:
                actual_qty = holdings.get(t, {}).get('quantity', 0)
                if actual_qty > 0:
                    sell_ok = place_order_fn("sell", my_acct, my_prod, t, min(shares, actual_qty))

        if not sell_ok:
            logger.error(f"SELL order failed for {t}, keeping position in state for retry")
            continue

        trades_executed.append({
            'action': 'SELL', 'ticker': t, 'quantity': shares,
            'price': exit_price, 'value': shares * exit_price,
            'currency': currency,
            'reason': f'Exit: {reason}, PnL: {currency_sym}{pnl:+,.0f}'
        })

        mkt_state.setdefault('trade_history', []).append({
            'ticker': t, 'entry_date': pos['entry_date'],
            'entry_price': pos['entry_price'], 'exit_date': now.isoformat(),
            'exit_price': exit_price, 'shares': shares, 'pnl': pnl, 'reason': reason,
        })
        del open_positions[t]

    # Refresh cash after exits
    if trades_executed and not paper_trading:
        time.sleep(5)
        cash = get_cash_fn(my_acct, my_prod)

    # --- ENTRY LOOP ---
    heat = sum(
        pos.get('risk_amount', 0) / capped_value
        for pos in open_positions.values()
    ) if capped_value > 0 else 0

    regime_map = {}
    vol_mult_map = {}

    for t in tickers:
        if t in open_positions or t not in ohlc_data:
            continue

        df = ohlc_data[t]
        regime = calc_regime(df['Close'].iloc[:-1], params.get('regime_fast', 10),
                             params.get('regime_mid', 30), params.get('regime_slow', 60))
        regime_map[t] = REGIME_LABELS[regime]

        if regime == 0:
            logger.info(f"  {t}: BEAR regime, skip")
            continue

        signal, breakout_level, k_val, atr_val = check_vb_signal(df, params)

        # Vol multiplier — only applied for markets where it's enabled
        if use_vol_managed:
            vol_mult = calc_vol_size_multiplier(
                df['Close'], params.get('target_vol', 0.15),
                params.get('vol_lookback', 20), params.get('vol_size_cap', 2.0)
            )
        else:
            vol_mult = 1.0
        vol_mult_map[t] = vol_mult

        rsi_val = calc_rsi(df['Close'].iloc[:-1], 14)
        current_close = float(df['Close'].iloc[-1])

        logger.info(f"  {t}: regime={REGIME_LABELS[regime]}, k={k_val:.2f}, "
                     f"breakout={currency_sym}{breakout_level:,.2f}, "
                     f"high={currency_sym}{df['High'].iloc[-1]:,.2f}, "
                     f"RSI={rsi_val:.0f}, vol_mult={vol_mult:.2f}")

        if not signal:
            continue

        entry_price = breakout_level

        shares, stop_price, risk_budget = compute_position_size(
            entry_price, atr_val, capped_value, regime, vol_mult,
            params, heat, dd_scale, cash,
        )
        if shares <= 0:
            continue

        logger.info(f"ENTRY {t}: {shares} shares @ {currency_sym}{entry_price:,.2f}, "
                     f"stop={currency_sym}{stop_price:,.2f}, risk={currency_sym}{risk_budget:,.0f}, "
                     f"vol_mult={vol_mult:.2f}")

        buy_ok = True
        if not paper_trading:
            if market == 'us':
                excg = exchange_map.get(t, 'AMEX') if isinstance(exchange_map, dict) else 'AMEX'
                buy_ok = place_order_fn("buy", my_acct, my_prod, t, excg, shares, entry_price)
            else:
                buy_ok = place_order_fn("buy", my_acct, my_prod, t, shares)

        if not buy_ok:
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
        cash -= shares * entry_price

        trades_executed.append({
            'action': 'BUY', 'ticker': t, 'quantity': shares,
            'price': entry_price, 'value': shares * entry_price,
            'currency': currency,
            'reason': f'VB breakout ({REGIME_LABELS[regime]}, k={k_val:.2f}, vol_mult={vol_mult:.2f})'
        })

    # --- Save state ---
    mkt_state['open_positions'] = open_positions
    mkt_state['peak_equity'] = peak_eq
    state[market] = mkt_state
    save_state(state)

    # --- Regime map for tickers with positions ---
    for t in open_positions:
        if t in ohlc_data and t not in regime_map:
            r = calc_regime(ohlc_data[t]['Close'], params.get('regime_fast', 10),
                            params.get('regime_mid', 30), params.get('regime_slow', 60))
            regime_map[t] = REGIME_LABELS[r]

    # --- Write strategy result JSON ---
    strategy_name = f'HYBRID_VB_{market.upper()}'
    results_dir = os.path.join(project_root, 'strategy_results')
    os.makedirs(results_dir, exist_ok=True)

    # Re-fetch holdings post-trade
    if trades_executed and not paper_trading:
        try:
            time.sleep(3)
            if market == 'us':
                holdings = get_holdings_fn(my_acct, my_prod, tickers, exchange_map)
            else:
                holdings = get_holdings_fn(my_acct, my_prod, tickers)
        except Exception:
            pass

    holdings_list = []
    for t, h in holdings.items():
        cp = float(ohlc_data[t]['Close'].iloc[-1]) if t in ohlc_data else h['avg_price']
        profit = (cp - h['avg_price']) * h['quantity']
        profit_rate = ((cp / h['avg_price']) - 1) * 100 if h['avg_price'] > 0 else 0
        holdings_list.append({
            'ticker': t, 'quantity': h['quantity'], 'value': h['value'],
            'avg_price': h['avg_price'], 'current_price': cp,
            'profit': profit, 'profit_rate': profit_rate, 'currency': currency,
        })

    result_data = {
        'strategy_name': strategy_name,
        'timestamp': now.isoformat(),
        'allocation_mode': 'fixed',
        'capital_allocation': budget,
        'allocated_capital': budget,
        'total_value': capped_value,
        'cash_balance': cash,
        'total_profit': sum(h.get('profit', 0) for h in holdings_list),
        'holdings': holdings_list,
        'session_summary': {
            'orders_executed': len(trades_executed),
            'regime': regime_map,
            'drawdown': cur_dd,
            'dd_scale': dd_scale,
            'heat': heat,
            'heat_limit': params.get('heat_limit', 0.08),
            'paper_trading': paper_trading,
            'open_positions_count': len(open_positions),
            'vol_multipliers': vol_mult_map,
        },
    }

    result_file = os.path.join(results_dir, f'hybrid_vb_{market}_result.json')
    try:
        tmp_result = result_file + '.tmp'
        with open(tmp_result, 'w') as f:
            json.dump(result_data, f, indent=2, default=str)
        os.replace(tmp_result, result_file)
        logger.info(f"Result written: {result_file}")
    except Exception as e:
        logger.error(f"Failed to write result: {e}")

    # --- Discord notifications ---
    mode = "PAPER" if paper_trading else "LIVE"

    if trades_executed:
        notifier.send_trade_execution(trades_executed)

    # Position summary
    pos_text = "None (all cash)"
    if open_positions:
        lines = []
        for t, pos in open_positions.items():
            cp = float(ohlc_data[t]['Close'].iloc[-1]) if t in ohlc_data else pos['entry_price']
            pnl = (cp - pos['entry_price']) * pos['shares']
            vm = vol_mult_map.get(t, 1.0)
            lines.append(f"**{t}**: {pos['shares']}sh @ {currency_sym}{pos['entry_price']:,.2f} "
                         f"(stop {currency_sym}{pos['stop_price']:,.2f}, PnL {currency_sym}{pnl:+,.0f}, vm={vm:.1f})")
        pos_text = "\n".join(lines)

    regime_text = "  ".join(f"{t}: {r}" for t, r in regime_map.items())

    notifier._send(
        title=f"Hybrid VB [{market.upper()}] [{mode}] — Session Complete",
        fields=[
            {"name": "Portfolio", "value": f"{currency_sym}{capped_value:,.0f} (DD: {cur_dd:.1%})", "inline": True},
            {"name": "DD Scale", "value": f"{dd_scale:.0%}", "inline": True},
            {"name": "Heat", "value": f"{heat:.1%} / {params.get('heat_limit', 0.08):.0%}", "inline": True},
            {"name": "Positions", "value": pos_text, "inline": False},
            {"name": "Regime", "value": regime_text or "N/A", "inline": False},
            {"name": "Trades Today", "value": str(len(trades_executed)), "inline": True},
            {"name": "Mode", "value": mode, "inline": True},
        ],
        color=notifier.COLORS['info'],
        footer=f"Hybrid VB [{market.upper()}] | {now.strftime('%Y-%m-%d %H:%M')} {'KST' if market == 'kr' else 'ET'}"
    )


def run_us_session(config, state, notifier):
    """US trading session."""
    params = config.get('hybrid_vb_strategy', {})
    us_tickers_map = params.get('us_tickers', {})
    tickers = list(us_tickers_map.keys())
    exchange_map = us_tickers_map  # {ticker: exchange}

    logger.info(f"US session — {len(tickers)} ETFs: {', '.join(tickers)}")

    _run_session(
        market='us',
        tickers=tickers,
        exchange_map=exchange_map,
        get_holdings_fn=get_holdings_us,
        get_cash_fn=get_cash_us,
        place_order_fn=place_order_us,
        config=config,
        state=state,
        notifier=notifier,
    )


def run_kr_session(config, state, notifier):
    """Korea trading session."""
    now_kst = datetime.now(KST)

    # Weekend check
    if now_kst.weekday() >= 5:
        logger.warning(f"Weekend ({now_kst.strftime('%A')}). Aborting.")
        return

    # Holiday check
    if is_krx_holiday(now_kst):
        logger.warning(f"KRX holiday. Aborting.")
        return

    # Market hours guard (09:00 - 15:30 KST)
    hour, minute = now_kst.hour, now_kst.minute
    market_open = (hour > 9 or (hour == 9 and minute >= 0)) and (hour < 15 or (hour == 15 and minute <= 30))
    if not market_open:
        logger.warning(f"Outside KRX hours ({hour:02d}:{minute:02d} KST). Aborting.")
        return

    # Holiday table coverage check
    if now_kst.year not in KRX_HOLIDAYS:
        logger.error(f"KRX_HOLIDAYS missing for {now_kst.year}!")
        notifier.send_error("KRX Holiday Table Missing", f"Update KRX_HOLIDAYS for {now_kst.year}")
        return

    params = config.get('hybrid_vb_strategy', {})
    kr_tickers = params.get('kr_tickers', [])

    logger.info(f"KR session — {len(kr_tickers)} ETFs: {', '.join(kr_tickers)}")

    _run_session(
        market='kr',
        tickers=kr_tickers,
        exchange_map=None,
        get_holdings_fn=get_holdings_kr,
        get_cash_fn=get_cash_kr,
        place_order_fn=place_order_kr,
        config=config,
        state=state,
        notifier=notifier,
    )


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Hybrid VB Auto-Trader')
    parser.add_argument('--market', choices=['kr', 'us'], required=True)
    args = parser.parse_args()

    # Logger setup
    log_name = f'HybridVB_{args.market.upper()}'
    logger.name = log_name
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(os.path.join(project_root, 'hybrid_vb.log'))
        sh = logging.StreamHandler()
        fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)

    logger.info("=" * 60)
    logger.info(f"Hybrid VB Auto-Trader [{args.market.upper()}] - Start")
    logger.info("=" * 60)

    config = load_config()

    # Discord webhook — three-way fallback
    webhook_url = (config.get("DISCORD_WEBHOOK_URL")
                   or os.environ.get("DISCORD_WEBHOOK_URL")
                   or _load_env_file().get("DISCORD_WEBHOOK_URL"))
    notifier = DiscordNotifier(webhook_url, f"Hybrid VB [{args.market.upper()}]")

    state = load_state()

    if args.market == 'us':
        run_us_session(config, state, notifier)
    else:
        run_kr_session(config, state, notifier)

    logger.info("=" * 60)
    logger.info(f"Hybrid VB Auto-Trader [{args.market.upper()}] - Complete")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
