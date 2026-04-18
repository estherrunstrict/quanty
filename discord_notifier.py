#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord Notification System
============================
Clean, minimal Discord notifications for trading bots.
Uses description-based embeds over field grids for readability.
"""

import requests
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Discord notification handler — description-first, minimal emoji."""

    # Sidebar colors (Discord embed accent)
    COLORS = {
        'info':       0x4a78a8,   # Steel blue
        'success':    0x3a8a5a,   # Muted green
        'warning':    0xc9a24d,   # Amber
        'error':      0xa84848,   # Muted red
        'trade_buy':  0x3a8a5a,
        'trade_sell': 0xa84848,
        'neutral':    0x7a68a8,   # Muted purple
    }

    def __init__(self, webhook_url: str, bot_name: str = "Trading Bot"):
        self.webhook_url = webhook_url
        self.bot_name = bot_name

    # ── Core sender ──────────────────────────────────────────

    def _send(self, title: str, fields: List[Dict] = None, color: int = None,
              footer: str = None, description: str = None, url: str = None):
        """Send a Discord embed. Prefer `description` for body text, `fields` for tabular data."""
        if not self.webhook_url:
            logger.warning("Discord webhook URL not configured")
            return

        embed = {
            "title": title,
            "color": color or self.COLORS['info'],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": footer or self.bot_name},
        }
        if description:
            embed["description"] = description
        if fields:
            embed["fields"] = fields
        if url:
            embed["url"] = url

        try:
            resp = requests.post(self.webhook_url, json={"embeds": [embed]}, timeout=10)
            resp.raise_for_status()
            logger.debug(f"Discord sent: {title}")
        except Exception as e:
            logger.error(f"Discord failed: {e}")

    # ── Formatting helpers ───────────────────────────────────

    @staticmethod
    def _fv(value, currency='USD'):
        """Format a monetary value with correct currency symbol."""
        if currency == 'KRW':
            return f"₩{round(value):,}"
        return f"${value:,.2f}"

    @staticmethod
    def _fpnl(value, currency='USD'):
        """Format P&L with +/- sign."""
        if currency == 'KRW':
            return f"₩{round(value):+,}"
        return f"${value:+,.2f}"

    @staticmethod
    def _pct(value):
        return f"{value:+.1f}%"

    @staticmethod
    def _dot(value):
        """Green/red dot based on value sign."""
        return "+" if value >= 0 else "-"

    # ── Startup / Shutdown ───────────────────────────────────

    def send_startup(self, strategy_name: str, config: Dict[str, Any]):
        mode = config.get('mode', 'Live')
        lines = [f"**{strategy_name}** started in **{mode}** mode."]
        if 'portfolio_value' in config:
            lines.append(f"Portfolio: ${config['portfolio_value']:,.2f}")
        if 'allocation' in config:
            lines.append(f"Allocation: {config['allocation']}")
        if 'state' in config:
            lines.append(f"State: {config['state'].upper()}")

        self._send(
            title=f"{strategy_name} — Session Started",
            description="\n".join(lines),
            color=self.COLORS['success'],
        )

    def send_shutdown(self, summary: Dict[str, Any] = None):
        lines = ["Session complete."]
        if summary:
            if 'trades_executed' in summary:
                lines.append(f"Trades: {summary['trades_executed']}")
            if 'portfolio_value' in summary:
                lines.append(f"Portfolio: ${summary['portfolio_value']:,.2f}")

        self._send(
            title=f"{self.bot_name} — Session Complete",
            description="\n".join(lines),
            color=self.COLORS['info'],
        )

    # ── Trade Execution ──────────────────────────────────────

    def send_trade_execution(self, trades: List[Dict[str, Any]]):
        """Send trade summary. Respects 'currency' key in trade dicts."""
        if not trades:
            return

        currency = trades[0].get('currency', 'USD')
        buys = [t for t in trades if t['action'].upper() == 'BUY']
        sells = [t for t in trades if t['action'].upper() == 'SELL']

        lines = []
        if buys:
            lines.append("**BUY**")
            total = 0
            for t in buys:
                lines.append(f"  {t['ticker']}  {t.get('quantity',0)} × {self._fv(t['price'], currency)} = {self._fv(t['value'], currency)}")
                total += t['value']
            lines.append(f"  Total: {self._fv(total, currency)}")
            lines.append("")

        if sells:
            lines.append("**SELL**")
            total = 0
            for t in sells:
                lines.append(f"  {t['ticker']}  {t.get('quantity',0)} × {self._fv(t['price'], currency)} = {self._fv(t['value'], currency)}")
                total += t['value']
            lines.append(f"  Total: {self._fv(total, currency)}")

        reason = trades[0].get('reason', '')
        if reason:
            lines.append(f"\n_{reason}_")

        self._send(
            title=f"Trade Executed — {len(trades)} order{'s' if len(trades)>1 else ''}",
            description="\n".join(lines),
            color=self.COLORS['trade_buy'] if buys and not sells else self.COLORS['trade_sell'],
        )

    # ── Combined Portfolio Report (US strategies) ────────────

    def send_combined_portfolio_report(self, report: Dict[str, Any]):
        """Single embed for all US strategies. Description-based, not field-grid."""
        total_value = report.get('total_value', 0)
        total_profit = report.get('total_profit', 0)
        total_profit_pct = report.get('total_profit_pct', 0)
        total_cash = report.get('total_cash', 0)

        pnl_sign = "+" if total_profit >= 0 else ""

        lines = [
            f"**${total_value:,.0f}**  |  P/L: **${total_profit:+,.0f}** ({total_profit_pct:+.1f}%)  |  Cash: ${total_cash:,.0f}",
            "",
        ]

        for s in report.get('strategies', []):
            stale = " *(stale)*" if s.get('is_stale') else ""
            p = s.get('profit', 0)
            pp = s.get('profit_pct', 0)
            dot = "▲" if p >= 0 else "▼"
            signal = s.get('market_signal', '')
            action = s.get('action_summary', 'HOLD')

            # Holdings inline
            hparts = [f"{h['ticker']}×{h['quantity']}" for h in s.get('holdings', [])[:4] if h.get('quantity', 0) > 0]
            hold_str = "  ".join(hparts) if hparts else "No positions"

            lines.append(f"**{s['name']}**{stale}")
            lines.append(f"  ${s.get('budget',0):,.0f} → **${s.get('total_value',0):,.0f}**  {dot} ${p:+,.0f} ({pp:+.1f}%)")
            lines.append(f"  {hold_str}  |  {action}")
            if signal:
                lines.append(f"  Signal: {signal}")
            lines.append("")

        # Holdings detail
        all_h = report.get('all_holdings', [])
        if all_h:
            lines.append("**Holdings**")
            for h in all_h[:8]:
                dot = "▲" if h.get('profit', 0) >= 0 else "▼"
                pct = f" ({h['profit_rate']:+.1f}%)" if h.get('profit_rate') else ""
                lines.append(f"  {dot} {h['ticker']}  {h['quantity']} × ${h['current_price']:.2f} = ${h['value']:,.0f}  {self._fpnl(h.get('profit',0))}{pct}")
            if len(all_h) > 8:
                lines.append(f"  _+{len(all_h)-8} more_")

        total_trades = sum(s.get('trades_today', 0) for s in report.get('strategies', []))

        if total_profit_pct >= 0.5:
            color = self.COLORS['success']
        elif total_profit_pct <= -2.0:
            color = self.COLORS['error']
        else:
            color = self.COLORS['info']

        now_et = datetime.now(ZoneInfo('America/New_York'))
        self._send(
            title=f"US Portfolio — {now_et.strftime('%Y-%m-%d')}",
            description="\n".join(lines),
            color=color,
            footer=f"{total_trades} trades | {report.get('timestamp', now_et.strftime('%H:%M'))} ET",
        )

    # ── Daily Summary ────────────────────────────────────────

    def send_daily_summary(self, summary: Dict[str, Any]):
        pv = summary.get('portfolio_value', 0)
        pc = summary.get('portfolio_change', 0)
        pcp = summary.get('portfolio_change_pct', 0)
        dot = "▲" if pc >= 0 else "▼"

        lines = [
            f"**${pv:,.2f}**  {dot} ${pc:+,.2f} ({pcp:+.2f}%)",
            f"Cash: ${summary.get('cash_usd',0):,.2f}",
        ]

        # Holdings
        for h in summary.get('holdings', [])[:5]:
            hdot = "▲" if h.get('profit', 0) >= 0 else "▼"
            pct = f" ({h['profit_rate']:+.1f}%)" if 'profit_rate' in h else ""
            lines.append(f"  {hdot} **{h['ticker']}** {h['quantity']} × ${h['current_price']:.2f} = ${h['value']:,.2f}{pct}")

        # State / regime
        parts = []
        if 'state' in summary:
            parts.append(f"State: {summary['state'].upper()}")
        if 'regime' in summary:
            parts.append(f"Regime: {summary['regime'].upper()}")
        trades = summary.get('trades_executed', 0)
        parts.append(f"Trades: {trades}")
        if parts:
            lines.append("\n" + "  |  ".join(parts))

        if 'next_action' in summary:
            lines.append(f"\n_{summary['next_action']}_")

        self._send(
            title=f"Daily Summary — {datetime.now().strftime('%Y-%m-%d')}",
            description="\n".join(lines),
            color=self.COLORS['info'],
        )

    # ── Account Overview ─────────────────────────────────────

    def send_account_overview(self, account_data: Dict[str, Any]):
        tv = account_data['total_account_value']
        tp = account_data.get('total_profit', 0)
        tpp = account_data.get('total_profit_pct', 0)
        dot = "▲" if tp >= 0 else "▼"

        lines = [
            f"**${tv:,.2f}**  {dot} ${tp:+,.2f} ({tpp:+.1f}%)",
            f"Cash: ${account_data.get('total_cash_usd', 0):,.2f}",
        ]

        if 'strategies' in account_data:
            lines.append("")
            for s in account_data['strategies']:
                lines.append(f"  **{s['name']}**: ${s['allocated']:,.0f} ({s['percentage']:.1f}%)")

        if 'all_holdings' in account_data:
            lines.append("")
            for h in account_data['all_holdings'][:8]:
                lines.append(f"  **{h['ticker']}** {h['quantity']} × ${h['current_price']:.2f} = ${h['value']:,.2f}")

        self._send(
            title="Account Overview",
            description="\n".join(lines),
            color=self.COLORS['info'],
        )

    def send_account_daily_summary(self, account_data: Dict[str, Any]):
        tv = account_data['total_account_value']
        tc = account_data.get('total_change', 0)
        tcp = account_data.get('total_change_pct', 0)
        dot = "▲" if tc >= 0 else "▼"

        lines = [
            f"**${tv:,.2f}**  {dot} ${tc:+,.2f} ({tcp:+.2f}%) today",
            f"Cash: USD ${account_data.get('cash_usd',0):,.2f}",
        ]

        if account_data.get('cash_krw', 0) > 0:
            lines[1] += f"  |  KRW ₩{account_data['cash_krw']:,.0f}"

        if 'strategies' in account_data:
            lines.append("")
            for s in account_data['strategies']:
                sdot = "▲" if s.get('change', 0) >= 0 else "▼"
                lines.append(f"  {sdot} **{s['name']}**: ${s['value']:,.2f}  {s.get('change',0):+,.2f} ({s.get('change_pct',0):+.2f}%)  Trades: {s.get('trades',0)}")

        if 'all_holdings' in account_data and account_data['all_holdings']:
            lines.append("")
            for h in account_data['all_holdings'][:8]:
                pct = (h['value'] / tv * 100) if tv > 0 else 0
                lines.append(f"  **{h['ticker']}**: ${h['value']:,.2f} ({pct:.1f}%)")

        total_trades = account_data.get('total_trades', 0)

        self._send(
            title=f"Account Daily Summary — {datetime.now().strftime('%Y-%m-%d')}",
            description="\n".join(lines),
            color=self.COLORS['info'],
            footer=f"{total_trades} trades today",
        )

    # ── State Change ─────────────────────────────────────────

    def send_state_change(self, old_state: str, new_state: str, reason: str, details: Dict = None):
        lines = [
            f"**{old_state.upper()}** → **{new_state.upper()}**",
            reason,
        ]
        if details:
            for k, v in details.items():
                lines.append(f"  {k}: {v}")

        color = self.COLORS['info']
        if new_state.upper() == 'ALERT':
            color = self.COLORS['warning']
        elif new_state.upper() == 'PANIC':
            color = self.COLORS['error']
        elif new_state.upper() == 'NORMAL':
            color = self.COLORS['success']

        self._send(
            title="State Change",
            description="\n".join(lines),
            color=color,
        )

    # ── Error / Alert ────────────────────────────────────────

    def send_error(self, error_msg: str, details: str = None):
        desc = error_msg
        if details:
            desc += f"\n```\n{details[:800]}\n```"
        self._send(
            title="Error",
            description=desc,
            color=self.COLORS['error'],
        )

    def send_alert(self, title: str, message: str, severity: str = 'info'):
        self._send(
            title=title,
            description=message,
            color=self.COLORS.get(severity, self.COLORS['info']),
        )


