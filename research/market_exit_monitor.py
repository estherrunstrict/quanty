#!/usr/bin/env python3
"""
Market exit monitor — applies the Kim Hyo-jin (신영증권) bubble-collapse exit
overlay to live data and prints a de-risk report.

This is the I/O front-end for `framework.regime.exit_overlay` (which is pure).
It fetches prices via yfinance, derives the three signals' inputs, evaluates
the composite overlay, and shows the resulting risk budget — i.e. how much a
vol-based long signal (quant_rv / managed-vol) should be scaled today.

It is exploration / research only: it never touches live `*AutoTrade*.py`,
`check_and_run.sh`, or any broker. It only reports.

Usage:
    python research/market_exit_monitor.py
    python research/market_exit_monitor.py --raw-long-weight 1.0
    python research/market_exit_monitor.py --json

Signals:
  A. Breadth divergence  — QQQ (leader) vs SPY (broad) vs DIA (old economy)
  B. Global negative count — 6-month returns of major DM/EM country ETFs
  C. Rate threshold       — ^TNX 10y yield vs its prior-cycle high
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# Allow running as `python research/market_exit_monitor.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.regime.exit_overlay import MarketExitOverlay  # noqa: E402

LEADER = "QQQ"
BROAD = "SPY"
OLDECON = "DIA"
RATE_TICKER = "^TNX"  # CBOE 10y Treasury yield, quoted in percent

# Major developed + emerging country ETFs, "far from the AI/semis leadership".
COUNTRY_ETFS = {
    "Germany": "EWG",
    "UK": "EWU",
    "France": "EWQ",
    "Japan": "EWJ",
    "Korea": "EWY",
    "Taiwan": "EWT",
    "India": "INDA",
    "China": "MCHI",
    "Brazil": "EWZ",
    "Mexico": "EWW",
}

SIXMO_DAYS = 126          # ~6 trading months for the global breadth lookback
RATE_HIGH_LOOKBACK = 600  # ~2.5y window for the "prior cycle high"
RATE_HIGH_EXCLUDE = 21    # ignore the last ~1m so "prior" high != today
STATE_FILE = Path(__file__).resolve().parent / "state" / "exit_overlay_neg_counts.json"
HISTORY_LEN = 12          # rolling negative-count readings kept for step detection


def _close_series(ticker: str, start: str, end: str) -> pd.Series:
    """Download adjusted closes for one ticker as a clean float Series."""
    df = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"no data for {ticker}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):  # yfinance MultiIndex single-ticker case
        close = close.iloc[:, 0]
    return close.astype(float).dropna()


def _daily_returns(close: pd.Series) -> list[float]:
    return close.pct_change().dropna().tolist()


def _trailing_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return close.iloc[-1] / close.iloc[0] - 1.0
    return close.iloc[-1] / close.iloc[-1 - days] - 1.0


def _load_history() -> list[int]:
    try:
        return json.loads(STATE_FILE.read_text()).get("negative_counts", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history(history: list[int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"negative_counts": history[-HISTORY_LEN:]}, indent=2))


def fetch_inputs() -> dict:
    """Pull everything the overlay needs. Returns a kwargs dict for evaluate()."""
    end = datetime.utcnow().date()
    start = (end - timedelta(days=400)).isoformat()  # ~1y+ of daily bars
    end_s = (end + timedelta(days=1)).isoformat()

    leader = _close_series(LEADER, start, end_s)
    broad = _close_series(BROAD, start, end_s)
    oldecon = _close_series(OLDECON, start, end_s)

    country_returns: dict[str, float] = {}
    for name, etf in COUNTRY_ETFS.items():
        try:
            c = _close_series(etf, start, end_s)
            country_returns[name] = _trailing_return(c, SIXMO_DAYS)
        except Exception as exc:  # noqa: BLE001 — skip a flaky ticker, keep going
            print(f"  ! skipping {name} ({etf}): {exc}", file=sys.stderr)

    # Rates: current yield + prior-cycle high (max over window, excluding last month).
    rate_start = (end - timedelta(days=RATE_HIGH_LOOKBACK + 200)).isoformat()
    tnx = _close_series(RATE_TICKER, rate_start, end_s)
    current_yield = float(tnx.iloc[-1])
    window = tnx.iloc[-(RATE_HIGH_LOOKBACK + RATE_HIGH_EXCLUDE): -RATE_HIGH_EXCLUDE]
    prior_cycle_high = float(window.max()) if len(window) else float(tnx.max())

    return dict(
        leader_returns=_daily_returns(leader),
        broad_returns=_daily_returns(broad),
        oldecon_returns=_daily_returns(oldecon),
        country_returns=country_returns,
        current_yield=current_yield,
        prior_cycle_high=prior_cycle_high,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-long-weight", type=float, default=1.0,
                    help="raw vol-based long weight to gate (default 1.0)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--no-state", action="store_true",
                    help="do not read/update the negative-count history file")
    args = ap.parse_args()

    print("Fetching market data...", file=sys.stderr)
    inputs = fetch_inputs()

    history = [] if args.no_state else _load_history()

    overlay = MarketExitOverlay()
    res = overlay.evaluate(prior_negative_counts=history, **inputs)

    if not args.no_state:
        _save_history(history + [res.global_breadth.negative_count])

    gated = res.gate(args.raw_long_weight)

    if args.json:
        out = {
            "as_of": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "risk_budget": round(res.risk_budget, 4),
            "exit_triggered": res.exit_triggered,
            "de_risk": round(res.de_risk, 4),
            "raw_long_weight": args.raw_long_weight,
            "gated_long_weight": round(gated, 4),
            "breadth": {
                "score": round(res.breadth.score, 4),
                "leader_mom": round(res.breadth.leader_mom, 4),
                "broad_mom": round(res.breadth.broad_mom, 4),
                "oldecon_mom": round(res.breadth.oldecon_mom, 4),
                "divergence_days": res.breadth.divergence_days,
                "concentration": res.breadth.concentration,
            },
            "global_breadth": {
                "score": round(res.global_breadth.score, 4),
                "negative_count": res.global_breadth.negative_count,
                "total": res.global_breadth.total,
                "negative_countries": res.global_breadth.negative_countries,
                "stepping_up": res.global_breadth.stepping_up,
            },
            "rate": {
                "factor": round(res.rate.factor, 4),
                "current_yield": res.rate.current_yield,
                "prior_cycle_high": res.rate.prior_cycle_high,
                "gap_bp": round(res.rate.gap_bp, 1),
                "breached": res.rate.breached,
            },
            "warnings": res.warnings,
        }
        print(json.dumps(out, indent=2))
        return 0

    bar = "█" * int(round(res.risk_budget * 20)) + "·" * (20 - int(round(res.risk_budget * 20)))
    print("\n" + "=" * 64)
    print("  KIM HYO-JIN BUBBLE-COLLAPSE EXIT OVERLAY")
    print(f"  as of {datetime.utcnow():%Y-%m-%d %H:%M UTC}")
    print("=" * 64)
    print(f"\n  RISK BUDGET : {res.risk_budget:5.0%}  [{bar}]")
    print(f"  EXIT SIGNAL : {'⚠️  DE-RISK / EXIT' if res.exit_triggered else 'hold'}")
    print(f"  long weight : {args.raw_long_weight:.2f}  ->  {gated:.3f}")
    print("\n  --- Signals ---")
    print(f"  A breadth   : score {res.breadth.score:4.0%}  | {res.breadth.reason}")
    print(f"  B global    : score {res.global_breadth.score:4.0%}  | {res.global_breadth.reason}")
    print(f"  C rate      : factor {res.rate.factor:4.0%} | {res.rate.reason}")
    if res.warnings:
        print("\n  --- Warnings ---")
        for w in res.warnings:
            print(f"  • {w}")
    print("=" * 64 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
