#!/usr/bin/env python3
"""Refresh realized P/L per strategy from KIS transaction history."""
import sys, os, json
from datetime import datetime

TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm"))

TICKER_TO_STRATEGY = {
    'SPY': 'Quant40', 'IEF': 'Quant40', 'SH': 'Quant40',
    'NVDA': 'JD Strategy', 'AAPL': 'JD Strategy', 'MSFT': 'JD Strategy',
    'GOOGL': 'JD Strategy', 'AMZN': 'JD Strategy',
    'EFA': 'Modified Dual Momentum', 'AGG': 'Modified Dual Momentum',
    'SHY': 'Modified Dual Momentum', 'TLT': 'Modified Dual Momentum',
    'TIP': 'Modified Dual Momentum', 'LQD': 'Modified Dual Momentum',
    'HYG': 'Modified Dual Momentum', 'BWX': 'Modified Dual Momentum',
    'EMB': 'Modified Dual Momentum',
    'GLD': 'Hybrid VB (US)', 'SLV': 'Hybrid VB (US)', 'GLTR': 'Hybrid VB (US)',
    'GDX': 'Hybrid VB (US)', 'USO': 'Hybrid VB (US)', 'URA': 'Hybrid VB (US)',
    'FTGC': 'Hybrid VB (US)', 'CPER': 'Hybrid VB (US)', 'DBA': 'Hybrid VB (US)',
    'SIL': 'Hybrid VB (US)',
    '139220': 'Korea ETF Momentum', '144600': 'Korea ETF Momentum',
    '132030': 'Korea ETF Momentum',
    '069500': 'Hybrid VB (KR)', '229200': 'Hybrid VB (KR)',
    '305720': 'Hybrid VB (KR)', '091170': 'Hybrid VB (KR)',
    '364690': 'Hybrid VB (KR)',
}


def refresh():
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "domestic_stock", "inquire_period_trade_profit"))
    sys.path.insert(0, os.path.join(TRADING_DIR, "open-trading-api", "examples_llm", "overseas_stock", "inquire_period_profit"))

    import kis_auth as ka
    ka.auth(svr="prod")
    acct = ka.getTREnv()

    today = datetime.now().strftime("%Y%m%d")
    strategy_realized = {}

    # Domestic
    try:
        import inquire_period_trade_profit as ipt
        df1, _ = ipt.inquire_period_trade_profit(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            sort_dvsn="00", inqr_strt_dt="20260101", inqr_end_dt=today, cblc_dvsn="00",
        )
        if df1 is not None and not df1.empty:
            for _, row in df1.iterrows():
                t = row.get("pdno", "")
                pnl = float(row.get("rlzt_pfls", 0))
                strat = TICKER_TO_STRATEGY.get(t, "Unknown")
                strategy_realized[strat] = strategy_realized.get(strat, 0) + pnl
    except Exception as e:
        print(f"Domestic error: {e}")

    # Overseas
    try:
        import inquire_period_profit as opp
        result = opp.inquire_period_profit(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            ovrs_excg_cd="NASD", natn_cd="840", crcy_cd="USD", pdno="",
            inqr_strt_dt="20260101", inqr_end_dt=today,
            wcrc_frcr_dvsn_cd="01", FK200="", NK200="",
        )
        if result and isinstance(result, tuple) and result[0] is not None and not result[0].empty:
            for _, row in result[0].iterrows():
                t = row.get("ovrs_pdno", "")
                pnl_usd = float(row.get("ovrs_rlzt_pfls_amt", 0))
                fx = float(row.get("frst_bltn_exrt", 1450))
                pnl_krw = pnl_usd * fx
                strat = TICKER_TO_STRATEGY.get(t, "Unknown")
                strategy_realized[strat] = strategy_realized.get(strat, 0) + pnl_krw
    except Exception as e:
        print(f"Overseas error: {e}")

    # Round and add total
    output = {k: round(v) for k, v in strategy_realized.items()}
    output["_total"] = sum(output.values())

    out_path = os.path.join(TRADING_DIR, "strategy_results", "realized_pl_2026.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    return output


if __name__ == "__main__":
    result = refresh()
    print(json.dumps(result, indent=2))
