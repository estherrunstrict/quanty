# -*- coding: utf-8 -*-
"""
JD Investment Strategy - Real-World Trading Bot
===============================================

Implements the JD Wealth Research Institute's investment strategy for live trading.

Core Strategy:
1. Hold world's #1 stock by market cap (AAPL/MSFT)
2. Three-State Model: Normal, Alert, Panic
3. Signal: NASDAQ Composite Index -3% drops
4. Rebalancing Protocol: Progressive cash building at -2.5%, -5%, -7.5%
5. Stake-in Protocol: Systematic buying during crashes
6. Crisis Detection: 4 or more -3% drops in 30 days = Panic

Safety Features:
- Paper trading mode for testing
- Maximum position limits
- Daily execution only
- Detailed logging and Discord notifications
"""

import pandas as pd
from datetime import datetime, timezone, timedelta
import sys
import yaml
import logging
import requests
import os
import numpy as np
import yfinance as yf
import matplotlib.pyplot as plt
import json
from typing import Dict, List, Tuple, Optional
from enum import Enum

# --- Path Setup ---
current_dir = os.path.dirname(__file__)
project_root = os.path.abspath(os.path.join(current_dir))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'overseas_stock', 'inquire_balance'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'overseas_stock', 'inquire_psamount'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'overseas_stock', 'dailyprice'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'overseas_stock', 'order'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'overseas_stock', 'inquire_daily_chartprice'))
sys.path.append(os.path.join(project_root, 'open-trading-api','examples_llm', 'domestic_stock', 'inquire_account_balance'))

# --- KIS API Modules ---
import kis_auth as ka
import order
import inquire_balance
import inquire_daily_chartprice as idc
import inquire_psamount
import inquire_account_balance as iab

# --- Position Tracking ---
from strategy_positions import get_tracker

# Discord notifications
from discord_notifier import get_notifier

# --- Portfolio Summary (for dual-strategy mode) ---
from portfolio_summary import send_portfolio_summary

# --- Logger Setup ---
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = logging.FileHandler('jd_strategy_trader.log')
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


class MarketState(Enum):
    """Three states of market condition"""
    NORMAL = "normal"
    ALERT = "alert"
    PANIC = "panic"


class FedRateRegime(Enum):
    """Federal Reserve monetary policy regime"""
    EASING = "easing"
    TIGHTENING = "tightening"


