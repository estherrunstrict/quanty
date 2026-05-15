# -*- coding: utf-8 -*-
"""
Korea ETF Momentum Rotation Auto-Trader
========================================
Strategy: Multi-TF Dual Momentum (1/3/6/12m weighted)
Universe: KODEX Silver Futures (144600) + KODEX Gold Futures (132030)
         + TIGER 200 Construction (139220)
Rebalance: Monthly (first trading day of each month)
"""

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
import tempfile

# --- Path setup ---
current_dir = os.path.dirname(__file__)
project_root = os.path.abspath(os.path.join(current_dir))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_psbl_order'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'order_cash'))
sys.path.append(os.path.join(project_root, 'open-trading-api', 'examples_llm', 'domestic_stock', 'inquire_daily_itemchartprice'))

import kis_auth as ka
from domestic_stock.order_cash.order_cash import order_cash
import inquire_balance
import inquire_psbl_order
from inquire_daily_itemchartprice import inquire_daily_itemchartprice

from discord_notifier import DiscordNotifier

# --- Logger ---
logger = logging.getLogger('KoreaETFMomentum')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(os.path.join(project_root, 'korea_etf_momentum.log'))
    sh = logging.StreamHandler()
    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)

# --- State file for tracking ---
STATE_FILE = os.path.join(project_root, 'korea_etf_momentum_state.json')

# All datetime.now() calls MUST use KST — server runs UTC, market timing is KST.
KST = ZoneInfo("Asia/Seoul")

# --- KRX Market Holidays ---
# Key: year, Value: set of 'MMDD' strings.
# Weekends are excluded separately via weekday() check.
# Update each December for the following year.
KRX_HOLIDAYS = {
    2025: {
        '0101',              # 신정
        '0128', '0129', '0130',  # 설날 연휴
        '0301',              # 삼일절
        '0501',              # 근로자의 날 (Labor Day)
        '0505', '0506',      # 어린이날 + 대체공휴일
        '0606',              # 현충일
        '0815',              # 광복절
        '1003', '1006', '1007', '1008',  # 추석 연휴
        '1009',              # 한글날
        '1225',              # 크리스마스
        '1231',              # 연말 임시휴장
    },
    2026: {
        '0101',              # 신정
        '0127', '0128', '0129',  # 설날 연휴
        '0301',              # 삼일절
        '0501',              # 근로자의 날 (Labor Day)
        '0505',              # 어린이날
        '0606',              # 현충일
        '0815',              # 광복절
        '0924', '0925', '0928',  # 추석 연휴
        '1009',              # 한글날
        '1225',              # 크리스마스
        '1231',              # 연말 임시휴장
    },
}


def is_krx_holiday(date: datetime) -> bool:
    """Return True if date is a KRX market holiday (weekends checked separately)."""
    holidays = KRX_HOLIDAYS.get(date.year, set())
    return date.strftime('%m%d') in holidays


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


def _load_allocation_override():
    """Read allocation multiplier from SecondLevelBot. Returns 1.0 if stale or unavailable."""
    override_path = os.path.join(project_root, 'allocation_override.json')
    try:
        if os.path.exists(override_path):
            with open(override_path) as f:
                data = json.load(f)
            ts = data.get('timestamp', '')
            if ts:
                override_time = datetime.fromisoformat(ts)
                now = datetime.now(override_time.tzinfo or KST)
                # SecondLevelBot runs at ~15:55 ET (= ~04:55-05:55 KST next morning).
                # Korea ETF bot runs at 09:00 KST — 4h gap is structural, not stale.
                # Treat override as stale only when it is from a prior KST calendar date.
                override_kst_date = override_time.astimezone(KST).date()
                now_kst_date = now.astimezone(KST).date()
                if override_kst_date < now_kst_date:
                    age_hours = (now - override_time).total_seconds() / 3600
                    logger.warning(f"Second Level override is {age_hours:.1f}h old (prior day) — using default 1.0")
                    return 1.0
            mult = data.get('allocation_multiplier', 1.0)
            regime = data.get('regime', 'NEUTRAL')
            logger.info(f"Second Level override: {regime} → allocation {mult:.0%}")
            return mult
    except Exception as e:
        logger.warning(f"Failed to read allocation override: {e}")
    return 1.0


def load_config():
    with open(os.path.join(project_root, 'config.yaml'), 'r') as f:
        return yaml.safe_load(f)


