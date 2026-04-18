"""KIS API broker for Korea domestic stock trading.

Wraps the existing open-trading-api modules for KRX domestic market.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import List

from framework.broker import BaseBroker
from framework.types import Order, OrderResult, Position

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for subpath in [
    'open-trading-api/examples_llm',
    'open-trading-api/examples_llm/domestic_stock/inquire_balance',
    'open-trading-api/examples_llm/domestic_stock/inquire_psbl_order',
    'open-trading-api/examples_llm/domestic_stock/order_cash',
    'open-trading-api/examples_llm/domestic_stock/inquire_daily_itemchartprice',
]:
    p = os.path.join(_PROJECT_ROOT, subpath)
    if p not in sys.path:
        sys.path.append(p)


class KISKoreaBroker(BaseBroker):
    """KIS API broker for Korean domestic stocks (KRX)."""

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
        logger.info(f"KIS Korea auth OK: {self._my_acct}")

    def get_positions(self) -> List[Position]:
        if not self._authenticated:
            self.authenticate()
        import inquire_balance
        positions = []
        try:
            output1, _ = inquire_balance.inquire_balance(
                env_dv="real", cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                afhr_flpr_yn="N", inqr_dvsn="02", unpr_dvsn="01",
                fund_sttl_icld_yn="N", fncg_amt_auto_rdpt_yn="N", prcs_dvsn="00"
            )
            if output1 is not None and not output1.empty:
                for _, row in output1.iterrows():
                    qty = int(row['hldg_qty'])
                    if qty <= 0:
                        continue
                    positions.append(Position(
                        ticker=row['pdno'],
                        quantity=qty,
                        avg_price=float(row.get('pchs_avg_pric', 0)),
                        market_value=float(row.get('evlu_amt', 0)),
                        profit=float(row.get('evlu_pfls_amt', 0)),
                    ))
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
        return positions

    def get_cash_balance(self) -> float:
        if not self._authenticated:
            self.authenticate()
        import inquire_psbl_order
        try:
            df = inquire_psbl_order.inquire_psbl_order(
                env_dv="real", cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                pdno="005930", ord_unpr="0", ord_dvsn="01",
                cma_evlu_amt_icld_yn="N", ovrs_icld_yn="Y"
            )
            if df is not None and not df.empty:
                return float(df.iloc[0]['nrcvb_buy_amt'])
        except Exception as e:
            logger.error(f"Failed to get cash: {e}")
        return 0.0

    def place_order(self, order: Order) -> OrderResult:
        if not self._authenticated:
            self.authenticate()
        from domestic_stock.order_cash import order_cash
        try:
            df = order_cash(
                env_dv="real", ord_dv=order.side,
                cano=self._my_acct, acnt_prdt_cd=self._my_prod,
                pdno=order.ticker, ord_dvsn="01",  # market order
                ord_qty=str(order.quantity), ord_unpr="0",
                excg_id_dvsn_cd="KRX"
            )
            logger.info(f"Order {order.side} {order.ticker} x{order.quantity}: {df}")
            return OrderResult(True, order.ticker, order.side, order.quantity)
        except Exception as e:
            logger.error(f"Order failed: {e}")
            return OrderResult(False, order.ticker, order.side, 0, message=str(e))
