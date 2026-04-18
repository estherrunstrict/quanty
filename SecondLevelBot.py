#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Second Level Bot — Cycle-Aware Meta-Controller
================================================

Howard Marks' Second Level Thinking applied to portfolio allocation.

"The biggest investing errors come not from factors that are informational
or analytical, but from those that are psychological." — Howard Marks

This bot doesn't trade directly. It reads market cycle indicators, computes
a composite "cycle score" (-100 to +100), and writes an allocation_override.json
that all other trading bots read before sizing positions.

Euphoria (+70 to +100): Cut all bot budgets to 30% → "Be fearful when others are greedy"
Optimism (+40 to +70):  Reduce to 70%
Neutral  (-40 to +40):  Full 100% allocation
Fear     (-70 to -40):  Increase to 130%
Panic    (-100 to -70): Maximize to 160% → "Be greedy when others are fearful"

Additionally in extreme euphoria: buys TLT + GLD as hedges.
In extreme fear: sells hedges, lets momentum/VB bots run with extra capital.

Runs daily at 08:55 KST (before all other bots).

Usage:
  python SecondLevelBot.py
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml
import logging
import os
import json
import time

# --- Path setup ---
current_dir = os.path.dirname(__file__)
project_root = os.path.abspath(current_dir)

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

STATE_FILE = os.path.join(project_root, 'second_level_state.json')
OVERRIDE_FILE = os.path.join(project_root, 'allocation_override.json')

# --- Logger ---
logger = logging.getLogger('SecondLevel')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(os.path.join(project_root, 'second_level.log'))
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)


# =============================================================================
# CYCLE INDICATORS
# =============================================================================

def fetch_data(ticker, days=120):
    """Fetch OHLC from yfinance."""
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


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1 / period, min_periods=period).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = ag / al.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).iloc[-1]


def indicator_vix_level(vix_df):
    """
    VIX absolute level.
    Low VIX = complacency (euphoria signal), High VIX = fear.
    Score: -100 (extreme fear) to +100 (extreme euphoria)
    """
    if vix_df is None or vix_df.empty:
        return 0, "NO DATA"

    vix = float(vix_df['Close'].iloc[-1])

    if vix < 12:
        score = 90   # Extreme complacency
        desc = f"VIX {vix:.1f} — Extreme complacency"
    elif vix < 14:
        score = 60
        desc = f"VIX {vix:.1f} — Low vol, optimism"
    elif vix < 18:
        score = 20
        desc = f"VIX {vix:.1f} — Normal"
    elif vix < 25:
        score = -20
        desc = f"VIX {vix:.1f} — Elevated concern"
    elif vix < 30:
        score = -50
        desc = f"VIX {vix:.1f} — Fear"
    elif vix < 40:
        score = -75
        desc = f"VIX {vix:.1f} — High fear"
    else:
        score = -95
        desc = f"VIX {vix:.1f} — Panic"

    return score, desc


def indicator_vix_term_structure(vix_df, vix3m_df):
    """
    VIX / VIX3M ratio. Backwardation (>1.0) = fear, deep contango (<0.85) = euphoria.
    """
    if vix_df is None or vix3m_df is None:
        return 0, "NO DATA"

    vix = float(vix_df['Close'].iloc[-1])
    vix3m = float(vix3m_df['Close'].iloc[-1])

    if vix3m <= 0:
        return 0, "VIX3M zero"

    ratio = vix / vix3m

    if ratio > 1.10:
        score = -90
        desc = f"Backwardation {ratio:.2f} — Panic/stress"
    elif ratio > 1.0:
        score = -50
        desc = f"Mild backwardation {ratio:.2f} — Stress"
    elif ratio > 0.92:
        score = 0
        desc = f"Normal contango {ratio:.2f}"
    elif ratio > 0.85:
        score = 40
        desc = f"Contango {ratio:.2f} — Optimism"
    else:
        score = 80
        desc = f"Deep contango {ratio:.2f} — Complacency"

    return score, desc


def indicator_distance_from_200ma(spy_df):
    """
    SPY distance from 200-day MA. Far above = euphoria, far below = fear.
    """
    if spy_df is None or len(spy_df) < 200:
        return 0, "Insufficient data"

    close = float(spy_df['Close'].iloc[-1])
    ma200 = float(spy_df['Close'].rolling(200).mean().iloc[-1])

    if ma200 <= 0:
        return 0, "MA200 zero"

    pct = ((close / ma200) - 1) * 100

    if pct > 20:
        score = 90
        desc = f"SPY {pct:+.1f}% above 200MA — Extreme stretch"
    elif pct > 12:
        score = 60
        desc = f"SPY {pct:+.1f}% above 200MA — Extended"
    elif pct > 5:
        score = 20
        desc = f"SPY {pct:+.1f}% above 200MA — Healthy uptrend"
    elif pct > -5:
        score = 0
        desc = f"SPY {pct:+.1f}% from 200MA — Neutral"
    elif pct > -12:
        score = -30
        desc = f"SPY {pct:+.1f}% below 200MA — Correction"
    elif pct > -20:
        score = -60
        desc = f"SPY {pct:+.1f}% below 200MA — Bear territory"
    else:
        score = -90
        desc = f"SPY {pct:+.1f}% below 200MA — Crash"

    return score, desc


