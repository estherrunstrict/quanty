"""Compute today's target weights for a strategy spec, for paper trading.

One function, `target_weights(spec, prices)`, dispatched on signal.type for the
5 supported template types. `prices` is a date-indexed DataFrame of daily closes
(columns = universe tickers). Returns {ticker: weight} for longs (weights sum to
1 when invested, {} when fully in cash). Deliberately simple — this paces a
daily/monthly paper bot, not a high-frequency engine.
"""
from __future__ import annotations

from typing import Any

import pandas as pd


class EngineError(ValueError):
    """Raised for unsupported signal types or insufficient data."""


def _lookback_return(prices: pd.DataFrame, days: int) -> pd.Series:
    if len(prices) <= days:
        raise EngineError(f"need >{days} rows, have {len(prices)}")
    return prices.iloc[-1] / prices.iloc[-1 - days] - 1.0


def _equal_weight(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    w = 1.0 / len(tickers)
    return {t: w for t in tickers}


def target_weights(spec: dict[str, Any], prices: pd.DataFrame) -> dict[str, float]:
    sig = spec.get("signal", {})
    stype = sig.get("type")
    universe = [t for t in spec.get("universe", []) if t in prices.columns]
    lb = int(sig.get("lookback_days", 60))

    if stype in ("xs_momentum",):
        rets = _lookback_return(prices[universe], lb).sort_values(ascending=False)
        n_long = max(1, len(universe) // 3)
        return _equal_weight(list(rets.index[:n_long]))

    if stype in ("ts_momentum",):
        rets = _lookback_return(prices[universe], lb)
        return _equal_weight([t for t in universe if rets[t] > 0])

    if stype in ("dual_momentum",):
        rets = _lookback_return(prices[universe], lb)
        winners = [t for t in universe if rets[t] > 0]
        if not winners:
            return {}
        best = max(winners, key=lambda t: rets[t])
        return {best: 1.0}

    if stype in ("vol_breakout",):
        held = []
        for t in universe:
            s = prices[t]
            if len(s) < 2:
                continue
            rng = s.iloc[-2] - prices[t].rolling(2).min().iloc[-2]
            if s.iloc[-1] > s.iloc[-2] + 0.5 * abs(rng):
                held.append(t)
        return _equal_weight(held)

    if stype in ("mean_reversion",):
        held = []
        for t in universe:
            s = prices[t].tail(lb)
            if len(s) < 3 or s.std() == 0:
                continue
            z = (s.iloc[-1] - s.mean()) / s.std()
            if z < -1.0:
                held.append(t)
        return _equal_weight(held)

    raise EngineError(f"unsupported signal type: {stype!r}")
