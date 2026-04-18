"""LiveEngine — orchestrates strategy execution for live trading.

Lifecycle:
  1. Authenticate with broker
  2. Load strategy state
  3. Fetch historical data (from cache + provider)
  4. Detect regime
  5. Generate signals
  6. Compute target allocation
  7. Execute rebalancing orders via broker
  8. Save strategy state
  9. Send Discord notifications
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from framework.strategy import BaseStrategy
from framework.broker import BaseBroker
from framework.data.provider import BaseDataProvider
from framework.regime.detector import RegimeDetector
from framework.types import MarketRegime, Order, Portfolio, SignalDirection

logger = logging.getLogger(__name__)


class LiveEngine:
    """Runs a strategy once against live market data and executes orders."""

    def __init__(
        self,
        strategy: BaseStrategy,
        broker: BaseBroker,
        data_provider: BaseDataProvider,
        regime_detector: Optional[RegimeDetector] = None,
        notifier=None,
        budget_ceiling: float = 0,
    ):
        self.strategy = strategy
        self.broker = broker
        self.data_provider = data_provider
        self.regime_detector = regime_detector
        self.notifier = notifier
        self.budget_ceiling = budget_ceiling

    def run(self):
        """Execute one cycle of the strategy."""
        name = self.strategy.name
        logger.info(f"=== [{name}] Live engine start ===")

        try:
            # 1. Load state
            self.strategy.load_state()

            # 2. Fetch data for ALL assets (up to yesterday)
            yesterday = date.today() - timedelta(days=1)
            warmup_start = date.today() - timedelta(days=400)
            assets = self.strategy.get_asset_list()
            primary = assets[0]

            # Fetch data for all assets — strategies may need multi-asset data
            all_data = {}
            for asset in assets:
                all_data[asset] = self.data_provider.get_historical_data(asset, warmup_start, yesterday)

            data = all_data[primary]
            if data.empty or len(data) < 30:
                logger.error(f"[{name}] Insufficient data: {len(data)} rows")
                self._notify("Error", f"Insufficient data for {name}: {len(data)} rows")
                return

            # Build price map for all assets (for rebalancing)
            self._price_map = {}
            for asset, df in all_data.items():
                if not df.empty:
                    self._price_map[asset] = float(df["Close"].iloc[-1])

            # 3. Detect regime
            regime = MarketRegime.SIDEWAYS
            if self.regime_detector:
                regime = self.regime_detector.detect(data)
                if regime != self.strategy.current_regime:
                    self.strategy.on_regime_change(self.strategy.current_regime, regime)
            logger.info(f"[{name}] Regime: {regime.value}")

            # 4. Generate signals
            signals = self.strategy.generate_signals(data)
            signal_summary = {t: f"{s.direction.value} ({s.reason})" for t, s in signals.items()}
            logger.info(f"[{name}] Signals: {signal_summary}")

            # 5. Get portfolio state
            positions = self.broker.get_positions()
            cash = self.broker.get_cash_balance()
            total = cash + sum(p.market_value for p in positions)

            portfolio = Portfolio(
                positions=positions,
                cash_balance=cash,
                total_value=total,
                budget_ceiling=self.budget_ceiling or total,
            )
            logger.info(f"[{name}] Portfolio: ${total:,.2f} ({len(positions)} positions, ${cash:,.2f} cash)")

            # 6. Compute allocation
            allocation = self.strategy.get_target_allocation(signals, regime, portfolio)
            logger.info(f"[{name}] Target allocation: {allocation}")

            # 7. Execute rebalancing
            effective_capital = min(total, self.budget_ceiling) if self.budget_ceiling > 0 else total
            orders_placed = self._execute_rebalance(allocation, portfolio, effective_capital, data)

            # 8. Save state
            self.strategy.save_state()

            # 9. Write result JSON for portfolio_summary.py
            self._save_result_json(name, regime, signals, allocation, portfolio, orders_placed)

            # 10. Notify
            self._notify_summary(name, regime, signals, allocation, portfolio, orders_placed)

            logger.info(f"=== [{name}] Live engine done ===")

        except Exception as e:
            logger.error(f"[{name}] Live engine error: {e}", exc_info=True)
            self._notify("Error", f"{name} failed: {e}")

    def _execute_rebalance(self, allocation, portfolio, capital, data):
        """Rebalance portfolio toward target allocation."""
        orders = []
        for ticker, target_pct in allocation.items():
            if ticker.upper() == "CASH":
                continue

            target_value = capital * target_pct
            # Get current holding value
            pos = portfolio.get_position(ticker)
            current_value = pos.market_value if pos else 0

            # Get latest price from pre-built price map (P0-1 fix: per-ticker prices)
            try:
                price = self._price_map.get(ticker, 0)
                if price <= 0:
                    price = self.data_provider.get_latest_price(ticker)
                    self._price_map[ticker] = price
            except Exception:
                logger.warning(f"Cannot get price for {ticker}, skipping")
                continue

            diff = target_value - current_value
            if abs(diff) < max(100, capital * 0.02):  # Skip if <2% or <$100
                continue

            if diff > 0:  # Buy
                qty = int(diff / price)
                if qty > 0 and portfolio.cash_balance >= qty * price:
                    result = self.broker.place_order(Order(
                        ticker=ticker, side="buy", quantity=qty,
                        limit_price=price, exchange="",
                    ))
                    orders.append(f"BUY {ticker} x{qty} @ ~${price:.2f}")
            elif diff < 0:  # Sell
                qty = min(int(abs(diff) / price), pos.quantity if pos else 0)
                if qty > 0:
                    result = self.broker.place_order(Order(
                        ticker=ticker, side="sell", quantity=qty,
                        limit_price=price, exchange="",
                    ))
                    orders.append(f"SELL {ticker} x{qty} @ ~${price:.2f}")

        return orders

    def _save_result_json(self, name, regime, signals, allocation, portfolio, orders):
        """Write result JSON for portfolio_summary.py compatibility.

        Matches the format written by old scripts:
          strategy_results/{strategy_name}_result.json
        """
        import json
        from datetime import datetime

        # Map strategy name to file name
        name_map = {
            "quant40": "quant40_result.json",
            "jd_strategy": "jd_strategy_result.json",
            "uranium_vb": "uranium_vb_result.json",
            "korea_etf_momentum": "korea_etf_momentum_result.json",
        }
        filename = name_map.get(name)
        if not filename:
            return

        results_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "strategy_results"
        )
        os.makedirs(results_dir, exist_ok=True)

        # Filter holdings to only THIS strategy's positions
        # Use strategy's asset list + position ownership tracker
        my_assets = set(self.strategy.get_asset_list())

        # Also load position tracker for ownership info (old scripts used this)
        position_owners = {}
        tracker_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "strategy_positions.json"
        )
        try:
            if os.path.exists(tracker_path):
                with open(tracker_path) as f:
                    tracker = __import__("json").load(f)
                for ticker, info in tracker.get("positions", {}).items():
                    position_owners[ticker] = info.get("strategy", "").upper()
        except Exception:
            pass

        strategy_upper = name.upper()
        holdings = []
        for pos in portfolio.positions:
            # Include position if: owned by this strategy in tracker,
            # OR in this strategy's asset list and not owned by another strategy
            owner = position_owners.get(pos.ticker)
            if owner and owner != strategy_upper:
                continue  # belongs to another strategy
            if not owner and pos.ticker not in my_assets:
                continue  # not in our asset list and no ownership record

            price = self._price_map.get(pos.ticker, pos.avg_price)
            holdings.append({
                "ticker": pos.ticker,
                "quantity": pos.quantity,
                "value": pos.market_value,
                "avg_price": pos.avg_price,
                "current_price": price,
                "profit": pos.profit,
                "profit_krw": pos.profit * 1450,
                "profit_rate": (pos.profit / (pos.avg_price * pos.quantity) * 100)
                    if pos.avg_price > 0 and pos.quantity > 0 else 0,
            })

        # Signal summary
        signal_desc = ""
        for t, s in signals.items():
            signal_desc = s.reason
            break

        result = {
            "strategy_name": name.upper(),
            "timestamp": datetime.now().isoformat(),
            "allocation_mode": "fixed" if self.budget_ceiling > 0 else "percentage",
            "capital_allocation": self.budget_ceiling / portfolio.total_value
                if portfolio.total_value > 0 and self.budget_ceiling > 0 else 1.0,
            "allocated_capital": self.budget_ceiling if self.budget_ceiling > 0 else portfolio.total_value,
            "total_value": sum(h["value"] for h in holdings) + portfolio.cash_balance,
            "cash_balance": portfolio.cash_balance,
            "total_profit": sum(h["profit"] for h in holdings),
            "total_profit_krw": sum(h["profit"] for h in holdings) * 1450,
            "holdings": holdings,
            "session_summary": {
                "regime": regime.value,
                "trend_signal": signal_desc,
                "trend_description": signal_desc,
                "market_state": regime.value,
                "orders_executed": len(orders),
                "target_allocation": {k: round(v, 4) for k, v in allocation.items()},
            },
        }

        path = os.path.join(results_dir, filename)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(result, f, indent=2, default=str)
            os.replace(tmp, path)
            logger.info(f"[{name}] Result saved to {path}")
        except Exception as e:
            logger.warning(f"[{name}] Failed to save result: {e}")

    def _notify(self, title, message):
        if self.notifier:
            try:
                self.notifier.send_error(title, message)
            except Exception:
                pass

    def _notify_summary(self, name, regime, signals, allocation, portfolio, orders):
        if not self.notifier:
            return
        try:
            fields = [
                {"name": "Strategy", "value": name, "inline": True},
                {"name": "Regime", "value": regime.value.upper(), "inline": True},
                {"name": "Portfolio", "value": f"${portfolio.total_value:,.2f}", "inline": True},
            ]
            for t, s in signals.items():
                fields.append({"name": f"Signal: {t}", "value": s.reason, "inline": False})
            if orders:
                fields.append({"name": "Orders", "value": "\n".join(orders), "inline": False})
            else:
                fields.append({"name": "Orders", "value": "No rebalancing needed", "inline": False})

            self.notifier._send(title=f"{name} — Daily Report", fields=fields, color=3447003)
        except Exception as e:
            logger.warning(f"Discord notification failed: {e}")
