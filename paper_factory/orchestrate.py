"""Paper Factory driver: select -> (skill: extract/backtest/route/memo) ->
paper-deploy -> track -> 1M memo. ONE bot in flight at a time via a lock file.

The skill-driven stages (1-4) need a Claude session (reading the paper, fixing
ambiguous specs), so this module exposes the deterministic glue — lock handling,
the human-readable stage plan, and the post-backtest steps (scaffold->memo) — and
is invoked from a session. It never runs live deploys.
"""
from __future__ import annotations

from pathlib import Path

from paper_factory import REPO

LOCK = REPO / "paper_factory" / "in_flight.lock"


def acquire_lock(lock: Path, slug: str) -> bool:
    if lock.exists():
        return False
    lock.write_text(slug + "\n")
    return True


def release_lock(lock: Path) -> None:
    if lock.exists():
        lock.unlink()


def plan_stages() -> list[str]:
    return [
        "1. select + extract + backtest + route + audit-memo  (run: /build-trading-bot auto)",
        "2. on backtest pass -> paper-deploy (scaffold_paper_runner.py <slug>)",
        "3. ~2 weeks: paper cron accumulates paper_state/",
        "4. track.py paper_state/paper_<slug>_pv.csv -> stats",
        "5. write_live_memo.py <slug> -> proposed 1M-KRW memo — STOP for human approval",
    ]


def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["plan", "release"])
    args = ap.parse_args()
    if args.command == "plan":
        print("\n".join(plan_stages()))
    elif args.command == "release":
        release_lock(LOCK)
        print("lock released")


if __name__ == "__main__":
    _cli()
