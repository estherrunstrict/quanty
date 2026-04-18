#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Upbit BTC Volatility Breakthrough (Long-Only) — Auto Trading Bot
=================================================================

Larry Williams VB strategy with Equity Momentum allocation.
- Entry: Open + 0.40 × ATR(7) (long only)
- Filter: Close > MA(5)
- Exit: US market close (16:00 ET = 05:00 KST EDT / 06:00 KST EST)
- Allocation: 100% when equity > MA(40), 30% when below
- Commission: Upbit KRW market 0.05% per trade

Author: Based on backtest optimization (21,660 combos)
Version: 1.0
"""

import os
import sys
import json
import yaml
import logging
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from dataclasses import dataclass, field, asdict
import numpy as np
import pyupbit

# Discord notifications
from discord_notifier import get_notifier

# ==============================================================================
# CONFIGURATION
# ==============================================================================

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(CURRENT_DIR, "upbit_vb_strategy_state.json")
LOG_FILE = os.path.join(CURRENT_DIR, "upbit_vb_strategy.log")
KST = timezone(timedelta(hours=9))

# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("VB_Strategy")

# ==============================================================================
# STATE
# ==============================================================================

@dataclass
class VBState:
    """Persistent state across daily runs."""
    # Equity curve tracking (list of cumulative equity values for MA40)
    equity_history: list = field(default_factory=list)  # [(date_str, equity_value), ...]
    # Current position
    is_holding: bool = False
    entry_price: float = 0.0
    entry_date: str = ""
    entry_amount_krw: float = 0.0
    btc_amount: float = 0.0
    # Lifetime stats
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl_krw: float = 0.0
    # Allocation state
    current_allocation: float = 1.0  # 1.0 = 100%, 0.3 = 30%
    # Last run
    last_run_date: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'VBState':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ==============================================================================
# VB STRATEGY
# ==============================================================================

class UpbitVBStrategy:
    """
    BTC Volatility Breakthrough — Long-Only with Equity Momentum Allocation.

    Daily flow (09:00 KST):
    1. If holding from yesterday → SELL at market (exit at open)
    2. Calculate today's breakout target
    3. Determine allocation (equity momentum)
    4. Place limit BUY at target price
    5. Monitor until 23:59 KST, cancel if not filled
    """

    # Strategy parameters (optimized)
    K_LONG = 0.40
    ATR_PERIOD = 7
    MA_PERIOD = 5
    EQUITY_MA_PERIOD = 40
    BULL_ALLOCATION = 1.00   # equity > MA40
    BEAR_ALLOCATION = 0.30   # equity <= MA40
    TICKER = "KRW-BTC"

    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.upbit = self._init_upbit()

        # VB-specific config from config.yaml
        vb_config = self.config.get('upbit_vb_strategy', {})
        self.RESERVE_CASH_KRW = vb_config.get('reserve_cash_krw', 100000)
        self.MAX_CAPITAL_KRW = vb_config.get('max_capital_krw', 0)  # 0 = unlimited
        self.PAPER_TRADING = vb_config.get('paper_trading', False)
        self.MIN_ORDER_KRW = vb_config.get('min_order_krw', 5000)

        # Override strategy params from config if present
        self.K_LONG = vb_config.get('k_long', self.K_LONG)
        self.ATR_PERIOD = vb_config.get('atr_period', self.ATR_PERIOD)
        self.MA_PERIOD = vb_config.get('ma_period', self.MA_PERIOD)
        self.EQUITY_MA_PERIOD = vb_config.get('equity_ma_period', self.EQUITY_MA_PERIOD)
        self.BULL_ALLOCATION = vb_config.get('bull_allocation', self.BULL_ALLOCATION)
        self.BEAR_ALLOCATION = vb_config.get('bear_allocation', self.BEAR_ALLOCATION)
        # Discord
        webhook_url = self.config.get('DISCORD_WEBHOOK_URL')
        self.notifier = get_notifier(webhook_url, bot_type='upbit_vb')

        # State
        self.state = VBState()
        self._load_state()

        mode = "PAPER" if self.PAPER_TRADING else "REAL"
        logger.info(f"Initialized VB Strategy [{mode}] | k={self.K_LONG} ATR={self.ATR_PERIOD} MA={self.MA_PERIOD}")

    # ── Config & API ────────────────────────────────────────────────────────

    def _load_config(self, path: str) -> dict:
        full_path = os.path.join(CURRENT_DIR, path)
        with open(full_path, 'r') as f:
            return yaml.safe_load(f)

    def _init_upbit(self) -> pyupbit.Upbit:
        access = self.config['upbit']['access_key']
        secret = self.config['upbit']['secret_key']
        upbit = pyupbit.Upbit(access, secret)
        balances = upbit.get_balances()
        logger.info(f"Upbit connected. {len(balances)} balance entries.")
        return upbit

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    self.state = VBState.from_dict(json.load(f))
                logger.info(f"State loaded. Trades: {self.state.total_trades}, "
                            f"Holding: {self.state.is_holding}")
            except Exception as e:
                logger.warning(f"Failed to load state, starting fresh: {e}")

    def _save_state(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state.to_dict(), f, indent=2)

    # ── Market Data ─────────────────────────────────────────────────────────

    def get_ohlcv(self, count: int = 30) -> Optional[list]:
        """Get daily OHLCV from Upbit. Returns list of dicts."""
        try:
            df = pyupbit.get_ohlcv(self.TICKER, interval="day", count=count)
            if df is None or df.empty:
                logger.error("Failed to get OHLCV data")
                return None
            return df
        except Exception as e:
            logger.error(f"OHLCV fetch error: {e}")
            return None

    def calc_atr(self, df, period: int) -> float:
        """Calculate ATR (Average True Range)."""
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        tr = np.maximum(high[1:] - low[1:],
                        np.maximum(np.abs(high[1:] - close[:-1]),
                                   np.abs(low[1:] - close[:-1])))
        if len(tr) < period:
            return float(np.mean(tr))
        return float(np.mean(tr[-period:]))

    def calc_ma(self, df, period: int) -> float:
        """Calculate simple moving average of close."""
        closes = df['close'].values
        if len(closes) < period:
            return float(np.mean(closes))
        return float(np.mean(closes[-period:]))

    def get_current_price(self) -> Optional[float]:
        """Get current BTC price."""
        try:
            return pyupbit.get_current_price(self.TICKER)
        except:
            return None

    # ── Portfolio ───────────────────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        """Get current KRW balance and BTC holding."""
        try:
            krw_balance = self.upbit.get_balance("KRW")
            btc_balance = self.upbit.get_balance("BTC")
            btc_price = self.get_current_price() or 0
            btc_value = (btc_balance or 0) * btc_price

            return {
                "krw": float(krw_balance or 0),
                "btc": float(btc_balance or 0),
                "btc_value": btc_value,
                "btc_price": btc_price,
                "total": float(krw_balance or 0) + btc_value,
            }
        except Exception as e:
            logger.error(f"Portfolio fetch error: {e}")
            return {"krw": 0, "btc": 0, "btc_value": 0, "btc_price": 0, "total": 0}

    def get_available_capital(self, portfolio: dict) -> float:
        """Calculate capital available for trading (respecting reserve and max)."""
        available = portfolio["krw"] - self.RESERVE_CASH_KRW
        if self.MAX_CAPITAL_KRW > 0:
            available = min(available, self.MAX_CAPITAL_KRW)
        return max(available, 0)

    # ── Equity Momentum Allocation ──────────────────────────────────────────

    def update_equity_and_allocation(self, portfolio: dict):
        """
        Track equity curve and determine allocation.
        equity > MA(40) → 100% allocation
        equity <= MA(40) → 30% allocation
        """
        today = datetime.now(KST).strftime("%Y-%m-%d")
        equity_val = portfolio["total"]

        # Append today's equity (avoid duplicate for same day)
        if self.state.equity_history and self.state.equity_history[-1][0] == today:
            self.state.equity_history[-1] = (today, equity_val)
        else:
            self.state.equity_history.append((today, equity_val))

        # Keep only last 100 data points
        if len(self.state.equity_history) > 100:
            self.state.equity_history = self.state.equity_history[-100:]

        # Calculate MA(40) of equity
        values = [v for _, v in self.state.equity_history]
        if len(values) >= self.EQUITY_MA_PERIOD:
            eq_ma = np.mean(values[-self.EQUITY_MA_PERIOD:])
            if equity_val > eq_ma:
                self.state.current_allocation = self.BULL_ALLOCATION
                logger.info(f"Equity ₩{equity_val:,.0f} > MA40 ₩{eq_ma:,.0f} → FULL allocation ({self.BULL_ALLOCATION:.0%})")
            else:
                self.state.current_allocation = self.BEAR_ALLOCATION
                logger.info(f"Equity ₩{equity_val:,.0f} ≤ MA40 ₩{eq_ma:,.0f} → REDUCED allocation ({self.BEAR_ALLOCATION:.0%})")
        else:
            # Not enough history yet — default to full
            self.state.current_allocation = self.BULL_ALLOCATION
            logger.info(f"Equity history {len(values)}/{self.EQUITY_MA_PERIOD} days — default FULL allocation")

    # ── Trade Execution ─────────────────────────────────────────────────────

    # Sell result sentinels
    SELL_NOTHING = "nothing_to_sell"  # dust or zero balance — safe to continue
    SELL_FAILED = "sell_api_failed"   # API error — NOT safe, position still open

    def sell_at_market(self) -> Optional[dict]:
        """Sell all BTC at market price (exit signal).
        Returns: dict on success, SELL_NOTHING if dust/zero, SELL_FAILED on API error.
        """
        portfolio = self.get_portfolio()
        btc_amount = portfolio["btc"]

        if btc_amount <= 0 or btc_amount * portfolio["btc_price"] < self.MIN_ORDER_KRW:
            logger.info("No BTC to sell or below minimum order (dust).")
            self.state.is_holding = False
            self._save_state()
            return self.SELL_NOTHING

        if self.PAPER_TRADING:
            pnl = portfolio["btc_value"] - self.state.entry_amount_krw
            logger.info(f"[PAPER] SELL {btc_amount:.8f} BTC at ₩{portfolio['btc_price']:,.0f} "
                        f"| Value: ₩{portfolio['btc_value']:,.0f} | PnL: ₩{pnl:,.0f}")
            result = {"price": portfolio["btc_price"], "amount": btc_amount,
                      "value": portfolio["btc_value"], "pnl": pnl}
        else:
            try:
                order = self.upbit.sell_market_order(self.TICKER, btc_amount)
                # pyupbit returns None on internal error (doesn't raise)
                if order is None or (isinstance(order, dict) and 'error' in order):
                    logger.error(f"Sell order returned error/None: {order}")
                    return self.SELL_FAILED
                time.sleep(5)  # wait for fill + settlement
                # Get actual fill price from order details
                sell_price = self._get_order_avg_price(order) or portfolio["btc_price"]
                sell_value = btc_amount * sell_price
                pnl = sell_value - self.state.entry_amount_krw
                logger.info(f"SELL {btc_amount:.8f} BTC at ~₩{sell_price:,.0f} | PnL: ₩{pnl:,.0f}")
                result = {"price": sell_price, "amount": btc_amount,
                          "value": sell_value, "pnl": pnl, "order": order}
            except Exception as e:
                logger.error(f"Sell order failed: {e}")
                return self.SELL_FAILED

        # Update state
        self.state.total_trades += 1
        pnl = result.get("pnl", 0)
        if pnl > 0:
            self.state.winning_trades += 1
        self.state.total_pnl_krw += pnl
        self.state.is_holding = False
        self.state.entry_price = 0
        self.state.btc_amount = 0
        self._save_state()

        self._notify_trade("SELL (Exit)", result)
        return result

    def buy_market(self, target_price: float, capital_krw: float) -> Optional[dict]:
        """Place market buy order after breakout confirmed."""
        if capital_krw < self.MIN_ORDER_KRW:
            logger.info(f"Capital ₩{capital_krw:,.0f} below minimum ₩{self.MIN_ORDER_KRW:,.0f}. Skip.")
            return None

        if self.PAPER_TRADING:
            btc_amount = capital_krw / target_price
            logger.info(f"[PAPER] MARKET BUY {btc_amount:.8f} BTC at ~₩{target_price:,.0f} "
                        f"| Capital: ₩{capital_krw:,.0f}")
            self.state.is_holding = True
            self.state.entry_price = target_price
            self.state.entry_date = datetime.now(KST).strftime("%Y-%m-%d")
            self.state.entry_amount_krw = capital_krw
            self.state.btc_amount = btc_amount
            self._save_state()
            return {"price": target_price, "amount": btc_amount, "capital": capital_krw}
        else:
            try:
                # Snapshot BTC balance before buy to compute delta
                pre_btc = self.get_portfolio()["btc"]

                # Place market buy order
                order = self.upbit.buy_market_order(self.TICKER, capital_krw)
                # pyupbit returns None on internal error (doesn't raise)
                if order is None or (isinstance(order, dict) and 'error' in order):
                    logger.error(f"Buy order returned error/None: {order}")
                    return None
                time.sleep(5)  # Wait for fill + settlement

                # Compute filled amount as delta (ignores pre-existing BTC)
                post_portfolio = self.get_portfolio()
                filled_btc = post_portfolio["btc"] - pre_btc

                if filled_btc > 0:
                    # Get actual fill price from order details
                    avg_price = self._get_order_avg_price(order) or post_portfolio["btc_price"]
                    logger.info(f"MARKET BUY filled: {filled_btc:.8f} BTC at ~₩{avg_price:,.0f} "
                                f"(pre: {pre_btc:.8f}, post: {post_portfolio['btc']:.8f})")

                    self.state.is_holding = True
                    self.state.entry_price = avg_price
                    self.state.entry_date = datetime.now(KST).strftime("%Y-%m-%d")
                    self.state.entry_amount_krw = capital_krw
                    self.state.btc_amount = filled_btc
                    self._save_state()

                    self._notify_trade("MARKET BUY (Filled)", {
                        "price": avg_price, "amount": filled_btc, "capital": capital_krw
                    })
                    return {"price": avg_price, "amount": filled_btc,
                            "capital": capital_krw, "order": order}
                else:
                    logger.warning(f"Market buy placed but no BTC delta detected "
                                   f"(pre: {pre_btc:.8f}, post: {post_portfolio['btc']:.8f}).")
                    return None
            except Exception as e:
                logger.error(f"Buy market order failed: {e}")
                return None

    def cancel_open_orders(self):
        """Cancel all open orders for BTC."""
        try:
            orders = self.upbit.get_order(self.TICKER, state="wait")
            if orders:
                for order in orders:
                    self.upbit.cancel_order(order['uuid'])
                    logger.info(f"Cancelled order: {order['uuid']}")
                logger.info(f"Cancelled {len(orders)} open orders.")
        except Exception as e:
            logger.warning(f"Cancel orders failed: {e}")

    def _get_order_avg_price(self, order, retries: int = 3) -> Optional[float]:
        """Get average fill price from Upbit order response.
        Queries order details via UUID for actual execution price.
        Retries if trades array is empty (async settlement race).
        Returns None if unavailable (caller should fall back to spot price).
        """
        try:
            if not order or not isinstance(order, dict):
                return None
            uuid = order.get('uuid')
            if not uuid:
                return None

            for attempt in range(retries):
                detail = self.upbit.get_individual_order(uuid)
                if not detail:
                    continue

                # Upbit order detail has 'trades' array with 'price' and 'volume' per fill
                trades = detail.get('trades', [])
                if trades:
                    # Each trade has: price (fill price per unit), volume (filled qty), funds (total = price * volume)
                    total_funds = 0
                    total_volume = 0
                    for t in trades:
                        vol = float(t.get('volume', 0))
                        # Use 'funds' if available, otherwise compute from price * volume
                        funds = t.get('funds')
                        if funds:
                            total_funds += float(funds)
                        else:
                            total_funds += float(t.get('price', 0)) * vol
                        total_volume += vol
                    if total_volume > 0:
                        avg = total_funds / total_volume
                        logger.info(f"Order {uuid[:8]}.. avg fill price: ₩{avg:,.0f} ({len(trades)} trades)")
                        return avg

                # Fallback: Upbit v1 order response has 'executed_volume' and 'paid_fee'
                # Can compute from executed_volume and known total funds
                exec_vol = float(detail.get('executed_volume', 0))
                if exec_vol > 0 and detail.get('state') == 'done':
                    # For market buys, 'price' is total KRW spent; for sells, it's None
                    order_price = detail.get('price')
                    if order_price:
                        avg = float(order_price) / exec_vol
                        logger.info(f"Order {uuid[:8]}.. avg fill price (from executed): ₩{avg:,.0f}")
                        return avg

                # Trades not yet settled — wait and retry
                if attempt < retries - 1:
                    logger.info(f"Order {uuid[:8]}.. trades not yet available, retrying ({attempt+1}/{retries})...")
                    time.sleep(2)

        except Exception as e:
            logger.warning(f"Could not get order avg price: {e}")
        return None

    # ── Monitoring Loop ─────────────────────────────────────────────────────

    def monitor_breakout(self, target_price: float, timeout_minutes: int = 1430):
        """
        Monitor price until breakout target is hit or timeout.
        Default timeout: 1430 minutes (23h 50m) to catch overnight breakouts.
        For paper trading, check once.
        """
        if self.PAPER_TRADING:
            # In paper mode, check current price vs target
            current = self.get_current_price()
            if current and current >= target_price:
                logger.info(f"[PAPER] Breakout triggered: ₩{current:,.0f} ≥ ₩{target_price:,.0f}")
                return True
            else:
                logger.info(f"[PAPER] No breakout: ₩{current:,.0f} < ₩{target_price:,.0f}")
                return False

        logger.info(f"Monitoring for breakout at ₩{target_price:,.0f} (timeout: {timeout_minutes}min)")
        start = time.time()
        check_interval = 60  # check every 60 seconds
        consecutive_failures = 0
        FAILURE_ALERT_THRESHOLD = 10  # alert after 10 consecutive failures (10 min)
        alerted_feed_down = False

        while (time.time() - start) < timeout_minutes * 60:
            try:
                current = self.get_current_price()
                if current and current >= target_price:
                    logger.info(f"BREAKOUT! ₩{current:,.0f} ≥ ₩{target_price:,.0f}")
                    return True
                consecutive_failures = 0  # reset on success
                if alerted_feed_down:
                    logger.info("Price feed recovered.")
                    alerted_feed_down = False
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures == FAILURE_ALERT_THRESHOLD and not alerted_feed_down:
                    alerted_feed_down = True
                    logger.warning(f"Price feed down for {FAILURE_ALERT_THRESHOLD} consecutive checks: {e}")
                    try:
                        self.notifier._send(
                            title="Price Feed Unavailable",
                            fields=[
                                {"name": "Duration", "value": f"No data for {FAILURE_ALERT_THRESHOLD} min", "inline": True},
                                {"name": "Breakout Target", "value": f"₩{target_price:,.0f}", "inline": True},
                                {"name": "Status", "value": "Monitoring continues — will resume on recovery", "inline": False},
                                {"name": "Error Detail", "value": f"```{str(e)[:150]}```", "inline": False},
                            ],
                            color=self.notifier.COLORS.get('warning', 16776960)
                        )
                    except:
                        pass
            time.sleep(check_interval)

        logger.info(f"No breakout within {timeout_minutes} minutes. Target was ₩{target_price:,.0f}")
        return False

    # ── Discord ─────────────────────────────────────────────────────────────

    def _notify_trade(self, action: str, details: dict):
        try:
            is_buy = 'BUY' in action
            emoji = "🟢" if is_buy else "🔴"
            price = details.get('price', 0)
            amount = details.get('amount', 0)
            capital = details.get('capital', details.get('value', 0))
            pnl = details.get('pnl', 0)

            # PnL display for sells
            pnl_line = ""
            if not is_buy and pnl != 0:
                pnl_emoji = "🔥" if pnl > 0 else "💧"
                pnl_pct = (pnl / self.state.entry_amount_krw * 100) if self.state.entry_amount_krw > 0 else 0
                pnl_line = f"\n{pnl_emoji} **P&L: ₩{pnl:+,.0f}** ({pnl_pct:+.1f}%)"

            # Win rate
            wr = (self.state.winning_trades / self.state.total_trades * 100
                  if self.state.total_trades > 0 else 0)

            self.notifier._send(
                title=f"{'BUY' if is_buy else 'SELL'} Order Filled",
                fields=[
                    {"name": "Price", "value": f"₩{price:,.0f}", "inline": True},
                    {"name": "Amount", "value": f"{amount:.8f} BTC", "inline": True},
                    {"name": "Order Size", "value": f"₩{capital:,.0f}", "inline": True},
                    {"name": "Details",
                     "value": f"Allocation: {self.state.current_allocation:.0%}{pnl_line}",
                     "inline": False},
                ],
                color=self.notifier.COLORS.get('trade_buy' if is_buy else 'trade_sell', 3447003),
                footer=f"{self.state.total_trades} trades | Win rate {wr:.0f}% | Cumulative P&L ₩{self.state.total_pnl_krw:+,.0f}"
            )
        except Exception as e:
            logger.warning(f"Discord notification failed: {e}")

    def _notify_daily_summary(self, portfolio: dict, atr: float, target: float,
                              signal_dir: str, open_price: float):
        try:
            wr = (self.state.winning_trades / self.state.total_trades * 100
                  if self.state.total_trades > 0 else 0)

            # Signal direction styling
            if signal_dir == "LONG":
                dir_display = "LONG — Bullish (Close > MA5)"
                color = self.notifier.COLORS['success']
            elif "NO SIGNAL" in signal_dir:
                dir_display = "NO SIGNAL — Cash position (Close < MA5)"
                color = self.notifier.COLORS['neutral']
            else:
                dir_display = f"{signal_dir}"
                color = self.notifier.COLORS['info']

            # Breakout distance
            breakout_info = "—"
            if target > 0 and open_price > 0:
                gap_pct = (target - open_price) / open_price * 100
                breakout_info = f"₩{target:,.0f} (+{gap_pct:.2f}% from open)"

            # Allocation bar (visual)
            alloc = self.state.current_allocation
            alloc_emoji = "🟩" if alloc >= 0.8 else "🟨" if alloc >= 0.5 else "🟥"
            alloc_label = "FULL" if alloc >= 0.8 else "REDUCED"

            self.notifier._send(
                title=f"Daily Signal — {datetime.now(KST).strftime('%m/%d %H:%M')} KST",
                fields=[
                    {"name": "Signal", "value": dir_display, "inline": False},
                    {"name": "Open Price", "value": f"₩{open_price:,.0f}", "inline": True},
                    {"name": "Breakout Target", "value": breakout_info, "inline": True},
                    {"name": "ATR(7)", "value": f"₩{atr:,.0f}", "inline": True},
                    {"name": "Allocation", "value": f"{alloc:.0%} ({alloc_label})", "inline": True},
                    {"name": "Portfolio", "value": f"₩{portfolio['total']:,.0f}", "inline": True},
                    {"name": "Track Record",
                     "value": f"{self.state.total_trades} trades | Win rate {wr:.0f}% | P&L ₩{self.state.total_pnl_krw:+,.0f}",
                     "inline": False},
                ],
                color=color
            )
        except Exception as e:
            logger.warning(f"Discord summary failed: {e}")

    # ── Main Daily Run ──────────────────────────────────────────────────────

    def run(self):
        """
        Main daily execution flow (run at 09:00 KST).

        1. Exit yesterday's position (sell at US close 16:00 ET, or immediately)
        2. Get OHLCV data, calculate ATR/MA
        3. Determine signal direction (long only: Close > MA5)
        4. Calculate breakout target
        5. Determine allocation (equity momentum)
        6. Place limit buy at target
        7. Monitor until filled or timeout (until 08:50 KST next day)
        """
        today = datetime.now(KST).strftime("%Y-%m-%d")
        logger.info(f"{'='*60}")
        logger.info(f"  BTC VB Strategy — Daily Run {today}")
        logger.info(f"{'='*60}")

        # Prevent double-run
        if self.state.last_run_date == today:
            logger.warning(f"Already ran today ({today}). Skipping.")
            return

        try:
            # Step 0: Always cancel stale open orders first (crash recovery safety)
            logger.info("Step 0: Cancelling any stale open orders...")
            self.cancel_open_orders()

            # Step 1: Exit yesterday's position
            if self.state.is_holding:
                logger.info("Step 1: Exiting yesterday's position...")
                sell_result = self.sell_at_market()
                if sell_result == self.SELL_FAILED:
                    # API error — position still open, abort to prevent double exposure
                    logger.error("SELL order API failed. Aborting daily run to prevent unintended exposure.")
                    try:
                        self.notifier._send(
                            title="Sell Order Failed — Manual Action Required",
                            fields=[
                                {"name": "Issue", "value": "Could not exit yesterday's BTC position", "inline": False},
                                {"name": "Required Action", "value": "Manually sell BTC on Upbit — bot paused until resolved", "inline": False},
                                {"name": "Open Position", "value": f"Entry: ₩{self.state.entry_price:,.0f} | BTC: {self.state.btc_amount:.8f}", "inline": False},
                            ],
                            color=self.notifier.COLORS['error']
                        )
                    except:
                        pass
                    return
                # SELL_NOTHING (dust) or dict (success) — safe to continue
                time.sleep(10)  # wait for sell settlement before capital calc
            else:
                logger.info("Step 1: No position to exit.")
                # Reconcile: check if we actually hold BTC (e.g. from partial fill)
                portfolio_check = self.get_portfolio()
                if portfolio_check["btc"] > 0 and portfolio_check["btc_value"] >= self.MIN_ORDER_KRW:
                    logger.warning(f"Unexpected BTC holding: {portfolio_check['btc']:.8f} BTC "
                                   f"(₩{portfolio_check['btc_value']:,.0f}). Selling...")
                    self.state.is_holding = True
                    rec_result = self.sell_at_market()
                    if rec_result == self.SELL_FAILED:
                        logger.error("Reconcile sell failed. Aborting to prevent double exposure.")
                        try:
                            self.notifier._send(
                                title="Untracked BTC Position — Sell Failed",
                                fields=[
                                    {"name": "Issue", "value": "Detected unexpected BTC balance that could not be sold", "inline": False},
                                    {"name": "Position", "value": f"BTC: {portfolio_check['btc']:.8f} (approx. ₩{portfolio_check['btc_value']:,.0f})", "inline": False},
                                    {"name": "Required Action", "value": "Manually sell on Upbit — bot paused to prevent double exposure", "inline": False},
                                ],
                                color=self.notifier.COLORS['error']
                            )
                        except:
                            pass
                        return
                    time.sleep(10)  # wait for settlement

            # Step 2: Get market data
            logger.info("Step 2: Fetching market data...")
            df = self.get_ohlcv(count=30)
            if df is None:
                logger.error("Cannot get market data. Aborting.")
                return

            # Check if last row is today (KST). If trading EXACTLY at 09:00:00,
            # API might return yesterday as the last row depending on caching.
            ts = df.index[-1]
            if ts.tzinfo is None:
                ts = ts.tz_localize('UTC')
            last_dt = ts.tz_convert(KST)
            today_date = datetime.now(KST).date()

            if last_dt.date() == today_date:
                # Exclude today's incomplete candle for ATR/MA calculation
                df_complete = df.iloc[:-1]
                today_open = float(df['open'].iloc[-1])
            else:
                # API hasn't flipped yet — retry after short delay
                logger.info("API candle not yet flipped to today. Retrying in 60s...")
                time.sleep(60)
                df_retry = self.get_ohlcv(count=30)
                if df_retry is not None:
                    ts2 = df_retry.index[-1]
                    if ts2.tzinfo is None:
                        ts2 = ts2.tz_localize('UTC')
                    if ts2.tz_convert(KST).date() == today_date:
                        df = df_retry
                        df_complete = df.iloc[:-1]
                        today_open = float(df['open'].iloc[-1])
                        logger.info(f"Retry succeeded. Today's candle available. Open: ₩{today_open:,.0f}")
                    else:
                        # Still not flipped — use all rows as complete, fall back to live price
                        df_complete = df
                        today_open = self.get_current_price() or float(df['close'].iloc[-1])
                        logger.warning(f"API still shows yesterday after retry. Using live price as open: ₩{today_open:,.0f}")
                else:
                    df_complete = df
                    today_open = self.get_current_price() or float(df['close'].iloc[-1])
                    logger.warning(f"Retry OHLCV fetch failed. Using live price as open: ₩{today_open:,.0f}")

            atr = self.calc_atr(df_complete, self.ATR_PERIOD)
            ma = self.calc_ma(df_complete, self.MA_PERIOD)
            prev_close = float(df_complete['close'].iloc[-1])  # yesterday's close

            logger.info(f"  ATR(7): ₩{atr:,.0f}")
            logger.info(f"  MA(5):  ₩{ma:,.0f}")
            logger.info(f"  Prev Close: ₩{prev_close:,.0f}")
            logger.info(f"  Today Open:  ₩{today_open:,.0f}")

            # Step 3: Signal direction (LONG ONLY)
            if prev_close < ma:
                logger.info("Step 3: Close < MA(5) → NO SIGNAL (bearish). Sitting in cash.")
                self._notify_daily_summary(self.get_portfolio(), atr, 0, "NO SIGNAL (bearish)", today_open)
                self.state.last_run_date = today
                # Still update equity for MA40 tracking
                portfolio = self.get_portfolio()
                self.update_equity_and_allocation(portfolio)
                self._save_state()
                return

            logger.info("Step 3: Close ≥ MA(5) → LONG signal active.")

            # Step 4: Calculate breakout target
            target_price = today_open + self.K_LONG * atr
            logger.info(f"Step 4: Breakout target = ₩{today_open:,.0f} + {self.K_LONG} × ₩{atr:,.0f} = ₩{target_price:,.0f}")

            # Step 5: Allocation
            portfolio = self.get_portfolio()
            self.update_equity_and_allocation(portfolio)

            available = self.get_available_capital(portfolio)
            trade_capital = available * self.state.current_allocation

            logger.info(f"Step 5: Available ₩{available:,.0f} × {self.state.current_allocation:.0%} "
                        f"= ₩{trade_capital:,.0f}")

            # Send daily summary
            self._notify_daily_summary(portfolio, atr, target_price, "LONG", today_open)

            # Step 6: Monitor and Execute
            if trade_capital < self.MIN_ORDER_KRW:
                logger.info(f"Trade capital ₩{trade_capital:,.0f} below minimum. No trade.")
                self.state.last_run_date = today
                self._save_state()
                return

            logger.info(f"Step 6: Monitoring for breakout at ₩{target_price:,.0f}...")

            # Step 7: Monitor for breakout
            # Calculate remaining minutes until next reset (15:55 ET = 5 min before next run)
            now_utc = datetime.now(timezone.utc)
            # Determine current ET offset (EDT=-4, EST=-5)
            # Simple DST check: March 2nd Sunday to November 1st Sunday
            yr = now_utc.year
            mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
            dst_on = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
            nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
            dst_off = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=7)
            et_offset = -4 if dst_on <= now_utc < dst_off else -5
            et_tz = timezone(timedelta(hours=et_offset))
            now_et = now_utc.astimezone(et_tz)
            next_cutoff_et = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
            if next_cutoff_et <= now_et:
                next_cutoff_et += timedelta(days=1)
            next_cutoff_kst = next_cutoff_et.astimezone(KST)
            remaining_min = max(10, int((next_cutoff_kst - datetime.now(KST)).total_seconds() / 60))
            logger.info(f"  Monitor timeout: {remaining_min} minutes (until {next_cutoff_kst.strftime('%H:%M KST')} / 15:55 ET)")
            hit = self.monitor_breakout(target_price, timeout_minutes=remaining_min)
            if hit:
                logger.info(f"Step 7: Breakout triggered! Placing market buy for ₩{trade_capital:,.0f}...")
                order = self.buy_market(target_price, trade_capital)
                
                if order is None:
                    logger.error("Market buy failed after breakout confirmation.")
            else:
                logger.info("Breakout not reached today.")

            self.state.last_run_date = today
            self._save_state()

            logger.info(f"Daily run complete.")

        except Exception as e:
            logger.error(f"Run failed: {e}")
            logger.error(traceback.format_exc())
            try:
                self.notifier._send(
                    title="Runtime Error — Daily Run Stopped",
                    fields=[
                        {"name": "Issue", "value": "An unexpected error terminated the daily run", "inline": False},
                        {"name": "Error", "value": f"```{str(e)[:400]}```", "inline": False},
                        {"name": "Position Status", "value": f"Holding: {'Yes' if self.state.is_holding else 'No'} | Last run: {self.state.last_run_date or 'N/A'}", "inline": False},
                    ],
                    color=self.notifier.COLORS['error']
                )
            except:
                pass


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    logger.info("Starting BTC Volatility Breakthrough Auto Trader...")
    strategy = UpbitVBStrategy()
    strategy.run()
    logger.info("Done.")


if __name__ == "__main__":
    main()
