#!/usr/bin/env python3
"""Current market state report + strategy signals."""
import sys, os, warnings
warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.data.yfinance_provider import YFinanceProvider
from framework.data.cache import CachedProvider
from framework.regime.detector import RegimeDetector
from strategies.quant40 import Quant40Strategy, compute_trend_signal
from framework.config.loader import load_config
from datetime import date
import pandas as pd

provider = CachedProvider(YFinanceProvider(), cache_dir="data_cache")

# SPY data
spy = provider.get_historical_data("SPY", date(2024, 1, 1), date(2026, 3, 25))
print("=" * 60)
print("CURRENT MARKET STATE (as of 2026-03-25)")
print("=" * 60)
close = spy["Close"].astype(float)
print(f"SPY Close:   ${close.iloc[-1]:.2f}")
print(f"SPY 50 SMA:  ${close.rolling(50).mean().iloc[-1]:.2f}")
print(f"SPY 200 SMA: ${close.rolling(200).mean().iloc[-1]:.2f}")

det = RegimeDetector(market_name="US")
regime = det.detect(spy)
print(f"Market Regime: {regime.value.upper()}")

prices = spy["AdjClose"].astype(float) if "AdjClose" in spy.columns else close
signal, score = compute_trend_signal(prices)
print(f"Trend Score: {score}/4 -> Signal: {signal}")
sig_map = {1.0: "100% SPY", 0.5: "80% SPY", 0.0: "60% SPY", -0.5: "60% SPY + 20% SH", -1.0: "60% SPY + 40% SH"}
print(f"Quant40 Allocation: {sig_map.get(signal, 'unknown')}")

print()
print("SPY RECENT PERFORMANCE")
print("-" * 40)
for label, d in [("1 week", 5), ("1 month", 21), ("3 months", 63)]:
    if d < len(close):
        ret = (close.iloc[-1] / close.iloc[-d] - 1) * 100
        print(f"  {label:12s}: {ret:+.1f}%")
ytd = spy[spy.index >= pd.Timestamp("2026-01-01")]
if len(ytd) > 1:
    ret = (close.iloc[-1] / ytd["Close"].astype(float).iloc[0] - 1) * 100
    print(f"  {'YTD':12s}: {ret:+.1f}%")

# NASDAQ for JD strategy
print()
print("=" * 60)
print("NASDAQ CRISIS CHECK (JD Strategy)")
print("=" * 60)
nasdaq = provider.get_historical_data("^IXIC", date(2025, 1, 1), date(2026, 3, 25))
if not nasdaq.empty:
    nq_close = nasdaq["Close"].astype(float)
    nq_ret = nq_close.pct_change() * 100
    recent_30 = nq_ret.iloc[-30:]
    crashes = recent_30[recent_30 <= -3.0]
    print(f"NASDAQ Close: {nq_close.iloc[-1]:,.2f}")
    print(f"-3%+ drops in last 30 trading days: {len(crashes)}")
    if len(crashes) > 0:
        for d, r in crashes.items():
            dt = d.date() if hasattr(d, 'date') else d
            print(f"  {dt}: {r:.1f}%")
    else:
        print("  None")
    if len(crashes) == 0:
        print("JD State: NORMAL")
    elif len(crashes) < 4:
        print("JD State: ALERT")
    else:
        print("JD State: PANIC")

# Uranium
print()
print("=" * 60)
print("URANIUM ETFS")
print("=" * 60)
for t in ["URA", "URNM"]:
    d = provider.get_historical_data(t, date(2025, 1, 1), date(2026, 3, 25))
    if not d.empty:
        c = d["Close"].astype(float)
        ytd_ret = (c.iloc[-1] / c.iloc[0] - 1) * 100
        print(f"  {t}: ${c.iloc[-1]:.2f} (YTD: {ytd_ret:+.1f}%)")