# ══════════════════════════════════════════════════════════════
# Upbit (KRW) Notifier
# ══════════════════════════════════════════════════════════════

class UpbitNotifier(DiscordNotifier):
    """Upbit crypto notifier — KRW formatting."""

    def send_trade_execution(self, trades: List[Dict[str, Any]]):
        if not trades:
            return

        buys = [t for t in trades if t['action'].upper() == 'BUY']
        sells = [t for t in trades if t['action'].upper() == 'SELL']
        lines = []

        if buys:
            lines.append("**BUY**")
            total = 0
            for t in buys:
                lines.append(f"  {t['ticker']}  {t.get('quantity',0):.6f} × ₩{t['price']:,.0f} = ₩{t['value']:,.0f}")
                total += t['value']
            lines.append(f"  Total: ₩{total:,.0f}")
            lines.append("")

        if sells:
            lines.append("**SELL**")
            total = 0
            for t in sells:
                lines.append(f"  {t['ticker']}  {t.get('quantity',0):.6f} × ₩{t['price']:,.0f} = ₩{t['value']:,.0f}")
                total += t['value']
            lines.append(f"  Total: ₩{total:,.0f}")

        reason = trades[0].get('reason', '')
        if reason:
            lines.append(f"\n_{reason}_")

        self._send(
            title=f"Trade Executed — {len(trades)} order{'s' if len(trades)>1 else ''}",
            description="\n".join(lines),
            color=self.COLORS['trade_buy'] if buys and not sells else self.COLORS['trade_sell'],
        )

    def send_upbit_summary(self, summary: Dict[str, Any]):
        pv = summary.get('portfolio_value_krw', 0)
        pc = summary.get('portfolio_change_krw', 0)
        pcp = summary.get('portfolio_change_pct', 0)
        dot = "▲" if pc >= 0 else "▼"

        lines = [
            f"**₩{pv:,.0f}**  {dot} ₩{pc:+,.0f} ({pcp:+.2f}%)",
        ]

        # Asset
        asset = summary.get('asset', 'BTC')
        amt = summary.get('asset_amount', 0)
        av = summary.get('asset_value_krw', 0)
        cash = summary.get('cash_krw', 0)
        asset_pct = (av / pv * 100) if pv > 0 else 0

        lines.append(f"{asset}: {amt:.6f} (₩{av:,.0f}, {asset_pct:.0f}%)  |  Cash: ₩{cash:,.0f}")

        # State / regime
        state = summary.get('state', 'NORMAL')
        regime = summary.get('regime', 'NEUTRAL')
        lines.append(f"State: **{state}**  |  Regime: **{regime}**")

        # Price tracking
        cp = summary.get('current_price', 0)
        ath = summary.get('ath_price', 0)
        dd = summary.get('drawdown_pct', 0)
        if ath > 0:
            lines.append(f"Price: ₩{cp:,.0f}  |  ATH: ₩{ath:,.0f}  |  DD: {dd:.1f}%")

        # Technical
        parts = []
        if 'sma_50' in summary:
            parts.append(f"SMA50: ₩{summary['sma_50']:,.0f}")
        if 'rsi' in summary:
            parts.append(f"RSI: {summary['rsi']:.0f}")
        if 'volatility' in summary:
            parts.append(f"Vol: {summary['volatility']*100:.1f}%")
        if parts:
            lines.append("  ".join(parts))

        if 'next_action' in summary:
            lines.append(f"\n_{summary['next_action']}_")

        self._send(
            title=f"Upbit Summary — {datetime.now().strftime('%Y-%m-%d')}",
            description="\n".join(lines),
            color=self.COLORS['info'],
            footer=f"{summary.get('trades_count', 0)} trades | {self.bot_name}",
        )


# ══════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════

def get_notifier(webhook_url: str, bot_type: str = "general") -> DiscordNotifier:
    bot_names = {
        'upbit': 'Upbit BTC',
        'upbit_vb': 'BTC Volatility Breakout',
        'usa_stock': 'US Quant40',
        'jd_strategy': 'US JD Strategy',
        'korea_etf': 'Korea ETF Momentum',
        'general': 'Trading Bot',
    }
    name = bot_names.get(bot_type, 'Trading Bot')

    if bot_type in ('upbit', 'upbit_vb'):
        return UpbitNotifier(webhook_url, name)
    return DiscordNotifier(webhook_url, name)
