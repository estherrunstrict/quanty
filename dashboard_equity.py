#!/usr/bin/env python3
"""Shared equity math for both dashboard pipeline stages.

Single source of truth for: Eastern-Time date basis, FX->KRW conversion, and
pinning each bot's comparison-chart endpoint to its live Total P/L. Imported by
dashboard_server.py (operational API) and quanty-dashboard/generate_dashboard_data.py
(public generator) so the "chart == card" invariant has exactly one definition.
"""
from datetime import datetime, timezone, timedelta


def et_now():
    """Current Eastern Time (auto EDT/EST). Ported from dashboard_server so the
    equity_history snapshot row date and the pin date share one basis."""
    now_utc = datetime.now(timezone.utc)
    yr = now_utc.year
    mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
    dst_on = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
    nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
    dst_off = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=7)
    offset = -4 if dst_on <= now_utc < dst_off else -5
    return now_utc.astimezone(timezone(timedelta(hours=offset)))


def et_today():
    """Today's date (YYYY-MM-DD) in Eastern Time."""
    return et_now().strftime("%Y-%m-%d")


def to_krw(amount, currency, rate):
    """FX-convert a native amount to KRW (KRW passthrough; else * rate)."""
    return float(amount or 0) if currency == "KRW" else float(amount or 0) * rate


def pin_equity_endpoints(equity_series, strategies, rate, today):
    """Overwrite each bot's latest comparison-chart point with its live Total P/L (KRW).

    Replaces the last point in place when its date == today, else appends
    [today, val]. Handles hybrid_vb legs (kr=KRW, us=USD). Bots with no series
    are skipped (chart shows "insufficient data", never a wrong value). Past
    dates are untouched.
    """
    def _pin(key, total_native, cur):
        series = equity_series.get(key)
        if series is None:
            return
        val = round(to_krw(total_native, cur, rate), 2)
        if series and series[-1][0] == today:
            series[-1][1] = val
        else:
            series.append([today, val])

    for s in strategies:
        sid = s.get("id", "")
        if sid == "hybrid_vb":
            for leg_key, cur in (("kr", "KRW"), ("us", "USD")):
                leg = s.get(leg_key) or {}
                _pin("hybrid_vb_{}".format(leg_key), leg.get("total_pl_ytd", 0), cur)
            continue
        _pin(sid, s.get("total_pl_ytd", 0), s.get("currency", "KRW"))