def load_state():
    """Load persistent state (last rebalance date, current target, paper ledger)."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            # Backfill keys added in later versions
            state.setdefault('paper_holdings', {})
            state.setdefault('paper_cash', None)
            state.setdefault('target_qty', 0)  # shares owned by THIS bot (not combined account balance)
            return state
        except Exception as e:
            logger.error(f"State file corrupted ({e}), backing up and resetting")
            backup = STATE_FILE + '.corrupt'
            try:
                os.replace(STATE_FILE, backup)
            except OSError:
                pass
    return {
        'last_rebalance_month': None,  # None = first run, triggers initial rebalance
        'target_ticker': None,
        'target_name': None,
        'target_qty': 0,       # shares owned by THIS bot — not the full account balance
        'paper_holdings': {},  # {ticker: quantity} — used only in paper_trading mode
        'paper_cash': None,    # simulated cash balance for paper trading
    }


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def fetch_monthly_prices(ticker, months=14, current_ym=None):
    """Fetch monthly close prices using KIS API."""
    logger.info(f"Fetching {months}+ months of data for {ticker}...")
    if current_ym is None:
        current_ym = datetime.now(KST).strftime('%Y-%m')

    all_data = pd.DataFrame()
    end_date = datetime.now(KST)
    target_days = months * 25  # ~25 trading days per month

    max_retries = 3
    for attempt in range(max_retries):
        try:
            all_data = pd.DataFrame()
            fetch_end = end_date

            prev_fetch_end = None
            while len(all_data) < target_days:
                if fetch_end == prev_fetch_end:
                    logger.error(f"fetch_end not advancing for {ticker}, breaking loop")
                    break
                prev_fetch_end = fetch_end
                fetch_start = fetch_end - timedelta(days=150)

                _, df_chunk = inquire_daily_itemchartprice(
                    env_dv="real",
                    fid_cond_mrkt_div_code="J",
                    fid_input_iscd=ticker,
                    fid_input_date_1=fetch_start.strftime('%Y%m%d'),
                    fid_input_date_2=fetch_end.strftime('%Y%m%d'),
                    fid_period_div_code="D",
                    fid_org_adj_prc="1"
                )

                if df_chunk is None or df_chunk.empty:
                    break

                if all_data.empty:
                    all_data = df_chunk
                else:
                    all_data = pd.concat([df_chunk, all_data])

                all_data.drop_duplicates(subset=['stck_bsop_date'], inplace=True)

                oldest_date_str = df_chunk['stck_bsop_date'].min()
                first_date = pd.to_datetime(oldest_date_str, format='%Y%m%d')
                fetch_end = first_date - timedelta(days=1)

                time.sleep(0.5)  # API rate limit

            if all_data.empty:
                if attempt < max_retries - 1:
                    logger.warning(f"No data for {ticker}, retry {attempt+1}...")
                    time.sleep(60)
                    continue
                return None

            df = all_data[['stck_bsop_date', 'stck_clpr']].copy()
            df.rename(columns={'stck_bsop_date': 'Date', 'stck_clpr': 'Close'}, inplace=True)
            df['Date'] = pd.to_datetime(df['Date'], format='%Y%m%d')
            df.set_index('Date', inplace=True)
            df['Close'] = df['Close'].astype(float)
            df.sort_index(inplace=True)

            # Resample to monthly — 'ME' requires pandas >=2.2
            try:
                monthly = df['Close'].resample('ME').last().dropna()
            except ValueError:
                monthly = df['Close'].resample('M').last().dropna()

            # Drop the current partial month to ensure pure EOM momentum logic
            if monthly.index[-1].strftime('%Y-%m') == current_ym:
                monthly = monthly.iloc[:-1]
                
            logger.info(f"{ticker}: {len(df)} daily prices -> {len(monthly)} monthly prices")
            return monthly

        except Exception as e:
            logger.error(f"Error fetching {ticker} (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(60)

    return None


def calc_multitf_momentum(monthly_prices: dict):
    """
    Calculate Multi-TF Dual Momentum scores.
    Returns: {ticker: {'score': float, 'abs_mom_12m': float, 'qualified': bool, 'details': dict}}
    """
    results = {}

    any_valid = any(p is not None and len(p) >= 13 for p in monthly_prices.values())
    if not any_valid:
        raise RuntimeError("All ETF data fetches failed or returned insufficient history — aborting to prevent false CASH signal")

    for ticker, prices in monthly_prices.items():
        if prices is None or len(prices) < 13:
            logger.warning(f"{ticker}: Not enough data ({len(prices) if prices is not None else 0} months)")
            results[ticker] = {'score': 0, 'abs_mom_12m': 0, 'qualified': False, 'details': {}}
            continue

        current = prices.iloc[-1]

        # All lookback indices are guaranteed valid when len(prices) >= 13.
        m1  = current / prices.iloc[-2]  - 1
        m3  = current / prices.iloc[-4]  - 1
        m6  = current / prices.iloc[-7]  - 1
        m12 = current / prices.iloc[-13] - 1

        # Weighted composite score — weights sum to 1.0 (no partial zeroing possible).
        score = 0.4 * m1 + 0.3 * m3 + 0.2 * m6 + 0.1 * m12

        # Absolute momentum filter: 12m return > 0
        qualified = m12 > 0

        results[ticker] = {
            'score': score,
            'abs_mom_12m': m12,
            'qualified': qualified,
            'price': current,
            'details': {
                '1m': m1 * 100,
                '3m': m3 * 100,
                '6m': m6 * 100,
                '12m': m12 * 100,
                'score': score,
            }
        }

    return results


def get_target_allocation(momentum_data: dict):
    """Determine target: top 1 qualified ETF, or CASH."""
    qualified = {t: d for t, d in momentum_data.items() if d['qualified']}

    if qualified:
        best = max(qualified, key=lambda t: qualified[t]['score'])
        return best, momentum_data[best]
    else:
        return 'CASH', None


def get_holdings(my_acct, my_prod, tickers):
    """Get current holdings for our ETFs."""
    holdings = {}
    try:
        output1, output2 = inquire_balance.inquire_balance(
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
        logger.error(f"Failed to get holdings: {e}")

    return holdings


def get_cash_balance(my_acct, my_prod, ticker='144600'):
    """
    Get available buying power via inquire_psbl_order.
    nrcvb_buy_amt reflects brokerage real-time available balance,
    accounting for pending orders — more reliable than raw cash for post-sell sizing.
    """
    try:
        df_cash = inquire_psbl_order.inquire_psbl_order(
            env_dv="real", cano=my_acct, acnt_prdt_cd=my_prod,
            pdno=ticker, ord_unpr="0", ord_dvsn="01",
            cma_evlu_amt_icld_yn="N", ovrs_icld_yn="Y"
        )
        if df_cash is not None and not df_cash.empty:
            return float(df_cash.iloc[0]['nrcvb_buy_amt'])
    except Exception as e:
        logger.error(f"Failed to get cash: {e}")
    return 0.0


def fetch_current_price(ticker):
    """Fetch today's latest close price via KIS API (1-day chart)."""
    try:
        today = datetime.now(KST)
        _, df = inquire_daily_itemchartprice(
            env_dv="real", fid_cond_mrkt_div_code="J",
            fid_input_iscd=ticker,
            fid_input_date_1=(today - timedelta(days=5)).strftime('%Y%m%d'),
            fid_input_date_2=today.strftime('%Y%m%d'),
            fid_period_div_code="D", fid_org_adj_prc="1"
        )
        if df is not None and not df.empty:
            # Sort by date descending to ensure iloc[0] is most recent
            df = df.sort_values('stck_bsop_date', ascending=False)
            price = float(df.iloc[0]['stck_clpr'])  # Most recent day
            logger.info(f"Current price for {ticker}: {price:,.0f}")
            return price
    except Exception as e:
        logger.warning(f"Failed to fetch current price for {ticker}: {e}")
    return None


