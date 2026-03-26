"""Backtest performance metrics — Sharpe, CAGR, MDD, etc."""
from dataclasses import dataclass, field
from typing import List
import numpy as np


@dataclass
class BacktestMetrics:
    cagr: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    volatility: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0
    start_value: float = 0.0
    end_value: float = 0.0
    equity_curve: List[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"CAGR: {self.cagr:.2%} | MDD: {self.max_drawdown:.2%} | "
            f"Sharpe: {self.sharpe_ratio:.2f} | Trades: {self.num_trades} | "
            f"${self.start_value:,.0f} -> ${self.end_value:,.0f}"
        )


def compute_metrics(
    equity_curve: List[dict],
    daily_returns: List[float],
    initial_cash: float,
    trade_log: List[dict],
) -> BacktestMetrics:
    """Compute standard backtest metrics from equity curve and returns."""
    if not equity_curve or len(equity_curve) < 2:
        return BacktestMetrics(start_value=initial_cash, end_value=initial_cash)

    values = [e["value"] for e in equity_curve]
    start_val = values[0]
    end_val = values[-1]

    # Total return
    total_return = (end_val - start_val) / start_val if start_val > 0 else 0

    # CAGR
    n_days = len(equity_curve)
    n_years = n_days / 252.0
    if n_years > 0 and start_val > 0 and end_val > 0:
        cagr = (end_val / start_val) ** (1.0 / n_years) - 1.0
    else:
        cagr = 0.0

    # Max Drawdown
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd

    # Sharpe Ratio (annualized, assuming 0 risk-free rate)
    returns_arr = np.array(daily_returns) if daily_returns else np.array([0.0])
    vol = float(np.std(returns_arr)) if len(returns_arr) > 1 else 0.0
    ann_vol = vol * np.sqrt(252)
    mean_ret = float(np.mean(returns_arr))
    sharpe = (mean_ret * 252) / ann_vol if ann_vol > 0 else 0.0

    return BacktestMetrics(
        cagr=cagr,
        total_return=total_return,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        volatility=ann_vol,
        num_trades=len(trade_log),
        start_value=start_val,
        end_value=end_val,
        equity_curve=equity_curve,
    )