def indicator_index_rsi(spy_df):
    """
    RSI(14) on SPY. Overbought = euphoria, oversold = fear.
    """
    if spy_df is None or len(spy_df) < 20:
        return 0, "Insufficient data"

    rsi = calc_rsi(spy_df['Close'], 14)

    if rsi > 80:
        score = 85
        desc = f"RSI {rsi:.0f} — Extremely overbought"
    elif rsi > 70:
        score = 50
        desc = f"RSI {rsi:.0f} — Overbought"
    elif rsi > 55:
        score = 15
        desc = f"RSI {rsi:.0f} — Bullish"
    elif rsi > 45:
        score = 0
        desc = f"RSI {rsi:.0f} — Neutral"
    elif rsi > 30:
        score = -40
        desc = f"RSI {rsi:.0f} — Oversold"
    else:
        score = -85
        desc = f"RSI {rsi:.0f} — Extremely oversold"

    return score, desc


def indicator_kospi_regime(kospi_df):
    """
    KOSPI distance from 200MA + RSI — Korean market cycle gauge.
    """
    if kospi_df is None or len(kospi_df) < 200:
        return 0, "Insufficient data"

    close = float(kospi_df['Close'].iloc[-1])
    ma200 = float(kospi_df['Close'].rolling(200).mean().iloc[-1])
    rsi = calc_rsi(kospi_df['Close'], 14)

    if ma200 <= 0:
        return 0, "MA200 zero"

    pct = ((close / ma200) - 1) * 100

    # Blend distance + RSI
    dist_score = np.clip(pct * 5, -80, 80)
    rsi_score = (rsi - 50) * 1.5  # -75 to +75

    score = int((dist_score + rsi_score) / 2)
    score = np.clip(score, -100, 100)

    desc = f"KOSPI {pct:+.1f}% from 200MA, RSI {rsi:.0f}"
    return int(score), desc


# =============================================================================
# COMPOSITE SCORE
# =============================================================================

WEIGHTS = {
    'vix_level': 0.25,
    'vix_term': 0.20,
    'spy_200ma': 0.20,
    'spy_rsi': 0.15,
    'kospi': 0.20,
}

REGIME_MAP = {
    'PANIC':         {'range': (-100, -71),'multiplier': 1.60, 'emoji': '\U0001f7e3', 'action': 'Maximum 160%'},
    'FEAR':          {'range': (-70, -41), 'multiplier': 1.30, 'emoji': '\U0001f7e0', 'action': 'Increase to 130%'},
    'NEUTRAL':       {'range': (-40, 40),  'multiplier': 1.00, 'emoji': '\U0001f7e2', 'action': 'Full allocation'},
    'OPTIMISM':      {'range': (41, 70),   'multiplier': 0.70, 'emoji': '\U0001f7e1', 'action': 'Reduce to 70%'},
    'LATE_EUPHORIA': {'range': (71, 100),  'multiplier': 0.30, 'emoji': '\U0001f534', 'action': 'Cut risk to 30%'},
}


def compute_cycle_score(indicators):
    """Compute weighted composite cycle score from individual indicators."""
    total = 0
    total_weight = 0
    for key, (score, desc) in indicators.items():
        # Skip failed indicators so they don't dilute the score toward neutral
        if score == 0 and 'NO DATA' in str(desc):
            continue
        w = WEIGHTS.get(key, 0)
        total += score * w
        total_weight += w

    if total_weight > 0:
        composite = total / total_weight
    else:
        composite = 0

    return int(np.clip(composite, -100, 100))


def get_regime(score):
    """Map composite score to regime."""
    for name, cfg in REGIME_MAP.items():
        lo, hi = cfg['range']
        if lo <= score <= hi:
            return name, cfg
    if score > 0:
        return 'LATE_EUPHORIA', REGIME_MAP['LATE_EUPHORIA']
    return 'PANIC', REGIME_MAP['PANIC']


# =============================================================================
# ALLOCATION OVERRIDE
# =============================================================================

def write_allocation_override(regime_name, multiplier, score, indicators):
    """Write allocation_override.json for other bots to read."""
    override = {
        'timestamp': datetime.now(KST).isoformat(),
        'cycle_score': score,
        'regime': regime_name,
        'allocation_multiplier': multiplier,
        'indicators': {k: {'score': v[0], 'description': v[1]} for k, v in indicators.items()},
        'marks_quote': get_marks_quote(regime_name),
    }

    tmp = OVERRIDE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(override, f, indent=2)
    os.replace(tmp, OVERRIDE_FILE)

    logger.info(f"Allocation override written: {regime_name} → {multiplier:.0%}")
    return override