def place_order(side, my_acct, my_prod, ticker, quantity):
    """Execute buy or sell order. Returns True only on confirmed API success (rt_cd='0')."""
    logger.info(f"Placing {side.upper()} order: {ticker} x {quantity}")
    try:
        df_order = order_cash(
            env_dv="real", ord_dv=side,
            cano=my_acct, acnt_prdt_cd=my_prod,
            pdno=ticker, ord_dvsn="01",  # Market order
            ord_qty=str(int(quantity)), ord_unpr="0",
            excg_id_dvsn_cd="KRX"
        )
        if df_order is None or df_order.empty:
            logger.error(f"{side.upper()} failed: API returned empty/None response (likely network error)")
            return False
        rt_cd = str(df_order['rt_cd'].iloc[0])
        if rt_cd != '0':
            msg_cd = df_order['msg_cd'].iloc[0] if 'msg_cd' in df_order.columns else 'N/A'
            msg1 = df_order['msg1'].iloc[0] if 'msg1' in df_order.columns else 'N/A'
            logger.error(f"{side.upper()} API error: rt_cd={rt_cd}, msg_cd={msg_cd}, msg={msg1}")
            return False
        logger.info(f"{side.upper()} confirmed: rt_cd=0, order accepted")
        return True
    except Exception as e:
        logger.error(f"{side.upper()} failed: {e}")
        return False

def is_first_trading_day_of_month(today: datetime) -> bool:
    """True if today is the first valid trading day of the calendar month."""
    if is_krx_holiday(today) or today.weekday() >= 5:
        return False
        
    # Check all earlier days in the current month
    for d in range(1, today.day):
        past_day = today.replace(day=d)
        if not (is_krx_holiday(past_day) or past_day.weekday() >= 5):
            return False # A valid trading day happened before today
            
    return True


