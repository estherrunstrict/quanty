#!/usr/bin/env python3
"""
Aggregate YTD realized PnL per bot into strategy_results/realized_pl_{year}.json.

Reads each bot's trade-history source (a list of closed trades inside a
state file), filters to trades whose `exit_date` falls within the YTD
window (default: current KST year), sums `pnl`, and writes an atomic
JSON snapshot the dashboard can consume.

Safe to run while bots are actively trading — we take a fresh read of
each state file and write to a tmp file + os.replace, so a concurrent
bot write can't leave a partial aggregate file.

Exit codes:
    0 — all sources read cleanly
    1 — one or more sources failed to parse (aggregate still written,
        best-effort, with zeroed entries for the bad ones)
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "strategy_results"


@dataclass(frozen=True)
class Source:
    """Where to find one strategy's closed-trade list."""
    key: str                    # aggregator key, e.g. "HYBRID_VB_KR"
    state_path: Path            # absolute path to state file
    trade_list_path: tuple[str, ...]  # nested dict traversal to the list
    currency: str               # "USD" or "KRW"


SOURCES: list[Source] = [
    Source("BTC_VB",
           ROOT / "upbit_vb_strategy_state.json",
           ("closed_trades",),
           "KRW"),
    Source("HYBRID_VB_KR",
           ROOT / "hybrid_vb_state.json",
           ("kr", "trade_history"),
           "KRW"),
    Source("HYBRID_VB_US",
           ROOT / "hybrid_vb_state.json",
           ("us", "trade_history"),
           "USD"),
    Source("KOREA_ETF",
           ROOT / "korea_etf_momentum_state.json",
           ("closed_trades",),
           "KRW"),
    Source("JD_STRATEGY",
           ROOT / "jd_strategy_state.json",
           ("closed_trades",),
           "USD"),
    Source("QUANT40",
           ROOT / "quant40_state.json",
           ("closed_trades",),
           "USD"),
    Source("CLAUDE_AI_BOT",
           ROOT / "claude_trading_bot_state.json",
           ("trade_history",),
           "USD"),
    Source("DUAL_MOMENTUM",
           ROOT / "modified_dual_momentum_state.json",
           ("closed_trades",),
           "USD"),
]


def _nested(d: dict, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _parse_exit_date(raw: Any) -> Optional[datetime]:
    """Coerce exit_date to an aware datetime, assume KST if naive."""
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=KST)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Handle trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Date-only fallback
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def _ytd_window(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, tzinfo=KST)
    end = datetime(year + 1, 1, 1, tzinfo=KST)
    return start, end


def _summarize(source: Source, start: datetime, end: datetime) -> tuple[dict, Optional[str]]:
    """Return (summary_dict, error_message_or_None) for one source."""
    summary: dict[str, Any] = {
        "currency": source.currency,
        "realized_ytd": 0.0,
        "trades": 0,
        "last_trade": None,
    }

    if not source.state_path.exists():
        return summary, None  # absent state file = zero trades, not an error

    try:
        raw = source.state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
    except Exception as e:
        return summary, f"{source.key}: cannot read {source.state_path.name}: {e}"

    trades = _nested(state, source.trade_list_path)
    if trades is None:
        return summary, None
    if not isinstance(trades, list):
        return summary, f"{source.key}: trade list at {'.'.join(source.trade_list_path)} is not a list"

    total = 0.0
    count = 0
    last_trade: Optional[dict] = None
    last_exit: Optional[datetime] = None

    for t in trades:
        if not isinstance(t, dict):
            continue
        exit_dt = _parse_exit_date(t.get("exit_date"))
        if exit_dt is None or not (start <= exit_dt < end):
            continue
        pnl = t.get("pnl")
        try:
            pnl_f = float(pnl) if pnl is not None else 0.0
        except (TypeError, ValueError):
            continue
        total += pnl_f
        count += 1
        if last_exit is None or exit_dt > last_exit:
            last_exit = exit_dt
            last_trade = {
                "ticker": t.get("ticker"),
                "exit_date": exit_dt.isoformat(),
                "pnl": pnl_f,
            }

    summary["realized_ytd"] = round(total, 2)
    summary["trades"] = count
    summary["last_trade"] = last_trade
    return summary, None


def aggregate(year: int, sources: list[Source] = SOURCES) -> tuple[dict, list[str]]:
    start, end = _ytd_window(year)
    strategies: dict[str, dict] = {}
    errors: list[str] = []
    total_krw = 0.0
    total_usd = 0.0

    for src in sources:
        summary, err = _summarize(src, start, end)
        if err:
            errors.append(err)
        strategies[src.key] = summary
        if summary["currency"] == "KRW":
            total_krw += summary["realized_ytd"]
        elif summary["currency"] == "USD":
            total_usd += summary["realized_ytd"]

    out = {
        "_updated": datetime.now(KST).isoformat(timespec="seconds"),
        "_year": year,
        "_total_krw": round(total_krw, 2),
        "_total_usd": round(total_usd, 2),
        "strategies": strategies,
    }
    return out, errors


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.now(KST).year,
        help="YTD window year (KST). Defaults to current KST year.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path. Defaults to strategy_results/realized_pl_{year}.json",
    )
    args = parser.parse_args()

    out_path = args.output or (RESULTS_DIR / f"realized_pl_{args.year}.json")
    data, errors = aggregate(args.year)

    try:
        write_atomic(out_path, data)
    except Exception as e:
        print(f"FATAL: could not write {out_path}: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {out_path}")
    print(f"  total KRW: {data['_total_krw']:,.2f}")
    print(f"  total USD: {data['_total_usd']:,.2f}")
    for k, v in data["strategies"].items():
        print(f"  {k:<14} {v['currency']} {v['realized_ytd']:>14,.2f}  ({v['trades']} trades)")

    if errors:
        print("\nWARNINGS:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
