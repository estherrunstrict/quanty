# -*- coding: utf-8 -*-
import json
import os
import sys
import yaml
import logging
from datetime import datetime, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from discord_notifier import DiscordNotifier

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    stream_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

STALE_THRESHOLD_HOURS = 6

STRATEGY_DISPLAY = {
    'QUANT40': 'QUANT40',
    'JD_STRATEGY': 'JD Strategy',
    'URANIUM_VB': 'Uranium VB',
    'CLAUDE_AI_BOT': 'Claude AI Bot',
}


class PortfolioSummary:

    def __init__(self):
        self.project_root = os.path.abspath(os.path.dirname(__file__))
        self.results_dir = os.path.join(self.project_root, 'strategy_results')

        self.config = self._load_config()
        webhook_url = self.config.get("DISCORD_WEBHOOK_URL")
        self.notifier = DiscordNotifier(webhook_url, bot_name="Portfolio Manager")

        self.strategy_files = {
            'QUANT40': os.path.join(self.results_dir, 'quant40_result.json'),
            'JD_STRATEGY': os.path.join(self.results_dir, 'jd_strategy_result.json'),
            'URANIUM_VB': os.path.join(self.results_dir, 'uranium_vb_result.json'),
            'CLAUDE_AI_BOT': os.path.join(self.results_dir, 'claude_trading_bot_result.json'),
        }

        cap_mgmt = self.config.get('capital_management', {})
        self.budgets = {
            'QUANT40': cap_mgmt.get('quant40', {}).get('budget_usd', 0),
            'JD_STRATEGY': cap_mgmt.get('jd_strategy', {}).get('budget_usd', 0),
            'URANIUM_VB': cap_mgmt.get('uranium_vb', {}).get('budget_usd', 0),
            'CLAUDE_AI_BOT': cap_mgmt.get('claude_trading_bot', {}).get('budget_usd', 0),
        }

    def _load_config(self):
        try:
            with open(os.path.join(self.project_root, 'config.yaml'), 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("config.yaml file not found")
            sys.exit(1)
        except yaml.YAMLError as e:
            logger.error(f"config.yaml parsing error: {e}")
            sys.exit(1)

    def load_strategy_results(self) -> Dict[str, Optional[Dict]]:
        results = {}
        for strategy_name, file_path in self.strategy_files.items():
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r') as f:
                        data = json.load(f)
                        results[strategy_name] = data
                        logger.info(f"Loaded {strategy_name} results from {file_path}")
                except Exception as e:
                    logger.error(f"Failed to load {strategy_name} results: {e}")
                    results[strategy_name] = None
            else:
                logger.warning(f"{strategy_name} result file not found: {file_path}")
                results[strategy_name] = None
        return results

    def _check_stale(self, result: Dict) -> tuple:
        ts_str = result.get('timestamp', '')
        if not ts_str:
            return True, 999.0
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            return age_hours > STALE_THRESHOLD_HOURS, age_hours
        except Exception:
            return True, 999.0

    def _build_market_signal(self, strategy_name: str, result: Dict) -> str:
        session = result.get('session_summary', {})

        if strategy_name == 'QUANT40':
            desc = session.get('trend_description', '')
            if desc:
                return desc
            signal = session.get('trend_signal', None)
            if signal is not None:
                return f"Signal: {signal}"
            return "Monitoring"

        if strategy_name == 'JD_STRATEGY':
            state = session.get('market_state', 'unknown').upper()
            regime = session.get('regime', 'unknown').upper()
            state_emojis = {'NORMAL': '🟢', 'ALERT': '🟡', 'PANIC': '🔴'}
            return f"{state_emojis.get(state, '⚪')} {state} | {regime}"

        if strategy_name == 'URANIUM_VB':
            regime_map = session.get('regime', {})
            if isinstance(regime_map, dict):
                parts = [f"{t}: {r}" for t, r in regime_map.items()]
                return " | ".join(parts) if parts else "Monitoring"
            return str(regime_map)

        return "—"

    def _build_action_summary(self, strategy_name: str, result: Dict) -> str:
        session = result.get('session_summary', {})
        trades = session.get('orders_executed', 0)
        if trades == 0:
            return "HOLD"
        return f"{trades} trade{'s' if trades > 1 else ''} executed"

    def send_integrated_report(self):
        strategy_results = self.load_strategy_results()

        if not any(strategy_results.values()):
            logger.warning("No strategy results found to report")
            self.notifier.send_alert(
                "Portfolio Summary - No Data",
                "No strategy results found to report",
                severity='warning'
            )
            return

        total_stock_value = 0.0
        total_cash = 0.0
        total_profit = 0.0
        all_holdings = []
        strategies_report = []

        seen_cash = False

        for strategy_name, result in strategy_results.items():
            if result is None:
                display_name = STRATEGY_DISPLAY.get(strategy_name, strategy_name)
                budget = self.budgets.get(strategy_name, 0)
                strategies_report.append({
                    'name': display_name,
                    'total_value': 0,
                    'budget': budget,
                    'profit': 0,
                    'profit_pct': 0,
                    'holdings': [],
                    'trades_today': 0,
                    'market_signal': '⚠️ NO DATA',
                    'action_summary': 'Result file missing',
                    'is_stale': True,
                    'stale_hours': 999,
                })
                continue

            s_holdings = result.get('holdings', [])
            # Use sum of holdings values (stock value only) — NOT result['total_value']
            # which includes the full shared account cash and causes double-counting.
            s_stock_value = sum(h.get('value', 0) for h in s_holdings)
            s_profit = result.get('total_profit', 0)
            budget = self.budgets.get(strategy_name, 0)
            display_name = STRATEGY_DISPLAY.get(strategy_name, strategy_name)

            is_stale, stale_hours = self._check_stale(result)

            invested_capital = s_stock_value - s_profit if s_stock_value > s_profit else s_stock_value
            profit_pct = (s_profit / invested_capital * 100) if invested_capital > 0 else 0

            total_stock_value += s_stock_value
            total_profit += s_profit
            all_holdings.extend(s_holdings)

            # Cash is shared across strategies — take it exactly once from the first result
            if not seen_cash:
                total_cash = result.get('cash_balance', 0)
                seen_cash = True

            strategies_report.append({
                'name': display_name,
                'total_value': s_stock_value,
                'budget': budget,
                'profit': s_profit,
                'profit_pct': profit_pct,
                'holdings': s_holdings,
                'trades_today': result.get('session_summary', {}).get('orders_executed', 0),
                'market_signal': self._build_market_signal(strategy_name, result),
                'action_summary': self._build_action_summary(strategy_name, result),
                'is_stale': is_stale,
                'stale_hours': stale_hours,
            })

        total_value = total_stock_value + total_cash
        invested_total = total_value - total_profit
        total_profit_pct = (total_profit / invested_total * 100) if invested_total > 0 else 0

        now_et = datetime.now(ZoneInfo('America/New_York'))

        report = {
            'total_value': total_value,
            'total_cash': total_cash,
            'total_profit': total_profit,
            'total_profit_pct': total_profit_pct,
            'strategies': strategies_report,
            'all_holdings': all_holdings,
            'timestamp': now_et.strftime('%H:%M'),
        }

        self.notifier.send_combined_portfolio_report(report)


def send_portfolio_summary():
    try:
        aggregator = PortfolioSummary()
        aggregator.send_integrated_report()
        logger.info("Portfolio summary sent successfully")
        return True
    except Exception as e:
        logger.error(f"Portfolio summary failed: {e}", exc_info=True)
        return False


def main():
    logger.info("=" * 70)
    logger.info("=== Portfolio Summary Aggregator ===")
    logger.info("=" * 70)

    if send_portfolio_summary():
        logger.info("=== Portfolio Summary Complete ===")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
