#!/usr/bin/env python3
"""
Query total KIS account value and compute P/L from deposit history.
Returns account totals for dashboard integration.
"""
import sys, os

TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm"))


def get_kr_account():
    """Query KR holdings + cash from KIS domestic API."""
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "domestic_stock", "inquire_balance"))
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "domestic_stock", "inquire_psbl_order"))

    import kis_auth as ka
    import inquire_balance as kr_bal
    import inquire_psbl_order

    ka.auth(svr="prod")
    acct = ka.getTREnv()

    # Holdings
    output1, output2 = kr_bal.inquire_balance(
        env_dv="real", cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
        afhr_flpr_yn="N", inqr_dvsn="02", unpr_dvsn="01",
        fund_sttl_icld_yn="N", fncg_amt_auto_rdpt_yn="N", prcs_dvsn="00"
    )

    holdings = []
    stock_value = 0
    unrealized_pl = 0
    if output1 is not None and not output1.empty:
        for _, row in output1.iterrows():
            qty = int(row['hldg_qty'])
            if qty > 0:
                val = float(row['evlu_amt'])
                profit = float(row.get('evlu_pfls_amt', 0))
                rate = float(row.get('evlu_pfls_rt', 0))
                stock_value += val
                unrealized_pl += profit
                holdings.append({
                    'ticker': row['pdno'],
                    'quantity': qty,
                    'value': val,
                    'profit': profit,
                    'profit_rate': rate,
                })

    # Cash — use ord_psbl_cash (actual deposit cash, not inflated nrcvb_buy_amt)
    # nrcvb_buy_amt includes unsettled proceeds and margin — too high for asset calculation
    # ord_psbl_cash matches KIS app's 출금가능금액
    df_cash = inquire_psbl_order.inquire_psbl_order(
        env_dv="real", cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
        pdno="005930", ord_unpr="0", ord_dvsn="01",
        cma_evlu_amt_icld_yn="N", ovrs_icld_yn="Y"
    )
    if df_cash is not None and not df_cash.empty:
        row = df_cash.iloc[0]
        cash = float(row.get('ord_psbl_cash', 0))
        # Also get the foreign currency reuse amount (USD cash in KRW terms)
        ovrs_reuse = float(row.get('ovrs_re_use_amt_wcrc', 0))
    else:
        cash = 0
        ovrs_reuse = 0

    # output2 has the real account summary that matches KIS app.
    # We need prvs_rcdl_excc_amt (가수도정산금액 / provisional reconciliation), not
    # dnca_tot_amt, because dnca only reflects already-settled deposit cash —
    # when the bot sells today, those proceeds don't appear in dnca until T+2,
    # so total assets get understated by today's pending KR settlement amount
    # (matches tot_evlu_amt = scts_evlu_amt + prvs_rcdl_excc_amt in KIS app).
    kr_tot_evlu = 0  # tot_evlu_amt = KR stocks + prvs_rcdl_excc_amt (KIS app KR view)
    kr_deposit_cash = 0  # dnca_tot_amt = 예수금 (already-settled deposit only)
    kr_settled_cash = 0  # prvs_rcdl_excc_amt = settled + today's pending settlements
    if output2 is not None and not output2.empty:
        kr_tot_evlu = float(output2.iloc[0].get('tot_evlu_amt', 0))
        kr_deposit_cash = float(output2.iloc[0].get('dnca_tot_amt', 0))
        kr_settled_cash = float(output2.iloc[0].get('prvs_rcdl_excc_amt', kr_deposit_cash))

    return {
        'stock_value': stock_value,
        'cash': kr_settled_cash,       # prvs_rcdl_excc_amt — settled cash incl. today's KR pending
        'deposit_cash': kr_deposit_cash,  # dnca_tot_amt — for display only (KIS app's 예수금)
        'kr_tot_evlu': kr_tot_evlu,    # tot_evlu_amt = KR stocks + prvs_rcdl_excc_amt
        'unrealized_pl': unrealized_pl,
        'holdings': holdings,
        'acct': acct,
    }


