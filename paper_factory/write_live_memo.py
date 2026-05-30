"""Write a `proposed` decision memo to fund a paper-validated bot with 1M KRW.

ALWAYS proposed; ALWAYS carries the LOW-CONFIDENCE banner (the ~2-week paper
window cannot produce a meaningful Sharpe CI). A human flips it to approved.
Follows the wiki decision-memo template/frontmatter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from paper_factory import WIKI

LIVE_KRW = 1_000_000


def render_memo(slug: str, stats: dict[str, Any], today: str) -> str:
    sharpe = stats.get("sharpe")
    sharpe_s = f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else "n/a"
    ret_s = stats.get("total_return_pct")
    ret_s = f"{ret_s:+.2f}%" if isinstance(ret_s, (int, float)) else "n/a"
    return f"""---
type: decision
title: "Fund {slug} with 1M KRW (provisional)"
tags: [paper-factory, allocate]
related: [[{slug}]]
created: {today}
updated: {today}
decision_type: allocate
target: [[{slug}]]
proposed_by: claude
status: proposed
effective_date: {today}
---

# Fund [[{slug}]] with 1M KRW — provisional

## Recommendation

> Deploy [[{slug}]] live with **{LIVE_KRW:,} KRW** as a small validation tranche.

> ⚠️ **LOW-CONFIDENCE.** Only {stats.get('days_observed')} paper trading days observed.
> This window validates execution plumbing (fills, slippage, no rejects) — NOT edge.
> The realized Sharpe CI is uninformative at this sample size. Real edge is gated
> at the 20M scale-up after 60 live trading days.

## Evidence

- Paper days observed: {stats.get('days_observed')}
- Realized paper Sharpe (annualized, Lo CI): {sharpe_s}
- Paper total return over window: {ret_s}
- Backtest gate already passed (see [[{slug}]] frontmatter + ranking_target files).

## Trade-offs

- Gain: real-money execution validation at a capped 1M KRW risk.
- Risk: a possibly-overfit strategy loses part of 1M KRW.
- Do nothing: strategy stays paper-only; no execution learning.

## Counter-arguments

- 10 days is far short of the 60-day statistical bar; the edge is unproven.
- A passing backtest can still be regime-lucky or selection-biased.

## Proposed action

- [ ] Jae sets status to `approved` (or `rejected`).
- [ ] On approval: operator runs `prepare_live_artifact.py {slug}` and deploys the
      staged live script via the normal automation_oracle flow (NOT this pipeline).
- [ ] After 60 live trading days: revisit for the 20M scale-up memo.

## Decision

> **Status: proposed** — Jae sets `approved`/`rejected` with one-sentence reasoning.
"""


def write_memo(slug: str, stats: dict[str, Any], today: str,
               decisions_dir: Path | None = None) -> Path:
    decisions_dir = decisions_dir or (WIKI / "decisions")
    decisions_dir.mkdir(parents=True, exist_ok=True)
    path = decisions_dir / f"{today}-{slug}-fund-1m-krw.md"
    path.write_text(render_memo(slug, stats, today))
    return path


def _cli() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug")
    ap.add_argument("--stats-json", required=True, help="JSON from track.py")
    ap.add_argument("--today", required=True)
    args = ap.parse_args()
    print(write_memo(args.slug, json.loads(args.stats_json), args.today))


if __name__ == "__main__":
    _cli()