def is_rebalance_day(state):
    """
    Check if today is the first trading day of the month (KST).
    last_rebalance_month guard prevents double-firing.

    Special case: if last_rebalance_month is None (first ever run),
    force a rebalance immediately regardless of day-of-month so the
    bot establishes its initial position instead of waiting until the
    next month boundary.
    """
    today = datetime.now(KST)
    current_month = today.strftime('%Y-%m')
    last_rebalance = state.get('last_rebalance_month')

    if last_rebalance == current_month:
        logger.info(f"Already rebalanced this month ({current_month})")
        return False

    # First-ever run: force initial rebalance on any weekday
    if last_rebalance is None:
        if is_krx_holiday(today) or today.weekday() >= 5:
            logger.info("First run ever but today is a holiday/weekend — skipping initial rebalance")
            return False
        logger.info("First run ever — forcing initial rebalance to establish position")
        return True

    return is_first_trading_day_of_month(today)


def main():
    logger.info("=" * 60)
    logger.info("Korea ETF Momentum Rotation - Start")
    logger.info("=" * 60)

    config = load_config()
    params = config.get('korea_etf_momentum', {})
    webhook_url = config.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or _load_env_file().get("DISCORD_WEBHOOK_URL")
    notifier = DiscordNotifier(webhook_url, "Korea ETF Momentum")

    # Validate KRX holiday table covers the current year — abort if missing.
    # is_krx_holiday falls back to an empty set, meaning the bot would trade on holidays.
    _curr_year = datetime.now(KST).year
    if _curr_year not in KRX_HOLIDAYS:
        _msg = f"KRX_HOLIDAYS table missing for {_curr_year}. Bot may trade on market holidays! Update KRX_HOLIDAYS dict."
        logger.error(_msg)
        notifier.send_error(f"KRX Holiday Table Missing for {_curr_year}", _msg)
        return

    etfs = params.get('etfs', {
        '144600': 'KODEX Silver Futures',
        '132030': 'KODEX Gold Futures',
        '139220': 'TIGER 200 Construction',
    })
    # Capital budget from centralized capital_management
    cap_mgmt = config.get('capital_management', {}).get('korea_etf_momentum', {})
    base_budget = cap_mgmt.get('budget_krw', 0)  # 0 = no cap
    # Second Level Bot allocation override
    sl_mult = _load_allocation_override()
    budget_krw = int(base_budget * sl_mult) if base_budget > 0 else 0
    logger.info(f"Budget: ₩{base_budget:,} × {sl_mult:.0%} = ₩{budget_krw:,}")
    invest_ratio = params.get('invest_ratio', 0.95)
    paper_trading = params.get('paper_trading', True)

    tickers = list(etfs.keys())

    # Market hours guard: only allow trading during KRX hours (09:00–15:30 KST)
    now_kst_check = datetime.now(KST)
    hour, minute = now_kst_check.hour, now_kst_check.minute
    market_open = (hour > 9 or (hour == 9 and minute >= 0)) and \
                  (hour < 15 or (hour == 15 and minute <= 30))
    if not market_open:
        logger.warning(f"Outside KRX market hours ({hour:02d}:{minute:02d} KST). Aborting.")
        notifier._send(
            title="Skipped — Outside KRX Market Hours",
            fields=[{"name": "Current Time", "value": f"{now_kst_check.strftime('%H:%M')} KST (Market: 09:00-15:30)", "inline": True}],
            color=notifier.COLORS['warning'],
            footer=f"Korea ETF Momentum | {now_kst_check.strftime('%Y-%m-%d %H:%M')} KST"
        )
        return

    # Auth
    try:
        ka.auth(svr="prod")
        logger.info("KIS API auth OK")
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        notifier.send_error("KIS API Auth Failed", str(e))
        return

    acct_info = ka.getTREnv()
    my_acct, my_prod = acct_info.my_acct, acct_info.my_prod

    # Load persistent state
    state = load_state()

    # Determine current position.
    # - Live mode:  read from real brokerage account.
    # - Paper mode: read from simulated ledger in state file.
    #   Real account is never touched in paper mode.
    if paper_trading:
        paper_holdings = state.get('paper_holdings', {})
        nonzero = {t: q for t, q in paper_holdings.items() if q > 0}
        if len(nonzero) > 1:
            _msg = f"[PAPER] Inconsistent state: multiple holdings {nonzero}. Manual fix required."
            logger.error(_msg)
            notifier.send_error("Paper State Inconsistent", _msg)
            return
        current_holding = None
        current_qty = 0
        for t, qty in nonzero.items():
            current_holding = t
            current_qty = qty
            break
        holdings = {}  # not used in paper mode
        multi_holdings_conflict = False

        # Seed paper cash from real account on the very first paper run
        if state.get('paper_cash') is None:
            cash = get_cash_balance(my_acct, my_prod, tickers[0])
            state['paper_cash'] = min(cash, budget_krw) if budget_krw > 0 else cash
            save_state(state)  # Persist immediately — prevents re-seeding with different value on next non-rebalance run
            logger.info(f"[PAPER] First run — seeding paper cash from real account: {cash:,.0f}")
        cash = state['paper_cash']
    else:
        # Scope the holdings query to only the ticker this bot currently owns per state.
        # Other bots (e.g., HybridVB) may hold tickers that overlap with our universe —
        # querying the full universe would produce false multi-holdings conflicts.
        # On the very first run (no state target yet), scan the full universe to discover
        # any pre-existing position.
        state_target = state.get('target_ticker')
        check_tickers = [state_target] if state_target else tickers
        holdings = get_holdings(my_acct, my_prod, check_tickers)
        cash = get_cash_balance(my_acct, my_prod, tickers[0])
        # Multi-holding guard (should be unreachable in normal operation now that we scope
        # by state, but kept as a safety net for the first-run discovery path).
        nonzero_live = {t: h for t, h in holdings.items() if h['quantity'] > 0}
        multi_holdings_conflict = len(nonzero_live) > 1
        if multi_holdings_conflict:
            _msg = f"[LIVE] Inconsistent state: multiple holdings {list(nonzero_live.keys())}. Manual fix required before next rebalance."
            logger.error(_msg)
            notifier.send_error("Multiple Holdings Detected", _msg)
            # Don't hard-abort — reporting still runs; trade execution is blocked below.
        current_holding = None
        current_qty = 0
        for t, h in holdings.items():
            if h['quantity'] > 0:
                current_holding = t
                current_qty = h['quantity']
                break

        # One-time migration: if state has no target_qty yet, infer it from live balance.
        # This is safe at migration time because the current target (139220) is not in
        # HybridVB's universe, so the live qty is exclusively ours.
        if current_holding and state.get('target_qty', 0) == 0 and current_holding == state.get('target_ticker'):
            state['target_qty'] = current_qty
            logger.info(f"[LIVE] Inferred target_qty={current_qty} from live holdings (one-time state migration)")
            save_state(state)

    prefix = "[PAPER] " if paper_trading else ""
    logger.info(f"{prefix}Current holding: {current_holding} ({current_qty} shares)")
    logger.info(f"{prefix}Cash: {cash:,.0f}")

    # Fetch monthly prices — use a single consistent current_ym across all tickers
    logger.info("Fetching monthly price data...")
    current_ym = datetime.now(KST).strftime('%Y-%m')
    monthly_prices = {}
    for ticker in tickers:
        monthly_prices[ticker] = fetch_monthly_prices(ticker, months=14, current_ym=current_ym)
        time.sleep(1)  # Rate limit

    # Calculate momentum
    momentum = calc_multitf_momentum(monthly_prices)

    for t, m in momentum.items():
        d = m['details']
        logger.info(f"{t} ({etfs[t]}): 1m={d.get('1m',0):+.1f}% 3m={d.get('3m',0):+.1f}% "
                     f"6m={d.get('6m',0):+.1f}% 12m={d.get('12m',0):+.1f}% "
                     f"Score={d.get('score',0):+.4f} Qualified={m['qualified']}")

    # Determine target
    target_ticker, target_data = get_target_allocation(momentum)
    target_name = etfs.get(target_ticker, 'CASH')

    logger.info(f"Target: {target_ticker} ({target_name})")

    # Build momentum report
    mom_lines = []
    for t in sorted(momentum.keys(), key=lambda x: momentum[x]['score'], reverse=True):
        m = momentum[t]
        d = m['details']
        star = " **<-- TARGET**" if t == target_ticker else ""
        status = "OK" if m['qualified'] else "SKIP (12m < 0)"
        mom_lines.append(
            f"**{etfs[t]}** ({t})\n"
            f"1m: {d.get('1m',0):+.1f}% | 3m: {d.get('3m',0):+.1f}% | "
            f"6m: {d.get('6m',0):+.1f}% | 12m: {d.get('12m',0):+.1f}%\n"
            f"Score: {d.get('score',0):+.4f} | {status}{star}"
        )

    # Check if rebalance needed
    rebalance = is_rebalance_day(state)
    needs_trade = False

    if rebalance:
        if target_ticker == 'CASH':
            # Should have no positions
            needs_trade = current_holding is not None
        else:
            # Should hold target_ticker
            needs_trade = current_holding != target_ticker
    else:
        logger.info("Not a rebalance day, reporting only.")

    # Discord: Momentum Report
    report_fields = [
        {"name": "Strategy", "value": "Multi-TF Dual Momentum (Top 1)", "inline": True},
        {"name": "Rebalance Day", "value": "Yes" if rebalance else "No (report only)", "inline": True},
        {"name": "Current Position",
         "value": f"{etfs.get(current_holding, 'CASH')} ({current_qty} shares)" if current_holding else "CASH",
         "inline": True},
    ]

    for line in mom_lines:
        report_fields.append({"name": "Momentum", "value": line, "inline": False})

    if target_ticker != 'CASH':
        report_fields.append({
            "name": "Target",
            "value": f"**{target_name}** ({target_ticker}) | Score: {target_data['score']:+.4f}",
            "inline": False
        })
    else:
        report_fields.append({
            "name": "Target", "value": "**CASH** (no ETF qualifies)", "inline": False
        })

    action_text = "HOLD (no change needed)"
    color = notifier.COLORS['info']

    if rebalance and needs_trade:
        if target_ticker == 'CASH':
            action_text = f"SELL ALL {etfs.get(current_holding, current_holding)} -> CASH"
            color = notifier.COLORS['trade_sell']
        elif current_holding:
            action_text = f"ROTATE: {etfs.get(current_holding, current_holding)} -> {target_name}"
            color = notifier.COLORS['warning']
        else:
            action_text = f"BUY {target_name}"
            color = notifier.COLORS['trade_buy']
    elif rebalance and not needs_trade:
        action_text = "HOLD (already aligned)"
        # Persist rebalance month even when no trade is needed (e.g., target=CASH, already in CASH)
        # so the monthly guard is set and we don't re-evaluate every day this month.
        state['last_rebalance_month'] = datetime.now(KST).strftime('%Y-%m')
        state['target_ticker'] = target_ticker
        state['target_name'] = target_name
        save_state(state)
        logger.info(f"Rebalance complete (no trade needed): target={target_ticker}, state saved")

    report_fields.append({"name": "Action", "value": f"**{action_text}**", "inline": False})

    notifier._send(
        title="Momentum Analysis Report",
        fields=report_fields,
        color=color,
        footer=f"Korea ETF Momentum | {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST"
    )

    # Execute trades
    if rebalance and needs_trade and not paper_trading:
        if multi_holdings_conflict:
            logger.error("Trade execution blocked — multiple holdings inconsistency. Sell the extra position manually first.")
            notifier.send_error(
                "Trade Execution Blocked",
                f"Multiple holdings detected: {list(nonzero_live.keys())}. "
                "Sell the non-target position manually, then the bot will resume at next rebalance."
            )
            return
        logger.info("=== Executing rebalance trades ===")
        trades_executed = []

        # Step 1: Sell current holding if different from target
        sell_succeeded = True  # Assume no sell needed unless one is attempted
        if current_holding and current_holding != target_ticker:
            # Re-fetch to get fresh live qty, but sell only THIS bot's tracked quantity.
            # Another bot (e.g., HybridVB) may also hold current_holding — selling the
            # raw live balance would destroy its position. Use min(state_qty, live_qty).
            fresh_holdings = get_holdings(my_acct, my_prod, [current_holding])
            live_qty = fresh_holdings.get(current_holding, {}).get('quantity', current_qty)
            state_qty = state.get('target_qty', 0)
            if state_qty > 0:
                current_qty = min(state_qty, live_qty)
                logger.info(f"Sell qty: state={state_qty}, live={live_qty} → {current_qty} (cross-bot protection)")
            else:
                current_qty = live_qty  # migration fallback — no other bot owns this ticker at init
                logger.warning(f"Sell qty: state_qty=0 (fallback to live={live_qty})")
            logger.info(f"Selling {current_holding}: {current_qty} shares")
            success = place_order("sell", my_acct, my_prod, current_holding, current_qty)
            if success:
                state['target_qty'] = 0  # sold out; buy block will set to buy_qty if a buy follows
                trades_executed.append({
                    'action': 'SELL', 'ticker': current_holding,
                    'quantity': current_qty, 'name': etfs.get(current_holding, '')
                })
                # Wait for market order fill — poll until cash increases.
                # nrcvb_buy_amt updates on fill, but open-auction fills can take >10s.
                sell_price = fetch_current_price(current_holding) or momentum[current_holding].get('price', 0)
                expected_proceeds = current_qty * sell_price
                pre_sell_cash = cash
                deadline = time.time() + 60  # wait up to 60s
                while time.time() < deadline:
                    time.sleep(5)
                    new_cash = get_cash_balance(my_acct, my_prod, tickers[0])
                    if new_cash >= pre_sell_cash + expected_proceeds * 0.90:
                        cash = new_cash
                        logger.info(f"Sell fill confirmed: cash {pre_sell_cash:,.0f} -> {cash:,.0f}")
                        break
                else:
                    cash = get_cash_balance(my_acct, my_prod, tickers[0])
                    logger.warning(f"Sell fill not confirmed in 60s, proceeding with cash={cash:,.0f}")
            else:
                sell_succeeded = False
                logger.error("Sell order failed — aborting buy to avoid holding both ETFs simultaneously")
                notifier.send_error("Sell Failed", f"Sell of {current_holding} x{current_qty} rejected by API. Buy aborted.")

        # Step 2: Buy target if not CASH — only proceeds if sell was not attempted or sell succeeded
        if sell_succeeded and target_ticker != 'CASH' and target_ticker in tickers:
            capped_cash = min(cash, budget_krw) if budget_krw > 0 else cash
            available = capped_cash * invest_ratio
            # Use current market price for sizing (not stale month-end price)
            price = fetch_current_price(target_ticker) or momentum[target_ticker].get('price', 0)

            if price > 0:
                buy_qty = int(available / price)
                if buy_qty >= 1:
                    logger.info(f"Buying {target_ticker}: {buy_qty} shares @ ~{price:,.0f}")
                    success = place_order("buy", my_acct, my_prod, target_ticker, buy_qty)
                    if success:
                        state['target_qty'] = buy_qty  # record THIS bot's owned quantity
                        trades_executed.append({
                            'action': 'BUY', 'ticker': target_ticker,
                            'quantity': buy_qty, 'name': target_name,
                            'price': price, 'value': buy_qty * price
                        })
                else:
                    logger.error(f"Buy qty < 1 after sell (cash={available:,.0f}, price={price:,.0f}) — funds stranded in cash!")
                    notifier.send_error("Insufficient Funds for Buy Order",
                                        f"Available: ₩{available:,.0f}, Price: ₩{price:,.0f}. Funds remain in cash until next rebalance.")

        # Update state — only mark rebalance complete when sell did not fail.
        # Leaving last_rebalance_month unset allows a retry on the next run.
        if sell_succeeded:
            state['last_rebalance_month'] = datetime.now(KST).strftime('%Y-%m')
            state['target_ticker'] = target_ticker
            state['target_name'] = target_name
            save_state(state)
        else:
            logger.warning("Sell failed — skipping last_rebalance_month update to allow retry on next cron run")

        # Discord: Trade execution
        if trades_executed:
            trade_text = ""
            for t in trades_executed:
                if t['action'] == 'SELL':
                    trade_text += f"SELL {t['name']} ({t['ticker']}): {t['quantity']} shares\n"
                else:
                    trade_text += f"BUY {t['name']} ({t['ticker']}): {t['quantity']} shares @ {t.get('price',0):,.0f}\n"

            notifier._send(
                title="Rebalance Executed",
                fields=[
                    {"name": "Trades", "value": trade_text, "inline": False},
                    {"name": "Mode", "value": "LIVE", "inline": True},
                ],
                color=notifier.COLORS['success'],
                footer=f"Korea ETF Momentum | {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST"
            )

    elif rebalance and needs_trade and paper_trading:
        logger.info("=== [PAPER] Simulating rebalance trades ===")
        paper_holdings = state.get('paper_holdings', {})
        paper_cash_bal = state.get('paper_cash', cash)
        paper_trades = []

        # Simulate sell — use live price for realistic paper P&L
        if current_holding and current_holding != target_ticker:
            sell_price = fetch_current_price(current_holding) or momentum.get(current_holding, {}).get('price', 0)
            sell_qty = paper_holdings.get(current_holding, 0)
            if sell_qty > 0 and sell_price > 0:
                proceeds = sell_qty * sell_price
                paper_cash_bal += proceeds
                paper_holdings[current_holding] = 0
                msg = (f"SELL {etfs.get(current_holding, current_holding)}: "
                       f"{sell_qty} shares @ {sell_price:,.0f} \u2192 +{proceeds:,.0f}")
                paper_trades.append(msg)
                logger.info(f"[PAPER] {msg}")

        # Simulate buy — use live price for realistic paper fills
        if target_ticker != 'CASH' and target_ticker in tickers:
            buy_price = fetch_current_price(target_ticker) or momentum.get(target_ticker, {}).get('price', 0)
            if buy_price > 0:
                capped_paper = min(paper_cash_bal, budget_krw) if budget_krw > 0 else paper_cash_bal
                available = capped_paper * invest_ratio
                buy_qty = int(available / buy_price)
                if buy_qty >= 1:
                    cost = buy_qty * buy_price
                    paper_holdings[target_ticker] = paper_holdings.get(target_ticker, 0) + buy_qty
                    paper_cash_bal -= cost
                    msg = (f"BUY {target_name} ({target_ticker}): "
                           f"{buy_qty} shares @ {buy_price:,.0f} \u2192 -{cost:,.0f}")
                    paper_trades.append(msg)
                    logger.info(f"[PAPER] {msg}")
                else:
                    logger.warning(f"[PAPER] qty < 1 (available={available:,.0f}, price={buy_price:,.0f})")

        # Persist simulated ledger
        state['paper_holdings'] = paper_holdings
        state['paper_cash'] = paper_cash_bal
        state['last_rebalance_month'] = datetime.now(KST).strftime('%Y-%m')
        state['target_ticker'] = target_ticker
        state['target_name'] = target_name
        save_state(state)

        trade_summary = "\n".join(paper_trades) if paper_trades else action_text
        notifier._send(
            title="[PAPER] Rebalance Simulated",
            fields=[
                {"name": "Trades", "value": trade_summary, "inline": False},
                {"name": "Paper Cash After", "value": f"{paper_cash_bal:,.0f}", "inline": True},
            ],
            color=notifier.COLORS['warning'],
            footer=f"PAPER TRADING | {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST"
        )

    # Final status report
    now_kst = datetime.now(KST)
    if paper_trading:
        paper_holdings = state.get('paper_holdings', {})
        paper_cash_bal = state.get('paper_cash', 0)
        total_value = paper_cash_bal
        holdings_text = "None"
        h_lines = []
        for t, qty in paper_holdings.items():
            if qty > 0:
                price = momentum.get(t, {}).get('price', 0)
                val = qty * price
                total_value += val
                h_lines.append(f"{etfs.get(t, t)}: {qty} shares (est. {val:,.0f})")
        if h_lines:
            holdings_text = "\n".join(h_lines)

        notifier._send(
            title="[PAPER] Session Complete",
            fields=[
                {"name": "Paper Total (est.)", "value": f"{total_value:,.0f}", "inline": True},
                {"name": "Paper Cash", "value": f"{paper_cash_bal:,.0f}", "inline": True},
                {"name": "Holdings", "value": holdings_text, "inline": False},
            ],
            color=notifier.COLORS['info'],
            footer=f"PAPER TRADING | {now_kst.strftime('%Y-%m-%d %H:%M')} KST"
        )
    else:
        # Scope to KEM's own ticker only — avoids picking up HybridVB's shared holdings
        state_target = state.get('target_ticker')
        query_tickers = [state_target] if state_target else tickers[:1]
        final_holdings = get_holdings(my_acct, my_prod, query_tickers)

        kem_holdings_value = 0
        holdings_text = "None"
        if final_holdings:
            h_lines = []
            for t, h in final_holdings.items():
                if h['quantity'] > 0:
                    kem_holdings_value += h['value']
                    pnl_emoji = "+" if h['profit'] >= 0 else ""
                    h_lines.append(
                        f"{etfs.get(t, t)}: {h['quantity']}shares "
                        f"(avg {h['avg_price']:,.0f}, P/L {pnl_emoji}{h['profit']:,.0f} / {h['profit_rate']:.1f}%)"
                    )
            if h_lines:
                holdings_text = "\n".join(h_lines)

        # Estimated KEM cash = budget minus invested; full account cash is shared with HybridVB
        final_cash = max(0, budget_krw - kem_holdings_value)
        total_value = kem_holdings_value + final_cash

        notifier._send(
            title="Session Complete",
            fields=[
                {"name": "Total Value", "value": f"{total_value:,.0f}", "inline": True},
                {"name": "KEM Cash (est.)", "value": f"{final_cash:,.0f}", "inline": True},
                {"name": "Holdings", "value": holdings_text, "inline": False},
            ],
            color=notifier.COLORS['info'],
            footer=f"Korea ETF Momentum | {now_kst.strftime('%Y-%m-%d %H:%M')} KST"
        )

    # --- Write strategy result JSON (for dashboard) ---
    now_kst = datetime.now(KST)
    results_dir = os.path.join(project_root, 'strategy_results')
    os.makedirs(results_dir, exist_ok=True)

    result_holdings = []
    result_total_profit = 0
    if not paper_trading and final_holdings:
        for t, h in final_holdings.items():
            if h['quantity'] > 0:
                result_holdings.append({
                    'ticker': t,
                    'quantity': h['quantity'],
                    'value': h['value'],
                    'avg_price': h['avg_price'],
                    'current_price': h['value'] / h['quantity'] if h['quantity'] > 0 else 0,
                    'profit': h['profit'],
                    'profit_rate': h['profit_rate'],
                    'currency': 'KRW',
                })
                result_total_profit += h['profit']

    result_data = {
        'strategy_name': 'KOREA_ETF_MOMENTUM',
        'timestamp': now_kst.isoformat(),
        'allocation_mode': 'fixed',
        'capital_allocation': budget_krw,
        'allocated_capital': budget_krw,
        'total_value': total_value if not paper_trading else budget_krw,
        'cash_balance': final_cash if not paper_trading else 0,
        'total_profit': result_total_profit,
        'holdings': result_holdings,
        'session_summary': {
            'target_ticker': target_ticker,
            'target_name': target_name,
            'rebalance_day': rebalance,
            'paper_trading': paper_trading,
        },
    }

    result_file = os.path.join(results_dir, 'korea_etf_momentum_result.json')
    try:
        tmp = result_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(result_data, f, indent=2, default=str)
        os.replace(tmp, result_file)
        logger.info(f"Result written: {result_file}")
    except Exception as e:
        logger.error(f"Failed to write result: {e}")

    logger.info("=" * 60)
    logger.info("Korea ETF Momentum Rotation - Complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
