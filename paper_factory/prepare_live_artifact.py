"""Stage a live deployment for operator review — WITHOUT deploying.

Writes, under staging/<slug>/: a candidate <Slug>AutoTrade.py artifact and a
check_and_run.sh.diff SUGGESTION. It NEVER edits the real check_and_run.sh, never
writes into the automation_oracle root, and never scp's. The operator reviews the
staging dir and deploys via the normal automation_oracle flow.
"""
from __future__ import annotations

from pathlib import Path

from paper_factory import REPO


def _camel(slug: str) -> str:
    return "".join(p.capitalize() for p in slug.split("-"))


def prepare(slug: str, staging_root: Path | None = None,
            live_cron: Path | None = None) -> dict:
    staging_root = staging_root or (REPO / "paper_factory" / "staging")
    dest = staging_root / slug
    dest.mkdir(parents=True, exist_ok=True)

    artifact = dest / f"{_camel(slug)}AutoTrade.py"
    artifact.write_text(
        f'#!/usr/bin/env python3\n"""LIVE artifact for {slug} — STAGED, NOT DEPLOYED.\n'
        f'Operator: review, then deploy via the normal automation_oracle flow.\n'
        f'Derived from paper_run_{slug.replace("-", "_")}.py + the validated spec."""\n'
    )
    diff = dest / "check_and_run.sh.diff"
    diff.write_text(
        f"# SUGGESTED addition to check_and_run.sh (apply by hand, on the server):\n"
        f"# + launch {_camel(slug)}AutoTrade.py at its scheduled time\n"
    )
    return {"artifact": str(artifact), "cron_diff": str(diff),
            "note": "STAGED ONLY — operator deploys manually. live cron untouched."}


def _cli() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug")
    print(json.dumps(prepare(ap.parse_args().slug)))


if __name__ == "__main__":
    _cli()
