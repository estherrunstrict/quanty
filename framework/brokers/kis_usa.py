"""KIS API broker for US overseas stock trading.

Wraps the existing open-trading-api modules for:
  - Authentication (kis_auth)
  - Order placement (overseas_stock/order)
  - Balance inquiry (overseas_stock/inquire_balance)
  - Cash inquiry (overseas_stock/inquire_psamount)
"""
from __future__ import annotations

import logging
import os
import sys
from typing import List

from framework.broker import BaseBroker
from framework.types import Order, OrderResult, Position

logger = logging.getLogger(__name__)

# Add KIS API paths
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for subpath in [
    'open-trading-api/examples_llm',
    'open-trading-api/examples_llm/overseas_stock/inquire_balance',
    'open-trading-api/examples_llm/overseas_stock/inquire_psamount',
    'open-trading-api/examples_llm/overseas_stock/order',
    'open-trading-api/examples_llm/overseas_stock/inquire_daily_chartprice',
    'open-trading-api/examples_llm/domestic_stock/inquire_account_balance',
]:
    p = os.path.join(_PROJECT_ROOT, subpath)
    if p not in sys.path:
        sys.path.append(p)


EXCHANGE_MAP = {
    "SPY": "AMEX", "IEF": "NASD", "SH": "AMEX",
    "AAPL": "NASD", "MSFT": "NASD", "GOOGL": "NASD", "NVDA": "NASD", "AMZN": "NASD",
    "URA": "AMEX", "URNM": "AMEX",
}


class KISUSABroker(BaseBroker):
    """KIS API broker for US stocks."""

    def __init__(self, secrets: dict):
        self._secrets = secrets
        self._my_acct = None
        self._my_prod = None
        self._authenticated = False

    def authenticate(self):
        import kis_auth as ka
        ka.auth(svr="prod")
        acct = ka.getTREnv()
        self._my_acct = acct.my_acct
        self._my_prod = acct.my_prod
        self._authenticated = True
        logger.info(f"KIS USA auth OK: {self._my_acct}")

    def get_positions(self) -> List[Position]:
        """Query positions across ALL exchanges (NASD + AMEX + NYSE)."""
        if not self._authenticated:
            self.authenticate()
        import inquire_balance
        positions = []
        seen_tickers = set()
        # P0-2 fix: query all exchanges, not just NASD
        for excg in ["NASD", "AMEX", "NYSE"]:
            try:
                output1, _ = inquire_balance.inquire_balance(
                    cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                    ovrs_excg_cd=excg, tr_crcy_cd="USD", env_dv="real"
                )
                if output1 is not None and not output1.empty:
                    for _, row in output1.iterrows():
                        ticker = row['ovrs_pdno']
                        if ticker in seen_tickers:
                            continue
                        qty = int(row['ovrs_cblc_qty'])
                        if qty <= 0:
                            continue
                        seen_tickers.add(ticker)
                        purchase_amt = float(row.get('frcr_pchs_amt1', 0))
                        avg_price = purchase_amt / qty if qty > 0 else 0
                        market_value = float(row.get('ovrs_stck_evlu_amt', 0))
                        profit = float(row.get('frcr_evlu_pfls_amt', 0))
                        positions.append(Position(
                            ticker=ticker, quantity=qty, avg_price=avg_price,
                            market_value=market_value, profit=profit,
                        ))
            except Exception as e:
                logger.warning(f"Balance query failed for {excg}: {e}")
        return positions

    def get_cash_balance(self) -> float:
        """Get available USD cash. Uses domestic account balance API for accuracy."""
        if not self._authenticated:
            self.authenticate()
        # Use domestic account balance API (same approach as old scripts)
        try:
            import inquire_account_balance as iab
            _, df_cash = iab.inquire_account_balance(
                cano=self._my_acct, acnt_prdt_cd=self._my_prod,
            )
            if df_cash is not None and not df_cash.empty:
                return float(df_cash.iloc[0].get('dncl_amt', 0))
        except Exception as e1:
            logger.warning(f"Account balance API failed: {e1}, falling back to psamount")

        # Fallback: inquire_psamount
        try:
            import inquire_psamount
            df = inquire_psamount.inquire_psamount(
                cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                ovrs_excg_cd="NASD", item_cd="AAPL",
                ovrs_ord_unpr="0", env_dv="real"
            )
            if df is not None and not df.empty:
                return float(df.iloc[0]['ord_psbl_frcr_amt'])
        except Exception as e2:
            logger.error(f"Cash balance query failed: {e2}")
        return 0.0

    def place_order(self, order: Order) -> OrderResult:
        if not self._authenticated:
            self.authenticate()
        import order as kis_order
        try:
            exchange = order.exchange or EXCHANGE_MAP.get(order.ticker, "AMEX")
            # Use limit price at 1% above/below market for fills
            price_str = str(round(order.limit_price * (1.01 if order.side == "buy" else 0.99), 2)) if order.limit_price else "0"

            df = kis_order.order(
                cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                ovrs_excg_cd=exchange, pdno=order.ticker,
                ord_qty=str(order.quantity), ovrs_ord_unpr=price_str,
                ord_dv=order.side, ctac_tlno="", mgco_aptm_odno="",
                ord_svr_dvsn_cd="0", ord_dvsn="00", env_dv="real"
            )
            logger.info(f"Order {order.side} {order.ticker} x{order.quantity}: {df}")
            return OrderResult(True, order.ticker, order.side, order.quantity,
                               filled_price=order.limit_price or 0)
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return OrderResult(False, order.ticker, order.side, 0, message=str(e))
