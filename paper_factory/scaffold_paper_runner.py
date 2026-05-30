"""Generate a paper-trading runner for a candidate and wire the PAPER cron.

Writes automation_oracle/paper_run_<slug_underscored>.py (a thin wrapper around
paper_factory.paper_engine that reads the candidate's backtest_goal_targets spec,
fetches CSV prices via the existing convention, computes target weights, and
appends a snapshot to paper_state/). Then appends a guarded block to
check_and_run_paper.sh. NEVER touches check_and_run.sh or any *AutoTrade*.py.
"""
from __future__ import annotations

import pathlib

from paper_factory import PAPER_CRON, REPO

RUNNER_TEMPLATE = '''#!/usr/bin/env python3
"""PAPER runner for {slug} — PARALLEL TO LIVE, simulated money only."""
from __future__ import annotations
import csv, sys
from datetime import datetime
from pathlib import Path
import yaml, pandas as pd
REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from paper_factory import paper_engine, STATE_DIR

SLUG = "{slug}"
SPEC = REPO.parent / "backtest_goal_targets" / f"{slug}.yaml"
NOTIONAL = 1_000_000.0  # paper notional (KRW-equivalent units)

def _load_prices(universe):
    frames = {{}}
    for t in universe:
        csv_path = REPO / f"{{t}}_daily.csv"
        if not csv_path.exists():
            continue
        df = pd.read_csv(csv_path, parse_dates=[0], index_col=0)
        col = next((c for c in ("Close", "close", "Adj Close", "adj_close")
                    if c in df.columns), None)
        frames[t] = df[col] if col is not None else df.iloc[:, -1]
    return pd.DataFrame(frames).dropna()

def main():
    spec = yaml.safe_load(SPEC.read_text())
    prices = _load_prices(spec["universe"])
    weights = paper_engine.target_weights(spec, prices)
    STATE_DIR.mkdir(exist_ok=True)
    pv = REPO / "paper_state" / f"paper_{slug_us}_pv.csv"
    new = not pv.exists()
    with pv.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "portfolio_value", "cash", "n_positions"])
        invested = sum(weights.values()) * NOTIONAL
        w.writerow([datetime.now().isoformat(timespec="seconds"),
                    f"{{NOTIONAL:.2f}}", f"{{NOTIONAL - invested:.2f}}", len(weights)])
    print(f"{slug} weights: {{weights}}")

if __name__ == "__main__":
    main()
'''

CRON_BLOCK = '''
# ---------- Paper Factory: {slug} (auto-generated) — {hour:02d}:{minute:02d} ET weekdays ----------
PF_{tag}_SESSION="paper_{slug_us}"
PF_{tag}_STATE_FILE="/tmp/paper_{slug_us}.run"
if [ "$is_us_weekday" = true ] && [ "$ny_hour" -eq {hour} ] && [ "$ny_minute" -eq {minute} ]; then
    if [ ! -f "$PF_{tag}_STATE_FILE" ]; then
        echo "[$(date)] launching $PF_{tag}_SESSION" >> "$LOG_FILE"
        touch "$PF_{tag}_STATE_FILE"
        tmux kill-session -t "$PF_{tag}_SESSION" 2>/dev/null || true
        tmux new-session -d -s "$PF_{tag}_SESSION" \\
            "cd $SCRIPT_DIR && $VENV_PYTHON paper_run_{slug_us}.py 2>&1 | tee -a paper_{slug_us}.log"
    fi
fi
'''


def _us(slug: str) -> str:
    return slug.replace("-", "_")


def render_runner(slug: str) -> str:
    return RUNNER_TEMPLATE.format(slug=slug, slug_us=_us(slug))


def write_runner(slug: str, repo: pathlib.Path = REPO) -> pathlib.Path:
    path = repo / f"paper_run_{_us(slug)}.py"
    path.write_text(render_runner(slug))
    return path


def append_cron_block(cron_path: pathlib.Path, slug: str, hour_et: int, minute_et: int) -> bool:
    marker = f"paper_run_{_us(slug)}.py"
    text = cron_path.read_text()
    if marker in text:
        return False
    block = CRON_BLOCK.format(slug=slug, slug_us=_us(slug),
                              tag=_us(slug).upper(), hour=hour_et, minute=minute_et)
    cron_path.write_text(text.rstrip() + "\n" + block)
    return True


def scaffold(slug: str, hour_et: int = 9, minute_et: int = 40) -> dict:
    runner = write_runner(slug)
    wired = append_cron_block(PAPER_CRON, slug, hour_et, minute_et)
    return {"runner": str(runner), "cron_wired": wired}


def _cli() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slug")
    ap.add_argument("--hour-et", type=int, default=9)
    ap.add_argument("--minute-et", type=int, default=40)
    args = ap.parse_args()
    print(json.dumps(scaffold(args.slug, args.hour_et, args.minute_et)))


if __name__ == "__main__":
    _cli()