def get_us_account(acct):
    """Query US holdings + cash from KIS overseas API."""
    # Fresh imports for overseas modules
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "overseas_stock", "inquire_balance"))
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "overseas_stock", "inquire_psamount"))

    import importlib
    # Force reload to get the overseas version
    if 'inquire_balance' in sys.modules:
        del sys.modules['inquire_balance']
    import inquire_balance as us_bal
    import inquire_psamount

    holdings = {}
    stock_value = 0
    unrealized_pl = 0

    # Query AMEX (most of our ETFs are here)
    try:
        o1, _ = us_bal.inquire_balance(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            ovrs_excg_cd="NASD", tr_crcy_cd="USD", env_dv="real"
        )
        if o1 is not None and not o1.empty:
            for _, row in o1.iterrows():
                qty = int(row['ovrs_cblc_qty'])
                t = row['ovrs_pdno']
                if qty > 0 and t not in holdings:
                    val = float(row.get('ovrs_stck_evlu_amt', 0))
                    avg = float(row.get('frcr_pchs_amt1', 0)) / max(qty, 1)
                    cp = val / qty if qty > 0 else 0
                    pf = (cp - avg) * qty
                    stock_value += val
                    unrealized_pl += pf
                    holdings[t] = {
                        'ticker': t,
                        'quantity': qty,
                        'value': val,
                        'avg_price': avg,
                        'profit': pf,
                        'profit_rate': ((cp / avg) - 1) * 100 if avg > 0 else 0,
                    }
    except Exception as e:
        print(f"US balance error: {e}")

    # Cash
    try:
        df = inquire_psamount.inquire_psamount(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            ovrs_excg_cd="AMEX", item_cd="SPY",
            ovrs_ord_unpr="0", env_dv="real"
        )
        cash = float(df.iloc[0]['ord_psbl_frcr_amt']) if df is not None and not df.empty else 0
    except:
        cash = 0

    # Unsettled US sells — proceeds from US stock sales pending T+3 settlement.
    # These are gone from `stock_value` (positions closed) but haven't landed in
    # `cash` (still settling), so without this line the dashboard underreports
    # total assets by the settlement-in-flight amount. ustl_sll_amt_smtl is
    # already in KRW (KIS converts at the trade-day rate).
    unsettled_sell_krw = 0
    try:
        sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "overseas_stock", "inquire_present_balance"))
        import inquire_present_balance as ipb
        _, _, o3 = ipb.inquire_present_balance(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            wcrc_frcr_dvsn_cd="02", natn_cd="840", tr_mket_cd="00",
            inqr_dvsn_cd="00", env_dv="real"
        )
        if o3 is not None and not o3.empty:
            unsettled_sell_krw = float(o3.iloc[0].get('ustl_sll_amt_smtl', 0) or 0)
    except Exception as e:
        print(f"inquire_present_balance failed (unsettled US sells will be 0): {e}")

    return {
        'stock_value': stock_value,
        'cash': cash,
        'unsettled_sell_krw': unsettled_sell_krw,
        'total': stock_value + cash,
        'unrealized_pl': unrealized_pl,
        'holdings': list(holdings.values()),
    }


def get_account_totals():
    """
    Get complete account totals for KR + US.

    IMPORTANT: KRW 예수금 is SHARED between KR and US accounts.
    KR balance API returns full KRW cash (including US-earmarked portion).
    To avoid double-counting:
      계좌총자산 = KR stocks + US stocks (KRW) + KRW cash (once) + USD cash (KRW, once)
    NOT: KR total + US total (which double-counts KRW cash)
    """
    kr = get_kr_account()
    us = get_us_account(kr['acct'])

    # KRW cash from KR API includes the shared pool — don't add it again from US side
    # USD cash is separate (foreign currency deposit)
    kr_stock_value = kr['stock_value']
    us_stock_value = us['stock_value']
    krw_cash = kr['cash']        # Shared KRW cash (예수금) — count ONCE
    usd_cash = us['cash']        # USD cash in USD

    return {
        'kr': {
            'stock_value': kr_stock_value,
            'unrealized_pl': kr['unrealized_pl'],
            'holdings': kr['holdings'],
            'kr_tot_evlu': kr.get('kr_tot_evlu', kr_stock_value + krw_cash),
            'cash': krw_cash,
            'deposit_cash': kr.get('deposit_cash', krw_cash),  # dnca_tot_amt for display
        },
        'us': {
            'stock_value': us_stock_value,
            'unrealized_pl': us['unrealized_pl'],
            'holdings': us['holdings'],
            'unsettled_sell_krw': us.get('unsettled_sell_krw', 0),
        },
        'krw_cash': krw_cash,                                    # prvs_rcdl_excc_amt — settled cash incl. KR pending
        'usd_cash': usd_cash,                                    # USD cash in USD
        'unsettled_us_sell_krw': us.get('unsettled_sell_krw', 0),  # US T+3 pending sales in KRW
    }


if __name__ == "__main__":
    import json
    totals = get_account_totals()
    print(json.dumps(totals, indent=2, default=str))