def get_marks_quote(regime):
    quotes = {
        'LATE_EUPHORIA': '"The less prudence with which others conduct their affairs, the greater the prudence with which we should conduct our own."',
        'OPTIMISM': '"Risk means more things can happen than will happen."',
        'NEUTRAL': '"You can\'t predict. You can prepare."',
        'FEAR': '"The best opportunities are usually found among things most others won\'t do."',
        'PANIC': '"The greatest investment opportunities come when things look worst."',
    }
    return quotes.get(regime, '"Second-level thinking is deep, complex, and convoluted."')


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'history': [],  # [{date, score, regime, multiplier}]
        'hedge_positions': {},  # {ticker: {shares, entry_price, entry_date}}
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


# =============================================================================
# DISCORD NOTIFICATION
# =============================================================================

def send_discord(webhook_url, override_data, indicators):
    """Send cycle assessment to Discord."""
    try:
        import requests

        regime = override_data['regime']
        score = override_data['cycle_score']
        mult = override_data['allocation_multiplier']
        cfg = REGIME_MAP.get(regime, REGIME_MAP['NEUTRAL'])

        # Build indicator lines
        ind_lines = []
        for key, (s, desc) in indicators.items():
            bar_len = int(abs(s) / 10)
            bar = '\u2588' * bar_len
            direction = '+' if s > 0 else ''
            ind_lines.append(f"`{key:<12}` {direction}{s:>4} {bar} {desc}")

        ind_text = "\n".join(ind_lines)

        # Score visualization
        needle_pos = int((score + 100) / 200 * 20)
        gauge = ['\u2591'] * 20
        needle_pos = max(0, min(19, needle_pos))
        gauge[needle_pos] = '\u2588'
        gauge_str = ''.join(gauge)

        # Color based on regime
        colors = {
            'LATE_EUPHORIA': 15158332,  # Red
            'OPTIMISM': 16776960,       # Yellow
            'NEUTRAL': 3066993,         # Green
            'FEAR': 15105570,           # Orange
            'PANIC': 10181046,          # Purple
        }

        payload = {
            "embeds": [{
                "title": f"{cfg['emoji']} Second Level: {regime} (Score: {score:+d})",
                "description": (
                    f"**Allocation: {mult:.0%} of normal**\n"
                    f"```\nFEAR [{gauge_str}] GREED\n     Score: {score:+d}/100\n```\n"
                    f"{cfg['action']}\n\n"
                    f"**Indicators:**\n{ind_text}\n\n"
                    f"*{override_data['marks_quote']}*"
                ),
                "color": colors.get(regime, 3447003),
                "footer": {"text": f"Second Level Bot | {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST"},
            }]
        }

        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code in (200, 204):
            logger.info("Discord notification sent")
        else:
            logger.error(f"Discord failed: {resp.status_code}")
    except Exception as e:
        logger.error(f"Discord error: {e}")


# =============================================================================
# CONFIG
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


# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("=" * 60)
    logger.info("Second Level Bot — Cycle Assessment Start")
    logger.info("=" * 60)

    config = load_config()
    webhook_url = (config.get("DISCORD_WEBHOOK_URL")
                   or os.environ.get("DISCORD_WEBHOOK_URL")
                   or _load_env_file().get("DISCORD_WEBHOOK_URL"))

    state = load_state()

    # Fetch market data
    logger.info("Fetching cycle indicators...")
    spy_df = fetch_data("SPY", days=250)
    vix_df = fetch_data("^VIX", days=30)
    vix3m_df = fetch_data("^VIX3M", days=30)
    kospi_df = fetch_data("^KS11", days=250)

    # Compute indicators
    indicators = {}
    indicators['vix_level'] = indicator_vix_level(vix_df)
    indicators['vix_term'] = indicator_vix_term_structure(vix_df, vix3m_df)
    indicators['spy_200ma'] = indicator_distance_from_200ma(spy_df)
    indicators['spy_rsi'] = indicator_index_rsi(spy_df)
    indicators['kospi'] = indicator_kospi_regime(kospi_df)

    for key, (score, desc) in indicators.items():
        logger.info(f"  {key:<12}: {score:>+4}  {desc}")

    # Composite score
    cycle_score = compute_cycle_score(indicators)
    regime_name, regime_cfg = get_regime(cycle_score)
    multiplier = regime_cfg['multiplier']

    logger.info(f"Composite Score: {cycle_score:+d}")
    logger.info(f"Regime: {regime_name} → Allocation: {multiplier:.0%}")
    logger.info(f"Marks: {get_marks_quote(regime_name)}")

    # Write allocation override
    override = write_allocation_override(regime_name, multiplier, cycle_score, indicators)

    # Update state history
    today = datetime.now(KST).strftime('%Y-%m-%d')
    state['history'].append({
        'date': today,
        'score': cycle_score,
        'regime': regime_name,
        'multiplier': multiplier,
    })
    # Keep last 365 days
    state['history'] = state['history'][-365:]
    save_state(state)

    # Discord notification
    if webhook_url:
        send_discord(webhook_url, override, indicators)

    logger.info("=" * 60)
    logger.info("Second Level Bot — Complete")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
