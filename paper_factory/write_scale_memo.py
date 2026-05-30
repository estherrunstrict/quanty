"""Write a `proposed` memo to scale a proven live bot from 1M to 20M KRW.

Hard-gated on >= 60 live trading days (the rigorous bar the 2-week paper stage
skipped). Flags a concentration warning if 20M would exceed 40% of the FX-
converted portfolio (purpose.md rule). Always proposed; human approves.
"""
from __future__ import annotations

from pathlib import Path

from paper_factory import WIKI

SCALE_KRW = 20_000_000
MIN_LIVE_DAYS = 60
MAX_CONCENTRATION = 0.40


class GateNotMet(RuntimeError):
    """Raised when fewer than MIN_LIVE_DAYS live trading days have elapsed."""


def render_memo(slug: str, live_days: int, portfolio_total_krw: float, today: str) -> str:
    if live_days < MIN_LIVE_DAYS:
        raise GateNotMet(f"{live_days} live days < {MIN_LIVE_DAYS} required")
    share = SCALE_KRW / portfolio_total_krw if portfolio_total_krw else 1.0
    warn = ""
    if share > MAX_CONCENTRATION:
        warn = (f"\n> ⚠️ **CONCENTRATION WARNING.** 20M KRW would be {share:.0%} of the "
                f"~{portfolio_total_krw:,.0f} KRW portfolio, over the 40% cap "
                f"(purpose.md). Reduce the tranche or rebalance first.\n")
    return f"""---
type: decision
title: "Scale {slug} to 20M KRW"
tags: [paper-factory, allocate, scale-up]
related: [[{slug}]]
created: {today}
updated: {today}
decision_type: allocate
target: [[{slug}]]
proposed_by: claude
status: proposed
effective_date: {today}
---

# Scale [[{slug}]] to 20M KRW

## Recommendation

> Scale [[{slug}]] from 1M to **{SCALE_KRW:,} KRW** after {live_days} live trading days.
{warn}
## Evidence

- Live trading days: {live_days} (>= {MIN_LIVE_DAYS} bar met).
- See [[scorecard]] for realized live Sharpe/MDD vs backtest.
- Proposed share of portfolio: {share:.0%}.

## Trade-offs

- Gain: full capital behind a now-proven edge.
- Risk: larger drawdowns in absolute KRW; concentration.
- Do nothing: capital stays parked at 1M.

## Counter-arguments

- 60 days is the minimum, not a guarantee of regime coverage.
- Check realized correlation with existing live bots before scaling.

## Proposed action

- [ ] Jae sets status to `approved` (or `rejected`).
- [ ] On approval: operator adjusts the live position sizing via the normal flow.

## Decision

> **Status: proposed** — Jae sets `approved`/`rejected` with reasoning.
"""


def write_memo(slug: str, live_days: int, portfolio_total_krw: float, today: str,
               decisions_dir: Path | None = None) -> Path:
    text = render_memo(slug, live_days, portfolio_total_krw, today)
    decisions_dir = decisions_dir or (WIKI / "decisions")
    decisions_dir.mkdir(parents=True, exist_ok=True)
    path = decisions_dir / f"{today}-{slug}-scale-20m-krw.md"
    path.write_text(text)
    return path


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug")
    ap.add_argument("--live-days", type=int, required=True)
    ap.add_argument("--portfolio-krw", type=float, required=True)
    ap.add_argument("--today", required=True)
    args = ap.parse_args()
    print(write_memo(args.slug, args.live_days, args.portfolio_krw, args.today))


if __name__ == "__main__":
    _cli()