class JDStrategyTrader:
    """
    JD Investment Strategy Real-World Trading Bot

    Implements the complete JD Strategy for live trading with KIS API
    """

    def __init__(self, paper_trading=False):
        """Initialize the JD Strategy trading bot"""
        self.paper_trading = paper_trading
        self.config = self._load_config()

        # Discord notifications (NEW: Using professional notifier)
        webhook_url = self.config.get("DISCORD_WEBHOOK_URL")
        self.notifier = get_notifier(webhook_url, bot_type='jd_strategy')

        self.my_acct = None
        self.my_prod = None
        self.usd_krw_exchange_rate = None

        # Dual Strategy Configuration
        dual_config = self.config.get('dual_strategy', {})
        self.dual_strategy_enabled = dual_config.get('ENABLED', False)

        # Capital allocation: read from centralized capital_management
        cap_mgmt = self.config.get('capital_management', {}).get('jd_strategy', {})
        budget_usd = cap_mgmt.get('budget_usd', 0.0)
        # Second Level Bot allocation override
        sl_mult = self._load_allocation_override()
        budget_usd *= sl_mult
        if budget_usd > 0:
            self.allocation_mode = 'fixed'
            self.allocated_capital_fixed = budget_usd
            self.capital_allocation = None
            logger.info(f"JD Strategy using FIXED capital budget: ${self.allocated_capital_fixed:,.2f} (SL: {sl_mult:.0%})")
        else:
            # Fallback: no cap, use all available
            self.allocation_mode = 'percentage'
            self.capital_allocation = 1.0
            self.allocated_capital_fixed = None
            logger.info(f"JD Strategy using ALL available capital (no budget cap)")

        # JD Strategy Parameters from config
        jd_config = self.config.get('jd_strategy', {})
        self.TOP_STOCK_CANDIDATES = jd_config.get('TOP_STOCK_CANDIDATES', ['AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN'])
        self.MARKET_CAP_THRESHOLD = jd_config.get('MARKET_CAP_THRESHOLD', 0.10)

        # Core Rules
        self.MINUS_3_THRESHOLD = jd_config.get('MINUS_3_THRESHOLD', -3.0)
        self.PANIC_THRESHOLD = jd_config.get('PANIC_THRESHOLD', 4)
        self.ROLLING_WINDOW_DAYS = jd_config.get('ROLLING_WINDOW_DAYS', 30)

        # Rebalancing Protocol
        self.REBALANCING_THRESHOLDS = jd_config.get('REBALANCING_THRESHOLDS', {
            -2.5: 0.10,
            -5.0: 0.20,
            -7.5: 0.30,
        })

        # Stake-in Protocol (CORRECTED: Use target allocations, not incremental percentages)
        # 25% Plan (Easing: Fed rate <= 0.25%) - 5 levels
        self.STAKE_25_INTERVALS = jd_config.get('STAKE_25_INTERVALS', [-5.0, -10.0, -15.0, -20.0, -25.0])
        self.STAKE_25_TARGETS = jd_config.get('STAKE_25_TARGETS', [0.05, 0.10, 0.15, 0.20, 0.25])  # Target stock allocation

        # 50% Plan (Tightening: Fed rate > 0.25%) - 20 levels
        self.STAKE_50_INTERVALS = jd_config.get('STAKE_50_INTERVALS', [
            -2.5, -5.0, -7.5, -10.0, -12.5, -15.0, -17.5, -20.0, -22.5, -25.0,
            -27.5, -30.0, -32.5, -35.0, -37.5, -40.0, -42.5, -45.0, -47.5, -50.0
        ])
        self.STAKE_50_TARGETS = jd_config.get('STAKE_50_TARGETS', [
            0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
            0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00
        ])  # Target stock allocation

        # Exit Conditions (CORRECTED: Per guide, exit when 30-day window has 0 -3% events)
        self.CRISIS_EXIT_DAYS = 30  # Exit when rolling 30-day window has 0 -3% events
        self.EARLY_EXIT_UP_DAYS = jd_config.get('EARLY_EXIT_UP_DAYS', 8)

        # Safety Limits
        self.MAX_PORTFOLIO_VALUE = 1000000  # $1M max portfolio (sanity check)
        self.MIN_TRADE_SIZE_USD = jd_config.get('MIN_TRADE_SIZE_USD', 10)
        self.MAX_SINGLE_TRADE_PCT = jd_config.get('MAX_SINGLE_TRADE_PCT', 0.30)
        self.MAX_DAILY_TRADES = jd_config.get('MAX_DAILY_TRADES', 20)

        # State tracking file
        self.state_file = os.path.join(project_root, 'jd_strategy_state.json')
        self.state = self._load_state()

        # Market cap rebalancing frequency (days)
        self.REBALANCE_FREQUENCY_DAYS = 90  # Check market cap every 90 days

        # Exchange mapping
        self.asset_exchange_map = {
            "AAPL": "NASD",
            "MSFT": "NASD",
            "GOOGL": "NASD",
            "NVDA": "NASD",
            "AMZN": "NASD"
        }

        # Initialize daily trade counter
        self.daily_trade_count = 0
        self.last_trade_date = None

        # Position tracker for dual strategy mode
        self.position_tracker = get_tracker()
        self.strategy_name = "JD_STRATEGY"

        # Results directory for portfolio summary
        self.results_dir = os.path.join(project_root, 'strategy_results')
        os.makedirs(self.results_dir, exist_ok=True)
        self.result_file = os.path.join(self.results_dir, 'jd_strategy_result.json')

        if not self.paper_trading:
            self._authenticate()

    def _load_allocation_override(self):
        """Read allocation multiplier from SecondLevelBot. Returns 1.0 if stale or unavailable."""
        override_path = os.path.join(project_root, 'allocation_override.json')
        try:
            if os.path.exists(override_path):
                with open(override_path) as f:
                    data = json.load(f)
                ts = data.get('timestamp', '')
                if ts:
                    from datetime import datetime as dt, timezone
                    override_time = dt.fromisoformat(ts)
                    now = dt.now(override_time.tzinfo or timezone.utc)
                    age_hours = (now - override_time).total_seconds() / 3600
                    if age_hours > 2:
                        logger.warning(f"Second Level override is {age_hours:.1f}h old — using default 1.0")
                        return 1.0
                mult = data.get('allocation_multiplier', 1.0)
                regime = data.get('regime', 'NEUTRAL')
                logger.info(f"Second Level override: {regime} → allocation {mult:.0%}")
                return mult
        except Exception as e:
            logger.warning(f"Failed to read allocation override: {e}")
        return 1.0

    def _load_config(self):
        """Load configuration from config.yaml"""
        try:
            with open(os.path.join(project_root, 'config.yaml'), 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("config.yaml file not found")
            sys.exit(1)
        except yaml.YAMLError as e:
            logger.error(f"config.yaml parsing error: {e}")
            sys.exit(1)

    def _authenticate(self):
        """Perform API authentication"""
        try:
            ka.auth(svr="prod")
            logger.info("✅ API authentication successful")
            acct_info = ka.getTREnv()
            self.my_acct, self.my_prod = acct_info.my_acct, acct_info.my_prod
            logger.info(f"Account: {self.my_acct}, Product: {self.my_prod}")
        except Exception as e:
            logger.error(f"❌ API authentication failed: {e}")
            self.notifier.send_error("API authentication failed", str(e))
            sys.exit(1)

    def _load_state(self):
        """Load persistent strategy state from JSON file"""
        default_state = {
            'current_state': MarketState.NORMAL.value,
            'portfolio_ath': 0.0,
            'world_1_stock_ath': 0.0,  # ATH of world #1 stock PRICE for rebalancing
            'current_world_1_ticker': None,  # Current world #1 stock ticker
            'nasdaq_ath_at_alert': 0.0,
            'minus_3_dates': [],
            'last_minus_3_date': None,
            'stake_levels_executed': [],
            'current_holdings': [],
            'fed_regime': FedRateRegime.EASING.value,
            'consecutive_up_days': 0,
            'last_rebalance_date': None,
            'initial_investment_made': False
        }
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    logger.info(f"Loaded strategy state: {state}")
                    # Add missing keys for backward compatibility
                    for key, value in default_state.items():
                        if key not in state:
                            state[key] = value
                    return state
            except Exception as e:
                logger.warning(f"Failed to load state file: {e}, creating new state")

        # Default state
        return default_state

    def _save_state(self):
        """Save strategy state to JSON file (atomic write via tmp + replace)"""
        try:
            tmp = self.state_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.state, f, indent=2, default=str)
            os.replace(tmp, self.state_file)
            logger.info("Strategy state saved successfully")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    # OLD Discord method removed - now using self.notifier from discord_notifier.py

    def _send_market_cap_analysis(self, market_cap_info, account_summary):
        """Send detailed market cap analysis and investment strategy to Discord"""
        if not market_cap_info:
            return

        # Build rankings text with ATH info
        rankings_text = ""
        stock_details = market_cap_info.get('stock_details', {})

        if 'all_rankings' in market_cap_info:
            for i, (ticker, cap) in enumerate(market_cap_info['all_rankings'], 1):
                ranking_line = f"**#{i}. {ticker}**: ${cap/1e12:.2f}T"

                # Add ATH info if available
                if ticker in stock_details:
                    details = stock_details[ticker]
                    current_price = details['current_price']
                    ath_price = details['ath_price']
                    drawdown = details['drawdown_from_ath']

                    # Add price and drawdown info
                    if drawdown < 0:  # Below ATH
                        ranking_line += f"\n    💲 ${current_price:.2f} (ATH: ${ath_price:.2f}, {drawdown:.1f}%)"
                    else:  # At or near ATH
                        ranking_line += f"\n    💲 ${current_price:.2f} ✨ AT/NEAR ATH!"

                rankings_text += ranking_line + "\n"

        # Calculate current state description
        current_state = MarketState(self.state['current_state'])
        portfolio_value = account_summary.get('total_value', 0)
        cash_balance = account_summary.get('cash_balance_total_usd', 0)  # USD + KRW converted
        cash_ratio = (cash_balance / portfolio_value * 100) if portfolio_value > 0 else 0

        # Build strategy explanation
        if gap_pct := market_cap_info.get('gap_pct', 0):
            if gap_pct > 10:
                strategy_explanation = (
                    f"**10% Gap Rule Applied**: {market_cap_info['top_1_ticker']} leads by **{gap_pct:.1f}%** (>{self.MARKET_CAP_THRESHOLD*100:.0f}%)\n"
                    f"→ **Concentrate 100%** in market leader ({market_cap_info['top_1_ticker']})"
                )
            else:
                strategy_explanation = (
                    f"**10% Gap Rule Applied**: {market_cap_info['top_1_ticker']} leads by **{gap_pct:.1f}%** (≤{self.MARKET_CAP_THRESHOLD*100:.0f}%)\n"
                    f"→ **Diversify** between top 2 stocks by market cap weight:\n"
                    f"  • {market_cap_info['top_1_ticker']}: **{market_cap_info['weight_1']*100:.1f}%**\n"
                    f"  • {market_cap_info['top_2_ticker']}: **{market_cap_info['weight_2']*100:.1f}%**"
                )
        else:
            strategy_explanation = "Strategy calculation pending"

        # Market state explanation
        state_emoji = {"normal": "🟢", "alert": "🟡", "panic": "🔴"}
        state_description = {
            "normal": "Operating normally with rebalancing protocol",
            "alert": "Crisis detected - stake-in protocol monitoring",
            "panic": "Multiple crises - holding cash until recovery"
        }

        # Build detailed info for top 2 stocks
        top_1_ticker = market_cap_info.get('top_1_ticker', 'N/A')
        top_2_ticker = market_cap_info.get('top_2_ticker', 'N/A')

        top_1_value = f"Market Cap: ${market_cap_info.get('top_1_cap', 0)/1e12:.2f}T"
        top_2_value = f"Market Cap: ${market_cap_info.get('top_2_cap', 0)/1e12:.2f}T"

        # Add ATH details for top stocks
        if top_1_ticker in stock_details:
            details = stock_details[top_1_ticker]
            top_1_value += f"\nPrice: ${details['current_price']:.2f}"
            top_1_value += f"\nATH: ${details['ath_price']:.2f}"
            if details['drawdown_from_ath'] < 0:
                top_1_value += f"\nFrom ATH: {details['drawdown_from_ath']:.1f}%"
            else:
                top_1_value += f"\n✨ AT/NEAR ATH!"

        if top_2_ticker in stock_details:
            details = stock_details[top_2_ticker]
            top_2_value += f"\nPrice: ${details['current_price']:.2f}"
            top_2_value += f"\nATH: ${details['ath_price']:.2f}"
            if details['drawdown_from_ath'] < 0:
                top_2_value += f"\nFrom ATH: {details['drawdown_from_ath']:.1f}%"
            else:
                top_2_value += f"\n✨ AT/NEAR ATH!"

        fields = [
            {
                "name": "📊 **Market Cap Rankings (Top 5)**",
                "value": rankings_text if rankings_text else "Data not available",
                "inline": False
            },
            {
                "name": f"🥇 **#1: {top_1_ticker}**",
                "value": top_1_value,
                "inline": True
            },
            {
                "name": f"🥈 **#2: {top_2_ticker}**",
                "value": top_2_value,
                "inline": True
            },
            {
                "name": "📏 **Gap**",
                "value": f"{market_cap_info.get('gap_pct', 0):.1f}%",
                "inline": True
            },
            {
                "name": "🎯 **Investment Strategy (JD 10% Rule)**",
                "value": strategy_explanation,
                "inline": False
            },
            {
                "name": f"{state_emoji.get(current_state.value, '⚪')} **Current Market State**",
                "value": f"**{current_state.value.upper()}**\n{state_description.get(current_state.value, 'Unknown state')}",
                "inline": False
            },
            {
                "name": "💼 **Portfolio Status**",
                "value": (
                    f"Total: **${portfolio_value:,.2f}**\n"
                    f"Cash: **${cash_balance:,.2f}** ({cash_ratio:.1f}%)"
                ),
                "inline": False
            }
        ]

        # Add current holdings if any
        if account_summary.get('stocks'):
            holdings_text = ""
            for stock in account_summary['stocks']:
                holdings_text += f"**{stock['ticker']}**: {stock['quantity']} shares (${stock['value']:,.2f})\n"
            fields.append({
                "name": "📈 **Current Holdings**",
                "value": holdings_text,
                "inline": False
            })

        # Send market cap analysis using notifier
        self.notifier._send(
            title="Market Cap Analysis",
            fields=fields,
            color=3447003  # info
        )

    def _get_exchange_rate(self):
        """Get USD/KRW exchange rate"""
        try:
            exchange_rate_data = yf.download('USDKRW=X', period='5d')
            if not exchange_rate_data.empty:
                self.usd_krw_exchange_rate = exchange_rate_data['Close'].iloc[-1].item()
                logger.info(f"✅ USD/KRW exchange rate: {self.usd_krw_exchange_rate:,.2f}")
            else:
                raise ValueError("Exchange rate fetch failed")
        except Exception as e:
            logger.warning(f"yfinance exchange rate failed: {e}, using fallback")
            self.usd_krw_exchange_rate = 1300

    def get_top_stocks_by_market_cap(self) -> Tuple[List[Tuple[str, float]], Dict]:
        """
        Get top stocks by market cap with 10% rule

        Returns: (List of (ticker, weight) tuples, market_cap_info dict)
        """
        logger.info("=== Market Cap Ranking Analysis ===")
        market_caps = {}
        stock_details = {}

        for ticker in self.TOP_STOCK_CANDIDATES:
            try:
                stock = yf.Ticker(ticker)
                info = stock.info
                market_cap = info.get('marketCap')

                if market_cap and market_cap > 0:
                    market_caps[ticker] = float(market_cap)
                    logger.info(f"  {ticker}: ${market_cap/1e12:.2f}T")

                    # Get price data for ATH calculation
                    try:
                        hist = stock.history(period="1y")
                        if not hist.empty:
                            current_price = hist['Close'].iloc[-1]
                            ath_price = hist['Close'].max()
                            ath_date = hist['Close'].idxmax()
                            drawdown_from_ath = ((current_price - ath_price) / ath_price) * 100

                            stock_details[ticker] = {
                                'current_price': current_price,
                                'ath_price': ath_price,
                                'ath_date': ath_date,
                                'drawdown_from_ath': drawdown_from_ath
                            }
                            logger.info(f"    Current: ${current_price:.2f}, ATH: ${ath_price:.2f} ({ath_date.date()}), Drawdown: {drawdown_from_ath:.1f}%")
                    except Exception as e:
                        logger.warning(f"Failed to get ATH data for {ticker}: {e}")

            except Exception as e:
                logger.warning(f"Failed to fetch market cap for {ticker}: {e}")

        if not market_caps:
            logger.warning("No market cap data, defaulting to AAPL")
            return [("AAPL", 1.0)], {}

        # Sort by market cap
        sorted_stocks = sorted(market_caps.items(), key=lambda x: x[1], reverse=True)

        if len(sorted_stocks) < 2:
            market_cap_info = {
                'top_1_ticker': sorted_stocks[0][0],
                'top_1_cap': sorted_stocks[0][1],
                'gap_pct': 100.0,
                'allocation_rule': '100% to #1 (only one stock available)',
                'stock_details': stock_details
            }
            return [(sorted_stocks[0][0], 1.0)], market_cap_info

        top_1_ticker, top_1_cap = sorted_stocks[0]
        top_2_ticker, top_2_cap = sorted_stocks[1]

        # Calculate gap
        gap_pct = ((top_1_cap - top_2_cap) / top_2_cap) * 100

        logger.info(f"\n  Top #1: {top_1_ticker} (${top_1_cap/1e12:.2f}T)")
        logger.info(f"  Top #2: {top_2_ticker} (${top_2_cap/1e12:.2f}T)")
        logger.info(f"  Gap: {gap_pct:.1f}%")

        market_cap_info = {
            'top_1_ticker': top_1_ticker,
            'top_1_cap': top_1_cap,
            'top_2_ticker': top_2_ticker,
            'top_2_cap': top_2_cap,
            'gap_pct': gap_pct,
            'all_rankings': sorted_stocks[:5],  # Top 5 for display
            'stock_details': stock_details  # Include ATH data
        }

        # Apply 10% rule
        if gap_pct > self.MARKET_CAP_THRESHOLD * 100:
            logger.info(f"  → 100% allocation to {top_1_ticker} (gap > 10%)")
            market_cap_info['allocation_rule'] = f'100% to {top_1_ticker} (gap > 10%)'
            market_cap_info['weight_1'] = 1.0
            market_cap_info['weight_2'] = 0.0
            return [(top_1_ticker, 1.0)], market_cap_info
        else:
            total_cap = top_1_cap + top_2_cap
            weight_1 = top_1_cap / total_cap
            weight_2 = top_2_cap / total_cap
            logger.info(f"  → Split allocation: {top_1_ticker} {weight_1*100:.1f}%, {top_2_ticker} {weight_2*100:.1f}%")
            market_cap_info['allocation_rule'] = f'Split between #{top_1_ticker} and #{top_2_ticker} (gap ≤ 10%)'
            market_cap_info['weight_1'] = weight_1
            market_cap_info['weight_2'] = weight_2
            return [(top_1_ticker, weight_1), (top_2_ticker, weight_2)], market_cap_info

    def get_nasdaq_signal(self, days=30) -> Tuple[float, int, pd.Timestamp]:
        """
        Get NASDAQ signal and check for -3% drops

        Returns: (latest_return, minus_3_count_30d, latest_date)
        """
        logger.info("=== NASDAQ Signal Analysis ===")

        try:
            nasdaq_data = yf.download("^IXIC", period=f"{days+5}d", progress=False)

            if nasdaq_data.empty:
                logger.error("NASDAQ data download failed")
                return 0.0, 0, None

            # Calculate returns
            nasdaq_data['Return'] = nasdaq_data['Close'].pct_change() * 100

            # Get latest return
            latest_return = nasdaq_data['Return'].iloc[-1]
            latest_date = nasdaq_data.index[-1]

            # Count -3% drops in last 30 days
            recent_data = nasdaq_data.tail(days)
            minus_3_dates = recent_data[recent_data['Return'] <= self.MINUS_3_THRESHOLD].index.tolist()

            logger.info(f"  Latest NASDAQ return: {latest_return:.2f}% on {latest_date.date()}")
            logger.info(f"  -3% drops in last {days} days: {len(minus_3_dates)}")

            return latest_return, len(minus_3_dates), latest_date

        except Exception as e:
            logger.error(f"NASDAQ signal calculation failed: {e}")
            return 0.0, 0, None

    def get_fed_regime(self) -> FedRateRegime:
        """
        Determine Fed rate regime (simplified)

        In production, this should query actual Fed Funds Rate
        For now, we use NASDAQ SMA crossover as proxy
        """
        try:
            nasdaq_data = yf.download("^IXIC", period="400d", progress=False)

            if nasdaq_data.empty:
                return FedRateRegime.EASING

            nasdaq_data['SMA_50'] = nasdaq_data['Close'].rolling(window=50).mean()
            nasdaq_data['SMA_200'] = nasdaq_data['Close'].rolling(window=200).mean()

            latest_price = nasdaq_data['Close'].iloc[-1]
            sma_50 = nasdaq_data['SMA_50'].iloc[-1]
            sma_200 = nasdaq_data['SMA_200'].iloc[-1]

            if not pd.isna(sma_50) and not pd.isna(sma_200):
                if latest_price > sma_50 > sma_200:
                    return FedRateRegime.EASING
                else:
                    return FedRateRegime.TIGHTENING

            return FedRateRegime.EASING

        except Exception as e:
            logger.warning(f"Fed regime detection failed: {e}")
            return FedRateRegime.EASING

    def get_account_summary(self):
        """Get complete account balance information with capital allocation"""
        summary = {
            'stocks': [],
            'cash_balance_usd': 0.0,
            'total_value': 0.0,
            'total_profit': 0.0,
            'allocated_capital': 0.0,
            'available_capital': 0.0
        }

        if self.paper_trading:
            logger.info("📝 Paper trading mode - simulated account")
            return summary

        try:
            # Query USD balance
            output1_usd, _ = inquire_balance.inquire_balance(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd="NASD",
                tr_crcy_cd="USD",
                env_dv="real"
            )

            total_stock_value = 0.0
            all_holdings = []

            # Process ALL holdings first (to get total account value)
            if output1_usd is not None and not output1_usd.empty:
                for _, row in output1_usd.iterrows():
                    ticker = row['ovrs_pdno']
                    quantity = int(row['ovrs_cblc_qty'])

                    if quantity <= 0:
                        continue

                    purchase_amount = float(row.get('frcr_pchs_amt1', 0))
                    avg_price = purchase_amount / quantity if quantity > 0 else 0

                    # Use KIS API's real-time valuation (not yfinance T-1 close)
                    market_value = float(row.get('ovrs_stck_evlu_amt', 0))
                    profit_api = float(row.get('frcr_evlu_pfls_amt', 0))

                    if market_value > 0 and quantity > 0:
                        current_price = market_value / quantity
                        stock_value = market_value
                        profit = profit_api
                    else:
                        # Fallback to yfinance if KIS valuation missing
                        current_price_data = yf.download(ticker, period='5d')
                        if not current_price_data.empty:
                            current_price = current_price_data['Close'].iloc[-1].item()
                        else:
                            current_price = avg_price
                        stock_value = current_price * quantity
                        profit = (current_price - avg_price) * quantity

                    profit_rate = (profit / purchase_amount * 100) if purchase_amount > 0 else 0
                    exchange_rate = getattr(self, 'usd_krw_exchange_rate', None) or 1300
                    profit_krw = profit * exchange_rate

                    all_holdings.append({
                        'ticker': ticker,
                        'quantity': quantity,
                        'value': stock_value,
                        'avg_price': avg_price,
                        'current_price': current_price,
                        'profit': profit,
                        'profit_rate': profit_rate,
                        'profit_krw': profit_krw,
                        'currency': 'USD'
                    })

            # Calculate TOTAL stock value from ALL holdings (before filtering)
            total_all_stock_value = sum(stock['value'] for stock in all_holdings)

            # Filter holdings - only include positions owned by THIS strategy
            if self.dual_strategy_enabled:
                owned_holdings, other_positions_value = self.position_tracker.filter_account_holdings(
                    self.strategy_name, all_holdings
                )
                summary['stocks'] = owned_holdings
                summary['other_strategy_value'] = other_positions_value

                # Calculate stock value only from OUR positions (for display)
                for stock in owned_holdings:
                    total_stock_value += stock['value']
                    summary['total_profit'] += stock['profit']

                logger.info(f"Position Filter: {len(owned_holdings)}/{len(all_holdings)} positions belong to {self.strategy_name}")
                if other_positions_value > 0:
                    logger.info(f"Other strategy positions value: ${other_positions_value:,.2f}")
            else:
                # No dual strategy - include all positions
                summary['stocks'] = all_holdings
                for stock in all_holdings:
                    total_stock_value += stock['value']
                    summary['total_profit'] += stock['profit']
                total_all_stock_value = total_stock_value

            # Query USD cash balance
            df_cash_usd = inquire_psamount.inquire_psamount(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd="NAS",
                item_cd="AAPL",
                ovrs_ord_unpr="0",
                env_dv="real"
            )

            if df_cash_usd is not None and not df_cash_usd.empty:
                summary['cash_balance_usd'] = float(df_cash_usd.iloc[0]['ord_psbl_frcr_amt'])

            # Query KRW cash balance (for Korean domestic account)
            summary['cash_balance_krw'] = 0.0
            try:
                import inquire_account_balance as iab
                _, df_cash_krw = iab.inquire_account_balance(
                    cano=self.my_acct,
                    acnt_prdt_cd=self.my_prod,
                )
                if df_cash_krw is not None and not df_cash_krw.empty:
                    cash_krw = float(df_cash_krw.iloc[0].get('dncl_amt', 0))
                    summary['cash_balance_krw'] = cash_krw
                    logger.info(f"KRW 현금 잔고: ₩{cash_krw:,.0f}")
                else:
                    logger.warning("KRW 잔고 정보가 없습니다.")
            except Exception as e:
                logger.warning(f"KRW 현금 잔고 조회 실패: {e}")

            # Get exchange rate for KRW to USD conversion
            if not hasattr(self, 'usd_krw_exchange_rate') or self.usd_krw_exchange_rate is None:
                try:
                    exchange_rate_data = yf.download('USDKRW=X', period='1d', progress=False)
                    if not exchange_rate_data.empty:
                        self.usd_krw_exchange_rate = exchange_rate_data['Close'].iloc[-1].item()
                        logger.info(f"USD/KRW 환율: {self.usd_krw_exchange_rate:,.2f}")
                    else:
                        self.usd_krw_exchange_rate = 1300.0  # Fallback
                except Exception as e:
                    logger.warning(f"환율 조회 실패: {e}, 기본값 사용")
                    self.usd_krw_exchange_rate = 1300.0  # Fallback

            # Calculate total cash in USD (USD + KRW converted)
            summary['cash_balance_total_usd'] = summary['cash_balance_usd'] + (summary['cash_balance_krw'] / self.usd_krw_exchange_rate)

            # Calculate total values
            summary['total_value'] = total_stock_value + summary['cash_balance_total_usd']

            # Apply capital allocation if dual strategy is enabled
            if self.dual_strategy_enabled:
                total_account_value = total_all_stock_value + summary['cash_balance_total_usd']

                if self.allocation_mode == 'fixed':
                    # Fixed dollar allocation (money input base)
                    summary['allocated_capital'] = self.allocated_capital_fixed
                    summary['available_capital'] = self.allocated_capital_fixed
                    allocation_pct = (self.allocated_capital_fixed / total_account_value * 100) if total_account_value > 0 else 0
                    logger.info(f"Dual Strategy Mode (FIXED): ${self.allocated_capital_fixed:,.2f} allocated to JD Strategy ({allocation_pct:.1f}%)")
                else:
                    # Percentage-based allocation
                    summary['allocated_capital'] = total_account_value * self.capital_allocation
                    summary['available_capital'] = summary['allocated_capital']
                    logger.info(f"Dual Strategy Mode (PERCENTAGE): {self.capital_allocation*100:.0f}% allocation to JD Strategy")

                logger.info(f"Total Account Value: ${total_account_value:,.2f}")
                logger.info(f"Allocated Capital (JD): ${summary['allocated_capital']:,.2f}")
            else:
                summary['allocated_capital'] = summary['total_value']
                summary['available_capital'] = summary['total_value']

            logger.info(f"Account Summary: Total=${summary['total_value']:,.2f}, USD Cash=${summary['cash_balance_usd']:,.2f}, KRW Cash=₩{summary['cash_balance_krw']:,.0f}, Total Cash=${summary['cash_balance_total_usd']:,.2f}, P/L=${summary['total_profit']:,.2f}")

            return summary

        except Exception as e:
            logger.error(f"Account summary query error: {e}")
            return None

    def calculate_rebalancing_action(self, account_summary, world_1_ticker, world_1_price) -> Dict:
        """
        Calculate rebalancing action based on WORLD #1 STOCK PRICE drawdown (CORRECTED)
        Respects capital allocation if dual strategy is enabled

        Returns: dict with target cash ratio and actions needed
        """
        # Use allocated capital for JD strategy calculations
        total_value = account_summary.get('allocated_capital', account_summary['total_value'])

        if total_value <= 0:
            return {'action': 'none', 'reason': 'No portfolio value'}

        # Update portfolio ATH (based on allocated capital)
        if total_value > self.state['portfolio_ath']:
            self.state['portfolio_ath'] = total_value
            logger.info(f"📈 New Portfolio ATH: ${total_value:,.2f} (JD Strategy)")

        # CRITICAL: Track WORLD #1 STOCK ATH (not portfolio ATH!)
        # Note: ATH is synced from market cap analysis in NORMAL state before this is called
        # Here we only update if current price exceeds the ATH (intraday new high)
        if world_1_price > self.state.get('world_1_stock_ath', 0):
            self.state['world_1_stock_ath'] = world_1_price
            self.state['current_world_1_ticker'] = world_1_ticker
            logger.info(f"📈 New World #1 Stock ATH: {world_1_ticker} @ ${world_1_price:.2f}")

        # Calculate drawdown from WORLD #1 STOCK ATH (not portfolio!)
        stock_ath = self.state.get('world_1_stock_ath', world_1_price)
        if stock_ath <= 0:
            stock_ath = world_1_price

        stock_drawdown_pct = ((world_1_price - stock_ath) / stock_ath) * 100

        # Determine target cash ratio using strict inequality (CORRECTED)
        target_cash_ratio = 0.0
        rebalancing_tier = "No rebalancing"
        if stock_drawdown_pct > -2.5:
            target_cash_ratio = 0.0    # 0% to -2.5%: 0% cash
            rebalancing_tier = "Tier 0: 0% cash (0% to -2.5% drawdown)"
        elif stock_drawdown_pct > -5.0:
            target_cash_ratio = 0.10   # -2.5% to -5.0%: 10% cash
            rebalancing_tier = "Tier 1: 10% cash (-2.5% to -5.0% drawdown)"
        elif stock_drawdown_pct > -7.5:
            target_cash_ratio = 0.20   # -5.0% to -7.5%: 20% cash
            rebalancing_tier = "Tier 2: 20% cash (-5.0% to -7.5% drawdown)"
        else:
            target_cash_ratio = 0.30   # Below -7.5%: 30% cash
            rebalancing_tier = "Tier 3: 30% cash (below -7.5% drawdown)"

        target_stock_ratio = 1.0 - target_cash_ratio

        # CRITICAL: Calculate current allocation correctly for dual strategy mode
        # In dual strategy, account_summary['cash_balance_usd'] is TOTAL account cash (both strategies)
        # We need to calculate THIS strategy's cash portion separately
        current_stock_amount = sum(s['value'] for s in account_summary.get('stocks', []))
        current_cash_amount = total_value - current_stock_amount  # Strategy's cash = allocated capital - stock value

        current_stock_ratio = current_stock_amount / total_value if total_value > 0 else 0
        current_cash_ratio = current_cash_amount / total_value if total_value > 0 else 0

        logger.info(f"Rebalancing Check: {world_1_ticker} Stock Drawdown={stock_drawdown_pct:.2f}%, Target Cash={target_cash_ratio*100:.0f}%, Current Cash={current_cash_ratio*100:.0f}%")
        logger.info(f"  Current: ${current_stock_amount:,.2f} stocks ({current_stock_ratio*100:.0f}%), ${current_cash_amount:,.2f} cash ({current_cash_ratio*100:.0f}%)")

        # Calculate target amounts
        target_cash_amount = total_value * target_cash_ratio
        target_stock_amount = total_value * target_stock_ratio

        # Build comprehensive result
        result = {
            'world_1_ticker': world_1_ticker,
            'world_1_price': world_1_price,
            'stock_ath': stock_ath,
            'stock_drawdown_pct': stock_drawdown_pct,
            'rebalancing_tier': rebalancing_tier,
            'target_cash_ratio': target_cash_ratio,
            'target_stock_ratio': target_stock_ratio,
            'current_cash_ratio': current_cash_ratio,
            'current_stock_ratio': current_stock_ratio,
            'target_cash_amount': target_cash_amount,
            'target_stock_amount': target_stock_amount,
            'current_cash_amount': current_cash_amount,
            'current_stock_amount': current_stock_amount,
            'total_value': total_value
        }

        # Rebalance if there's a significant difference (can be sell OR buy)
        if abs(target_cash_ratio - current_cash_ratio) > 0.01:
            if target_cash_ratio > current_cash_ratio:
                # Need more cash: SELL stocks
                result['action'] = 'sell'
            else:
                # Need less cash: BUY stocks (stock rebounded)
                result['action'] = 'buy'
        else:
            result['action'] = 'none'
            result['reason'] = 'No rebalancing needed'

        return result

    def _send_rebalancing_status(self, rebalancing_info: Dict):
        """
        Send detailed rebalancing status to Discord

        Shows current position vs target based on #1 stock's ATH gap
        """
        world_1_ticker = rebalancing_info.get('world_1_ticker', 'N/A')
        world_1_price = rebalancing_info.get('world_1_price', 0)
        stock_ath = rebalancing_info.get('stock_ath', 0)
        stock_drawdown_pct = rebalancing_info.get('stock_drawdown_pct', 0)
        rebalancing_tier = rebalancing_info.get('rebalancing_tier', 'Unknown')

        target_stock_ratio = rebalancing_info.get('target_stock_ratio', 0)
        target_cash_ratio = rebalancing_info.get('target_cash_ratio', 0)
        current_stock_ratio = rebalancing_info.get('current_stock_ratio', 0)
        current_cash_ratio = rebalancing_info.get('current_cash_ratio', 0)

        target_stock_amount = rebalancing_info.get('target_stock_amount', 0)
        target_cash_amount = rebalancing_info.get('target_cash_amount', 0)
        current_stock_amount = rebalancing_info.get('current_stock_amount', 0)
        current_cash_amount = rebalancing_info.get('current_cash_amount', 0)

        total_value = rebalancing_info.get('total_value', 0)

        # Determine status emoji
        if stock_drawdown_pct >= 0:
            status_emoji = "✨"
            status_text = "AT/NEAR ATH"
        elif stock_drawdown_pct > -2.5:
            status_emoji = "🟢"
            status_text = "Healthy"
        elif stock_drawdown_pct > -5.0:
            status_emoji = "🟡"
            status_text = "Minor Correction"
        elif stock_drawdown_pct > -7.5:
            status_emoji = "🟠"
            status_text = "Moderate Correction"
        else:
            status_emoji = "🔴"
            status_text = "Significant Correction"

        fields = [
            {
                "name": f"🎯 **World #1 Stock: {world_1_ticker}**",
                "value": f"Current: ${world_1_price:.2f}\nATH: ${stock_ath:.2f}\nGap: {stock_drawdown_pct:+.2f}%",
                "inline": True
            },
            {
                "name": f"{status_emoji} **Status**",
                "value": status_text,
                "inline": True
            },
            {
                "name": "⚖️ **Rebalancing Protocol**",
                "value": rebalancing_tier,
                "inline": False
            },
            {
                "name": "📊 **Target Allocation**",
                "value": f"Stocks: {target_stock_ratio*100:.0f}% (${target_stock_amount:,.2f})\nCash: {target_cash_ratio*100:.0f}% (${target_cash_amount:,.2f})",
                "inline": True
            },
            {
                "name": "💼 **Current Allocation**",
                "value": f"Stocks: {current_stock_ratio*100:.0f}% (${current_stock_amount:,.2f})\nCash: {current_cash_ratio*100:.0f}% (${current_cash_amount:,.2f})",
                "inline": True
            }
        ]

        # Add action required if any
        action = rebalancing_info.get('action', 'none')
        if action == 'sell':
            diff_cash = target_cash_amount - current_cash_amount
            fields.append({
                "name": "⚠️ **Action Required**",
                "value": f"SELL ${diff_cash:,.2f} worth of stocks\nto reach {target_cash_ratio*100:.0f}% cash target",
                "inline": False
            })
        elif action == 'buy':
            diff_stock = target_stock_amount - current_stock_amount
            fields.append({
                "name": "📈 **Action Required**",
                "value": f"BUY ${diff_stock:,.2f} worth of stocks\nto reach {target_stock_ratio*100:.0f}% stock target",
                "inline": False
            })
        else:
            fields.append({
                "name": "✅ **Status**",
                "value": "Portfolio is balanced - No action needed",
                "inline": False
            })

        # Send to Discord
        color = 3066993 if action == 'none' else 16776960  # success : warning
        self.notifier._send(
            title="Rebalancing Status",
            fields=fields,
            color=color
        )

    def _validate_trade(self, side, ticker, quantity, price, account_summary):
        """
        Validate trade before execution

        Returns: (is_valid, error_message)
        """
        # Check daily trade limit
        today = datetime.now().date()
        if self.last_trade_date != today:
            self.daily_trade_count = 0
            self.last_trade_date = today

        if self.daily_trade_count >= self.MAX_DAILY_TRADES:
            return False, f"Daily trade limit reached ({self.MAX_DAILY_TRADES})"

        # Validate quantity
        if quantity <= 0:
            return False, "Invalid quantity (must be > 0)"

        # Validate price
        if price <= 0:
            return False, "Invalid price (must be > 0)"

        trade_value = quantity * price

        # Check minimum trade size
        if trade_value < self.MIN_TRADE_SIZE_USD:
            return False, f"Trade size too small (${trade_value:.2f} < ${self.MIN_TRADE_SIZE_USD})"

        # Check maximum single trade percentage
        if account_summary and account_summary.get('total_value', 0) > 0:
            trade_pct = trade_value / account_summary['total_value']

            if side == 'buy' and trade_pct > self.MAX_SINGLE_TRADE_PCT:
                return False, f"Trade too large ({trade_pct*100:.1f}% > {self.MAX_SINGLE_TRADE_PCT*100:.0f}% of portfolio)"

        # For buy orders, check available cash (USD + KRW converted)
        if side == 'buy' and account_summary:
            total_cash = account_summary.get('cash_balance_total_usd', 0)
            if trade_value > total_cash:
                return False, f"Insufficient cash (need ${trade_value:.2f}, have ${total_cash:.2f} [USD: ${account_summary.get('cash_balance_usd', 0):.2f} + KRW: ₩{account_summary.get('cash_balance_krw', 0):,.0f}])"

        # For sell orders, check holdings
        if side == 'sell' and account_summary:
            holding = next((s for s in account_summary.get('stocks', []) if s['ticker'] == ticker), None)
            if not holding or holding['quantity'] < quantity:
                return False, f"Insufficient holdings (trying to sell {quantity}, have {holding['quantity'] if holding else 0})"

        # Sanity check: portfolio value
        if account_summary and account_summary.get('total_value', 0) > self.MAX_PORTFOLIO_VALUE:
            return False, f"Portfolio value exceeds safety limit (${account_summary['total_value']:,.2f} > ${self.MAX_PORTFOLIO_VALUE:,.2f})"

        return True, "Validation passed"

    def place_order(self, side, ticker, quantity, price, account_summary=None):
        """Execute order via KIS API with safety validation and position tracking"""

        # Check if we can trade this ticker (position ownership)
        if self.dual_strategy_enabled:
            can_trade, reason = self.position_tracker.can_trade(self.strategy_name, ticker)
            if not can_trade:
                logger.warning(f"⚠️ Cannot trade {ticker}: {reason}")
                self.notifier.send_alert(
                    f"Trade Blocked - Position Ownership",
                    f"**{ticker}** ({side.upper()}): {reason}",
                    'warning'
                )
                return None

        # Validate trade
        is_valid, validation_msg = self._validate_trade(side, ticker, quantity, price, account_summary)

        if not is_valid:
            logger.warning(f"⚠️ Trade validation failed: {validation_msg}")
            self.notifier.send_alert(
                f"Trade Blocked",
                f"**{ticker}** ({side.upper()}): {validation_msg}",
                'warning'
            )
            return None

        logger.info(f"{'📝 [PAPER]' if self.paper_trading else '💰 [LIVE]'} {ticker} {quantity} shares {side.upper()} @ ${price:.2f}")

        if self.paper_trading:
            logger.info(f"  Paper trading - order not executed")
            self.daily_trade_count += 1
            return {'status': 'paper_trade', 'ticker': ticker, 'quantity': quantity, 'price': price}

        try:
            order_price = str(round(price * (1.03 if side == 'buy' else 0.95), 2))
            exchange = self.asset_exchange_map.get(ticker, "NASD")

            df_order = order.order(
                cano=self.my_acct,
                acnt_prdt_cd=self.my_prod,
                ovrs_excg_cd=exchange,
                pdno=ticker,
                ord_qty=str(int(quantity)),
                ovrs_ord_unpr=order_price,
                ord_dv=side,
                ctac_tlno="",
                mgco_aptm_odno="",
                ord_svr_dvsn_cd="0",
                ord_dvsn="00",  # Limit order
                env_dv="real"
            )

            logger.info(f"✅ {side.upper()} order result:\n{df_order}")
            self.daily_trade_count += 1

            # Register/update position ownership
            if self.dual_strategy_enabled:
                if side == 'buy':
                    # Register or update position
                    self.position_tracker.register_position(
                        self.strategy_name, ticker, quantity, price
                    )
                elif side == 'sell':
                    # Check if position fully closed
                    current_qty = next(
                        (s['quantity'] for s in account_summary.get('stocks', [])
                         if s['ticker'] == ticker), 0
                    )
                    remaining_qty = current_qty - quantity

                    if remaining_qty <= 0:
                        # Position fully closed - unregister
                        self.position_tracker.unregister_position(self.strategy_name, ticker)
                    else:
                        # Partial sell - update quantity
                        self.position_tracker.update_position_quantity(
                            self.strategy_name, ticker, remaining_qty
                        )

            # Log trade execution (notification will be batched in daily summary)
            logger.info(f"{'📝 [PAPER]' if self.paper_trading else '✅ [LIVE]'} Trade executed: {ticker} {quantity} {side.upper()} @ ${price:.2f}")

            return df_order

        except Exception as e:
            logger.error(f"❌ {side.upper()} order failed: {e}")
            self.notifier.send_error(
                f"Trade failed: {ticker} {side.upper()}",
                str(e)
            )
            return None

    def _check_exit_condition(self, minus_3_count: int) -> Tuple[bool, str]:
        """
        Check if we can exit Alert or Panic state

        Rules (CORRECTED per JD Strategy Guide):
        1. Early Exit: 8 consecutive NASDAQ up days (immediate)
        2. Crisis Exit: Rolling 30-day window has 0 -3% events (applies to both ALERT and PANIC)

        Returns: (can_exit, reason)
        """
        consecutive_up_days = self.state.get('consecutive_up_days', 0)

        # Early exit condition: 8 consecutive up days
        if consecutive_up_days >= self.EARLY_EXIT_UP_DAYS:
            return True, f"Early exit: {consecutive_up_days} consecutive up days"

        # Crisis exit condition: 0 -3% events in rolling 30-day window
        # This is the main exit mechanism per the guide
        if minus_3_count == 0:
            return True, f"Crisis exit: 30-day window clear of -3% events"

        return False, f"No exit: {minus_3_count} -3% events in 30-day window"

    def _execute_stake_planting(self, account_summary, nasdaq_drawdown_pct, fed_regime):
        """
        Execute Stake Planting Protocol (ALERT State)

        CRITICAL: Uses NASDAQ ATH from when alert was FIRST triggered
        Rebalances to TARGET ALLOCATION, not just buying fixed percentages

        Args:
            account_summary: Current account state
            nasdaq_drawdown_pct: NASDAQ drawdown from ATH when alert triggered
            fed_regime: Current Fed regime (EASING or TIGHTENING)
        """
        # Select stake protocol based on Fed regime
        if fed_regime == FedRateRegime.EASING:
            stake_intervals = self.STAKE_25_INTERVALS
            stake_targets = self.STAKE_25_TARGETS
            regime_name = "-25% Protocol (Easing)"
        else:
            stake_intervals = self.STAKE_50_INTERVALS
            stake_targets = self.STAKE_50_TARGETS
            regime_name = "-50% Protocol (Tightening)"

        # Get allocated capital
        total_value = account_summary.get('allocated_capital', account_summary['total_value'])
        if total_value <= 0:
            return

        # Calculate current stock allocation
        stock_value = sum(s['value'] for s in account_summary.get('stocks', []))
        current_stock_ratio = stock_value / total_value

        # Check if we've hit any new stake intervals
        for i, interval in enumerate(stake_intervals):
            target_stock_ratio = stake_targets[i]

            if nasdaq_drawdown_pct <= interval and interval not in self.state.get('stake_levels_executed', []):
                # Only rebalance if we need to increase stock allocation
                if target_stock_ratio > current_stock_ratio + 0.01:  # Small epsilon
                    # Calculate how much to buy
                    target_stock_value = total_value * target_stock_ratio
                    buy_amount = target_stock_value - stock_value

                    total_cash = account_summary.get('cash_balance_total_usd', 0)
                    if buy_amount > 0 and total_cash >= buy_amount:
                        logger.info(f"🎯 STAKE-IN at {interval}% - {regime_name}, Target allocation: {target_stock_ratio*100:.0f}% stock")

                        # Get target holdings
                        target_holdings, _ = self.get_top_stocks_by_market_cap()

                        # Send Discord notification
                        self.notifier._send(
                            title=f"Stake-In Triggered — Level {i+1}",
                            fields=[
                                {"name": "NASDAQ Drawdown", "value": f"{nasdaq_drawdown_pct:.2f}%", "inline": True},
                                {"name": "Trigger Level", "value": f"{interval}%", "inline": True},
                                {"name": "Target Stock Allocation", "value": f"{target_stock_ratio*100:.0f}%", "inline": True},
                                {"name": "Buy Amount", "value": f"${buy_amount:,.2f}", "inline": True},
                                {"name": "Market Regime", "value": regime_name, "inline": True}
                            ],
                            color=3066993  # success
                        )

                        # Buy according to target allocations
                        for ticker, weight in target_holdings:
                            try:
                                ticker_data = yf.download(ticker, period='5d', progress=False)
                                if not ticker_data.empty:
                                    current_price = ticker_data['Close'].iloc[-1].item()
                                    allocation = buy_amount * weight
                                    quantity = int(allocation / current_price)

                                    if quantity > 0:
                                        result = self.place_order('buy', ticker, quantity, current_price, account_summary)
                                        if result is not None:
                                            logger.info(f"✅ Stake-in: Bought {quantity} {ticker} @ ${current_price:.2f}")
                            except Exception as e:
                                logger.error(f"Error in stake planting for {ticker}: {e}")

                        # Mark this level as executed
                        if 'stake_levels_executed' not in self.state:
                            self.state['stake_levels_executed'] = []
                        self.state['stake_levels_executed'].append(interval)
                        self._save_state()

                    else:
                        total_cash = account_summary.get('cash_balance_total_usd', 0)
                        logger.warning(f"Insufficient cash for stake-in at {interval}%: need ${buy_amount:.2f}, have ${total_cash:.2f} [USD: ${account_summary.get('cash_balance_usd', 0):.2f} + KRW: ₩{account_summary.get('cash_balance_krw', 0):,.0f}]")
                        # Do NOT mark as executed — retry next session when cash is available
                        logger.info(f"Stake-in at {interval}% will be retried next session")

                elif target_stock_ratio <= current_stock_ratio:
                    # Already at or above target, mark as executed
                    if 'stake_levels_executed' not in self.state:
                        self.state['stake_levels_executed'] = []
                    if interval not in self.state['stake_levels_executed']:
                        self.state['stake_levels_executed'].append(interval)

    def _execute_reentry(self, account_summary, reason: str):
        """
        Re-enter market: Buy stocks with available cash (USD + KRW converted)

        Matches backtest behavior: Invest cash in top market cap stocks
        """
        cash_available = account_summary.get('cash_balance_total_usd', 0)

        if cash_available < 100:  # Minimum $100 to invest
            logger.info(f"Insufficient cash for re-entry: ${cash_available:.2f} [USD: ${account_summary.get('cash_balance_usd', 0):.2f} + KRW: ₩{account_summary.get('cash_balance_krw', 0):,.0f}]")
            return

        logger.info(f"🎯 RE-ENTRY - Investing ${cash_available:,.2f} in top stocks [USD: ${account_summary.get('cash_balance_usd', 0):.2f} + KRW: ₩{account_summary.get('cash_balance_krw', 0):,.0f}]")

        # Get top stocks by market cap
        target_holdings, _ = self.get_top_stocks_by_market_cap()

        self.notifier._send(
            title="Market Re-Entry",
            fields=[
                {"name": "Reason", "value": reason, "inline": False},
                {"name": "Cash to Invest", "value": f"${cash_available:,.2f}", "inline": True},
                {"name": "Target Holdings", "value": ", ".join([t for t, _ in target_holdings]), "inline": True}
            ],
            color=3066993  # success
        )

        # Invest in target holdings
        for ticker, weight in target_holdings:
            try:
                ticker_data = yf.download(ticker, period='5d', progress=False)
                if not ticker_data.empty:
                    current_price = ticker_data['Close'].iloc[-1].item()
                    allocation = cash_available * weight
                    quantity = int(allocation / current_price)

                    if quantity > 0:
                        result = self.place_order('buy', ticker, quantity, current_price, account_summary)
                        if result is not None:
                            logger.info(f"✅ Re-entry: Bought {quantity} {ticker} @ ${current_price:.2f}")
            except Exception as e:
                logger.error(f"Error in re-entry for {ticker}: {e}")

    def _save_strategy_result(self, account_summary, market_state, orders_executed):
        """Save strategy results to JSON file for portfolio summary"""
        try:
            # Calculate allocation percentage for display
            allocated_capital = account_summary.get('allocated_capital', 0)
            total_account_value = account_summary.get('total_value', 0)  # This might include other strategies' value
            allocation_pct = (allocated_capital / total_account_value) if total_account_value > 0 else 0

            result = {
                'strategy_name': self.strategy_name,
                'timestamp': datetime.now().isoformat(),
                'allocation_mode': self.allocation_mode,
                'capital_allocation': allocation_pct if self.allocation_mode == 'fixed' else self.capital_allocation,
                'allocated_capital': allocated_capital,
                'total_value': account_summary.get('total_value', 0),
                'cash_balance': account_summary.get('cash_balance_total_usd', 0),  # Total cash (USD + KRW)
                'total_profit': account_summary.get('total_profit', 0),
                'holdings': [
                    {
                        'ticker': stock['ticker'],
                        'quantity': stock['quantity'],
                        'value': stock['value'],
                        'avg_price': stock.get('avg_price', 0),
                        'current_price': stock.get('current_price', 0),
                        'profit': stock.get('profit', 0),
                        'profit_rate': stock.get('profit_rate', 0),
                        'profit_krw': stock.get('profit_krw', 0)
                    }
                    for stock in account_summary.get('stocks', [])
                ],
                'session_summary': {
                    'orders_executed': orders_executed,
                    'market_state': market_state,
                    'regime': self.get_fed_regime().value,  # Add regime for portfolio summary display
                    'minus_3_count': len(self.state.get('minus_3_dates', [])),
                    'dual_strategy_enabled': self.dual_strategy_enabled
                }
            }

            tmp = self.result_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(result, f, indent=2)
            os.replace(tmp, self.result_file)

            logger.info(f"Strategy result saved to {self.result_file}")

        except Exception as e:
            logger.error(f"Failed to save strategy result: {e}")

    def run(self):
        """Execute JD Strategy daily trading logic with comprehensive error handling"""
        try:
            self._run_strategy()
        except KeyboardInterrupt:
            logger.info("⚠️ Bot stopped by user (Ctrl+C)")
            self.notifier.send_alert(
                "Bot Stopped",
                "Manual interruption (Ctrl+C)",
                'warning'
            )
        except Exception as e:
            logger.error(f"❌ Critical error in bot execution: {e}", exc_info=True)
            self.notifier.send_error(
                "Critical bot execution error",
                f"{str(e)[:1000]}\n\nBot execution halted. Please check logs."
            )
            raise

    def _run_strategy(self):
        """Internal method containing the actual strategy logic"""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Track orders and trades in this session
        orders_executed_count = 0
        executed_trades = []  # For batched trade notifications

        # Get exchange rate
        self._get_exchange_rate()

        # Get account summary
        account_summary = self.get_account_summary()
        if not account_summary:
            self.notifier.send_error("Account summary failed", "Failed to get account summary")
            return

        # Startup notification - skip in dual strategy mode (portfolio summary handles it)
        if not self.dual_strategy_enabled:
            self.notifier.send_startup(
                strategy_name="JD Investment Strategy",
                config={
                    'mode': 'Paper Trading' if self.paper_trading else 'Live Trading',
                    'portfolio_value': account_summary['total_value'],
                    'cash_usd': account_summary.get('cash_balance_usd', 0),
                    'cash_krw': account_summary.get('cash_balance_krw', 0),
                    'allocation': 'Full Portfolio',
                    'state': self.state['current_state'].upper(),
                    'holdings': account_summary['stocks'] if account_summary['stocks'] else []
                }
            )

        # Get NASDAQ signal
        nasdaq_return, minus_3_count, signal_date = self.get_nasdaq_signal()

        if signal_date is None:
            self.notifier.send_error("NASDAQ signal failed", "Failed to get NASDAQ signal")
            return

        # Check for -3% trigger
        is_minus_3_today = nasdaq_return <= self.MINUS_3_THRESHOLD

        # State machine logic
        current_state = MarketState(self.state['current_state'])

        # Update minus_3_dates (deduplicated — double-run on same day must not double-count)
        if is_minus_3_today:
            today_str = str(signal_date.date())
            if today_str not in self.state['minus_3_dates']:
                self.state['minus_3_dates'].append(today_str)
                self.state['last_minus_3_date'] = today_str

            # Remove old dates (> 30 days)
            cutoff_date = signal_date - timedelta(days=self.ROLLING_WINDOW_DAYS)
            self.state['minus_3_dates'] = [d for d in self.state['minus_3_dates'] if pd.to_datetime(d) >= cutoff_date]

            minus_3_count = len(self.state['minus_3_dates'])

        # Update consecutive_up_days for the early-exit condition (was never updated before — dead code fix)
        if nasdaq_return > 0:
            self.state['consecutive_up_days'] = self.state.get('consecutive_up_days', 0) + 1
        else:
            self.state['consecutive_up_days'] = 0

        # State transitions
        if is_minus_3_today and current_state == MarketState.NORMAL:
            logger.info("⚠️ TRANSITION: NORMAL → ALERT")

            # Get NASDAQ ATH and current price
            nasdaq_data = yf.download("^IXIC", period="1y", progress=False)
            nasdaq_ath = nasdaq_data['Close'].max()
            current_nasdaq = nasdaq_data['Close'].iloc[-1]

            # Calculate current NASDAQ drawdown
            nasdaq_drawdown_pct = ((current_nasdaq - nasdaq_ath) / nasdaq_ath) * 100

            # Determine Fed regime for stake planting protocol
            fed_regime = self.get_fed_regime()

            # Select stake protocol based on Fed regime
            if fed_regime == FedRateRegime.EASING:
                stake_intervals = self.STAKE_25_INTERVALS
                stake_targets = self.STAKE_25_TARGETS
                regime_name = "25% Plan (Easing)"
            else:
                stake_intervals = self.STAKE_50_INTERVALS
                stake_targets = self.STAKE_50_TARGETS
                regime_name = "50% Plan (Tightening)"

            # OPTIMIZATION: Calculate initial target allocation based on current drawdown
            # This avoids selling 100% and immediately buying back
            target_stock_ratio = 0.0
            triggered_level = None
            for i, interval in enumerate(stake_intervals):
                if nasdaq_drawdown_pct <= interval:
                    target_stock_ratio = stake_targets[i]
                    triggered_level = interval
                    break

            target_cash_ratio = 1.0 - target_stock_ratio

            logger.info(f"🎯 ALERT ENTRY OPTIMIZATION: NASDAQ at {nasdaq_drawdown_pct:.2f}% from ATH")
            logger.info(f"   Target allocation: {target_stock_ratio*100:.0f}% stocks / {target_cash_ratio*100:.0f}% cash ({regime_name})")

            # Calculate current allocation
            total_value = account_summary.get('allocated_capital', account_summary['total_value'])
            current_stock_value = sum(s['value'] for s in account_summary['stocks'])
            current_stock_ratio = current_stock_value / total_value if total_value > 0 else 0

            # Send Discord notification with optimized action
            action_text = f"Target: {target_stock_ratio*100:.0f}% stocks / {target_cash_ratio*100:.0f}% cash"
            if triggered_level:
                action_text += f"\n(Triggered at {triggered_level}% drawdown level)"

            self.notifier.send_state_change(
                old_state='NORMAL',
                new_state='ALERT',
                reason=f'NASDAQ -3% drop detected ({nasdaq_return:.2f}%)',
                details={
                    'NASDAQ Current': f'${current_nasdaq:,.2f}',
                    'NASDAQ ATH': f'${nasdaq_ath:,.2f}',
                    'Drawdown': f'{nasdaq_drawdown_pct:.2f}%',
                    'Fed Regime': regime_name,
                    'Action': action_text
                }
            )

            # OPTIMIZED: Adjust to target allocation (not 100% cash)
            if target_stock_ratio < current_stock_ratio:
                # Need to sell some stocks to reach target
                target_stock_value = total_value * target_stock_ratio
                sell_value = current_stock_value - target_stock_value

                if sell_value > 0:
                    logger.info(f"🚨 ALERT ENTRY - Selling ${sell_value:,.2f} to reach {target_cash_ratio*100:.0f}% cash target...")

                    # Sell proportionally from all holdings
                    sell_ratio = sell_value / current_stock_value
                    for stock in account_summary['stocks']:
                        if stock['quantity'] > 0:
                            sell_quantity = int(stock['quantity'] * sell_ratio)
                            if sell_quantity > 0:
                                result = self.place_order('sell', stock['ticker'], sell_quantity, stock['current_price'], account_summary)
                                if result is not None:
                                    orders_executed_count += 1
                                    logger.info(f"✅ ALERT entry: Sold {sell_quantity} {stock['ticker']}")

                    logger.info(f"✅ ALERT ENTRY - Reached {target_cash_ratio*100:.0f}% cash target, Stake Planting Protocol activated")
                else:
                    logger.info(f"✅ ALERT ENTRY - Already at target allocation, no selling needed")
            else:
                logger.info(f"✅ ALERT ENTRY - Current stock allocation ({current_stock_ratio*100:.0f}%) below target, no immediate selling needed")

            # Now set ALERT state and parameters
            self.state['current_state'] = MarketState.ALERT.value
            self.state['nasdaq_ath_at_alert'] = float(nasdaq_ath)
            self.state['stake_levels_executed'] = []
            if triggered_level:
                # Mark this level as executed since we already positioned for it
                self.state['stake_levels_executed'].append(triggered_level)
            self._save_state()

        elif is_minus_3_today and current_state == MarketState.ALERT and minus_3_count >= self.PANIC_THRESHOLD:
            logger.info("🔴 TRANSITION: ALERT → PANIC")
            self.state['current_state'] = MarketState.PANIC.value
            self._save_state()

            self.notifier.send_state_change(
                old_state='ALERT',
                new_state='PANIC',
                reason=f'{minus_3_count} -3% events in 30 days',
                details={
                    'Action': 'PANIC SELL ALL STOCKS',
                    '-3% Count': str(minus_3_count)
                }
            )

            # Refresh account before panic sell to avoid double-selling shares already sold by Alert
            account_summary = self.get_account_summary() or account_summary
            for stock in account_summary['stocks']:
                if stock['quantity'] > 0:
                    result = self.place_order('sell', stock['ticker'], stock['quantity'], stock['current_price'], account_summary)
                    if result is not None:
                        orders_executed_count += 1

        # Execute strategy based on current state
        current_state = MarketState(self.state['current_state'])

        if current_state == MarketState.NORMAL:
            logger.info("🟢 NORMAL STATE - Rebalancing Protocol Active")

            # Get top stocks by market cap
            target_holdings, market_cap_info = self.get_top_stocks_by_market_cap()

            # Send detailed market cap analysis to Discord
            self._send_market_cap_analysis(market_cap_info, account_summary)

            # Get world #1 stock ticker and current price for rebalancing
            world_1_ticker = market_cap_info.get('top_1_ticker', 'AAPL')

            # CRITICAL: Sync ATH from market cap analysis (1-year historical data)
            # This ensures rebalancing uses the correct ATH, not outdated state value
            stock_details = market_cap_info.get('stock_details', {})
            if world_1_ticker in stock_details:
                market_cap_ath = stock_details[world_1_ticker].get('ath_price')
                world_1_price = stock_details[world_1_ticker].get('current_price')

                # Check if world #1 ticker changed
                if self.state.get('current_world_1_ticker') != world_1_ticker:
                    logger.info(f"🔄 World #1 changed: {self.state.get('current_world_1_ticker')} → {world_1_ticker}")
                    logger.info(f"📊 Setting new ATH from market cap analysis: {world_1_ticker} ATH ${market_cap_ath:.2f}")
                    self.state['world_1_stock_ath'] = market_cap_ath
                    self.state['current_world_1_ticker'] = world_1_ticker
                    self._save_state()
                # Update state with correct ATH from market data (even for same ticker)
                elif market_cap_ath and market_cap_ath > self.state.get('world_1_stock_ath', 0):
                    logger.info(f"📊 Syncing ATH from market cap analysis: {world_1_ticker} ATH ${market_cap_ath:.2f} (was ${self.state.get('world_1_stock_ath', 0):.2f})")
                    self.state['world_1_stock_ath'] = market_cap_ath
                    self._save_state()
            else:
                # Fallback to live data if stock details not available
                try:
                    ticker_data = yf.download(world_1_ticker, period='5d', progress=False)
                    if not ticker_data.empty:
                        world_1_price = ticker_data['Close'].iloc[-1].item()
                    else:
                        world_1_price = 100.0  # Fallback
                except Exception as e:
                    logger.warning(f"yfinance fallback for {world_1_ticker} failed: {e} — using last known price")
                    world_1_price = self.state.get('world_1_stock_ath', 150.0)

            # Check rebalancing (CORRECTED: based on world #1 stock drawdown)
            rebalancing_action = self.calculate_rebalancing_action(account_summary, world_1_ticker, world_1_price)

            # Send rebalancing status to Discord
            self._send_rebalancing_status(rebalancing_action)

            if rebalancing_action['action'] == 'sell':
                # Stock dropped, need more cash
                self.notifier._send(
                    title="Rebalancing — Sell Required",
                    fields=[
                        {"name": f"{world_1_ticker} Stock Drawdown", "value": f"{rebalancing_action['stock_drawdown_pct']:.2f}%", "inline": True},
                        {"name": "Target Cash", "value": f"{rebalancing_action['target_cash_ratio']*100:.0f}%", "inline": True},
                        {"name": "Current Cash", "value": f"{rebalancing_action['current_cash_ratio']*100:.0f}%", "inline": True}
                    ],
                    color=16776960  # warning
                )

                # Calculate how much to sell (use values from rebalancing_action for accuracy)
                target_cash_amount = rebalancing_action['target_cash_amount']
                current_cash_amount = rebalancing_action['current_cash_amount']
                cash_needed = target_cash_amount - current_cash_amount

                if cash_needed > 0:
                    # Sell proportionally from all holdings
                    total_stock_value = sum([s['value'] for s in account_summary['stocks']])
                    for stock in account_summary['stocks']:
                        if stock['quantity'] > 0 and total_stock_value > 0:
                            sell_ratio = cash_needed / total_stock_value
                            sell_quantity = int(stock['quantity'] * sell_ratio)

                            if sell_quantity > 0:
                                result = self.place_order('sell', stock['ticker'], sell_quantity, stock['current_price'], account_summary)
                                if result is not None:
                                    orders_executed_count += 1

            elif rebalancing_action['action'] == 'buy':
                # Stock rebounded, need less cash (CORRECTED: Added buy-back logic)
                self.notifier._send(
                    title="Rebalancing — Buy Required",
                    fields=[
                        {"name": f"{world_1_ticker} Stock Rebounded", "value": f"{rebalancing_action['stock_drawdown_pct']:.2f}% from ATH", "inline": True},
                        {"name": "Target Cash", "value": f"{rebalancing_action['target_cash_ratio']*100:.0f}%", "inline": True},
                        {"name": "Current Cash", "value": f"{rebalancing_action['current_cash_ratio']*100:.0f}%", "inline": True}
                    ],
                    color=3066993  # success
                )

                # Calculate how much to buy (use values from rebalancing_action for accuracy)
                target_stock_amount = rebalancing_action['target_stock_amount']
                current_stock_amount = rebalancing_action['current_stock_amount']
                buy_amount = target_stock_amount - current_stock_amount

                if buy_amount > 0:
                    # Buy according to target holdings
                    for ticker, weight in target_holdings:
                        try:
                            ticker_data = yf.download(ticker, period='5d', progress=False)
                            if not ticker_data.empty:
                                current_price = ticker_data['Close'].iloc[-1].item()
                                allocation = buy_amount * weight
                                quantity = int(allocation / current_price)

                                if quantity > 0:
                                    result = self.place_order('buy', ticker, quantity, current_price, account_summary)
                                    if result is not None:
                                        orders_executed_count += 1
                                        logger.info(f"✅ Rebalancing buy-back: Bought {quantity} {ticker} @ ${current_price:.2f}")
                        except Exception as e:
                            logger.error(f"Error in rebalancing buy for {ticker}: {e}")

            # ===== INITIAL INVESTMENT (First time only) =====
            if not self.state.get('initial_investment_made', False):
                logger.info("🎯 INITIAL INVESTMENT - Respecting rebalancing protocol from start")

                allocated_capital = rebalancing_action.get('total_value', account_summary.get('allocated_capital', account_summary['total_value']))
                current_cash_amount = rebalancing_action.get('current_cash_amount', 0)

                # CRITICAL: Use rebalancing target to determine initial stock allocation
                # This prevents buying 100% stocks if world #1 is already in drawdown
                target_stock_ratio = rebalancing_action.get('target_stock_ratio', 1.0)
                target_cash_ratio = rebalancing_action.get('target_cash_ratio', 0.0)
                target_stock_amount = rebalancing_action.get('target_stock_amount', allocated_capital * target_stock_ratio)
                target_cash_amount = rebalancing_action.get('target_cash_amount', allocated_capital * target_cash_ratio)

                logger.info(f"Initial investment respecting rebalancing: {target_stock_ratio*100:.0f}% stocks / {target_cash_ratio*100:.0f}% cash")
                logger.info(f"  Available: ${current_cash_amount:,.2f} cash for JD strategy")

                # Check if we have enough cash to invest
                if current_cash_amount >= allocated_capital * 0.9:  # At least 90% of allocated capital available
                    self.notifier._send(
                        title="Initial Investment",
                        fields=[
                            {"name": "Allocated Capital", "value": f"${allocated_capital:,.2f}", "inline": True},
                            {"name": "Available Cash", "value": f"${current_cash_amount:,.2f}", "inline": True},
                            {"name": "Rebalancing Tier", "value": rebalancing_action.get('rebalancing_tier', 'Tier 0'), "inline": False},
                            {"name": "Target Stock", "value": f"{target_stock_ratio*100:.0f}% (${target_stock_amount:,.2f})", "inline": True},
                            {"name": "Target Cash", "value": f"{target_cash_ratio*100:.0f}% (${target_cash_amount:,.2f})", "inline": True}
                        ],
                        color=3066993  # success
                    )

                    # Invest ONLY target stock amount (not full allocated capital)
                    if target_stock_amount > 0:
                        for ticker, weight in target_holdings:
                            try:
                                ticker_data = yf.download(ticker, period='5d', progress=False)
                                if not ticker_data.empty:
                                    current_price = ticker_data['Close'].iloc[-1].item()
                                    # Calculate allocation based on target_stock_amount, not full allocated_capital
                                    allocation = target_stock_amount * weight
                                    quantity = int(allocation / current_price)

                                    if quantity > 0:
                                        result = self.place_order('buy', ticker, quantity, current_price, account_summary)
                                        if result is not None:
                                            orders_executed_count += 1
                                            logger.info(f"✅ Initial investment: Bought {quantity} {ticker} @ ${current_price:.2f}")
                            except Exception as e:
                                logger.error(f"Error making initial investment in {ticker}: {e}")

                        logger.info(f"✅ Initial investment complete: ${target_stock_amount:,.2f} in stocks, ${target_cash_amount:,.2f} in cash reserve")
                    else:
                        logger.info(f"⚠️ Initial investment: Rebalancing protocol requires 100% cash (world #1 stock in significant drawdown)")

                    if target_stock_amount == 0 or orders_executed_count > 0:
                        self.state['initial_investment_made'] = True
                        self.state['last_rebalance_date'] = datetime.now().isoformat()
                        self._save_state()
                    else:
                        logger.warning("Initial investment: no orders confirmed — will retry next run")
                else:
                    logger.warning(f"Insufficient cash for initial investment: ${current_cash_amount:,.2f} < ${allocated_capital * 0.9:,.2f}")

            # ===== MARKET CAP REBALANCING (Every 90 days) =====
            else:
                last_rebalance = self.state.get('last_rebalance_date')
                if last_rebalance:
                    last_rebalance_date = datetime.fromisoformat(last_rebalance)
                    days_since_rebalance = (datetime.now() - last_rebalance_date).days
                else:
                    days_since_rebalance = 999

                if days_since_rebalance >= self.REBALANCE_FREQUENCY_DAYS:
                    logger.info(f"📊 MARKET CAP REBALANCING - {days_since_rebalance} days since last rebalance")

                    # Get current holdings
                    current_tickers = set([stock['ticker'] for stock in account_summary['stocks']])
                    target_tickers = set([ticker for ticker, _ in target_holdings])

                    # Only rebalance if holdings changed
                    if current_tickers != target_tickers:
                        self.notifier._send(
                            title="Market Cap Rebalancing",
                            fields=[
                                {"name": "Current Holdings", "value": ", ".join(current_tickers) if current_tickers else "None", "inline": False},
                                {"name": "Target Holdings", "value": ", ".join(target_tickers), "inline": False},
                                {"name": "Days Since Last Rebalance", "value": str(days_since_rebalance), "inline": True}
                            ],
                            color=9807270  # neutral
                        )

                        # Sell all current holdings
                        for stock in account_summary['stocks']:
                            if stock['quantity'] > 0:
                                result = self.place_order('sell', stock['ticker'], stock['quantity'], stock['current_price'], account_summary)
                                if result is not None:
                                    orders_executed_count += 1
                                    logger.info(f"✅ Market cap rebalancing: Sold {stock['quantity']} {stock['ticker']}")

                        # Wait for sells to settle
                        import time
                        time.sleep(3)

                        # Refresh account to get updated cash (USD + KRW converted)
                        account_summary = self.get_account_summary()
                        allocated_capital = account_summary.get('allocated_capital', account_summary['total_value'])
                        cash_available = account_summary.get('cash_balance_total_usd', 0)

                        # Buy new target holdings
                        for ticker, weight in target_holdings:
                            try:
                                ticker_data = yf.download(ticker, period='5d', progress=False)
                                if not ticker_data.empty:
                                    current_price = ticker_data['Close'].iloc[-1].item()
                                    allocation = cash_available * weight
                                    quantity = int(allocation / current_price)

                                    if quantity > 0:
                                        result = self.place_order('buy', ticker, quantity, current_price, account_summary)
                                        if result is not None:
                                            orders_executed_count += 1
                                            logger.info(f"✅ Market cap rebalancing: Bought {quantity} {ticker} @ ${current_price:.2f}")
                            except Exception as e:
                                logger.error(f"Error in market cap rebalancing for {ticker}: {e}")

                        self.state['last_rebalance_date'] = datetime.now().isoformat()
                        self._save_state()
                    else:
                        logger.info(f"✅ Market cap holdings unchanged, no rebalancing needed")
                else:
                    logger.info(f"Next market cap rebalance in {self.REBALANCE_FREQUENCY_DAYS - days_since_rebalance} days")

        elif current_state == MarketState.ALERT:
            logger.info("🟡 ALERT STATE - Stake Planting Protocol Active")

            # Calculate NASDAQ drawdown from ATH at alert
            nasdaq_data = yf.download("^IXIC", period="5d", progress=False)
            if not nasdaq_data.empty:
                current_nasdaq = nasdaq_data['Close'].iloc[-1]
                nasdaq_ath_at_alert = self.state.get('nasdaq_ath_at_alert', current_nasdaq)
                if nasdaq_ath_at_alert > 0:
                    nasdaq_drawdown_pct = ((current_nasdaq - nasdaq_ath_at_alert) / nasdaq_ath_at_alert) * 100
                else:
                    nasdaq_drawdown_pct = 0.0

                logger.info(f"NASDAQ: ${current_nasdaq:,.2f}, ATH at Alert: ${nasdaq_ath_at_alert:,.2f}, Drawdown: {nasdaq_drawdown_pct:.2f}%")

                # Execute Stake Planting Protocol (CORRECTED: Now implemented)
                fed_regime = self.get_fed_regime()
                self._execute_stake_planting(account_summary, nasdaq_drawdown_pct, fed_regime)

            # Check exit condition (CORRECTED: Pass minus_3_count)
            can_exit, exit_reason = self._check_exit_condition(minus_3_count)
            if can_exit:
                logger.info(f"✅ EXITING ALERT STATE - {exit_reason}")
                self.notifier.send_state_change(
                    old_state='ALERT',
                    new_state='NORMAL',
                    reason=exit_reason,
                    details={
                        'Action': 'Re-entering market with available cash'
                    }
                )

                # Re-enter market if we have cash
                self._execute_reentry(account_summary, exit_reason)
                orders_executed_count += 1  # Count re-entry as executed order

                # Reset to NORMAL state
                self.state['current_state'] = MarketState.NORMAL.value
                self.state['portfolio_ath'] = account_summary.get('total_value', 0)
                self.state['nasdaq_ath_at_alert'] = 0.0
                self.state['stake_levels_executed'] = []
                self._save_state()

        elif current_state == MarketState.PANIC:
            logger.info("🔴 PANIC STATE - Holding Cash")

            # Check exit condition (CORRECTED: Pass minus_3_count)
            can_exit, exit_reason = self._check_exit_condition(minus_3_count)
            if can_exit:
                logger.info(f"✅ EXITING PANIC STATE - {exit_reason}")
                self.notifier.send_state_change(
                    old_state='PANIC',
                    new_state='NORMAL',
                    reason=exit_reason,
                    details={
                        'Action': 'Re-entering market with available cash'
                    }
                )

                # Re-enter market
                self._execute_reentry(account_summary, exit_reason)
                orders_executed_count += 1  # Count re-entry as executed order

                # Reset to NORMAL state
                self.state['current_state'] = MarketState.NORMAL.value
                self.state['portfolio_ath'] = account_summary.get('total_value', 0)
                self.state['nasdaq_ath_at_alert'] = 0.0
                self.state['stake_levels_executed'] = []
                self._save_state()

        # Save state
        self._save_state()

        # Get final account summary
        final_account_summary = self.get_account_summary()

        # Send consolidated daily summary
        if final_account_summary:
            # Calculate portfolio changes
            portfolio_change = final_account_summary['total_value'] - account_summary['total_value']
            portfolio_change_pct = (portfolio_change / account_summary['total_value'] * 100) if account_summary['total_value'] > 0 else 0

            # Get profit info
            total_profit = final_account_summary.get('total_profit', 0)
            profit_pct = (total_profit / final_account_summary['total_value'] * 100) if final_account_summary['total_value'] > 0 else 0

            # Prepare next action text
            next_action = f"State: {self.state['current_state'].upper()}"
            if self.state['current_state'] == MarketState.NORMAL.value:
                next_action += " | Monitoring for -3% NASDAQ drops"
            elif self.state['current_state'] == MarketState.ALERT.value:
                next_action += " | Stake Planting Protocol active"
            else:  # PANIC
                next_action += " | Waiting for 0 -3% events in 30d"

            # Send individual daily summary ONLY if dual strategy is disabled
            # (Portfolio summary will handle reporting in dual-strategy mode)
            if not self.dual_strategy_enabled:
                self.notifier.send_daily_summary({
                    'portfolio_value': final_account_summary['total_value'],
                    'portfolio_change': portfolio_change,
                    'portfolio_change_pct': portfolio_change_pct,
                    'cash_usd': final_account_summary.get('cash_balance_usd', 0),
                    'cash_krw': final_account_summary.get('cash_balance_krw', 0),
                    'total_profit': total_profit,
                    'total_profit_pct': profit_pct,
                    'holdings': final_account_summary.get('stocks', []),
                    'state': self.state['current_state'].upper(),
                    'trades_executed': orders_executed_count,
                    'next_action': next_action
                })

            # Save strategy result for portfolio summary
            self._save_strategy_result(
                final_account_summary,
                self.state['current_state'],
                orders_executed_count
            )

            # Portfolio summary is sent by check_and_run.sh after both bots complete

        # Send shutdown notification (skip in dual strategy mode - portfolio summary handles it)
        if not self.dual_strategy_enabled:
            self.notifier.send_shutdown({
                'trades_executed': orders_executed_count if 'orders_executed_count' in dir() else 0,
                'portfolio_value': final_account_summary['total_value'] if 'final_account_summary' in dir() and final_account_summary else 0
            })

        logger.info("=" * 70)
        logger.info("=== JD Strategy Bot Session Complete ===")
        logger.info("=" * 70)


if __name__ == "__main__":
    # SAFETY: Default to paper trading mode
    # To enable live trading, pass paper_trading=False
    trader = JDStrategyTrader(paper_trading=False)
    trader.run()



