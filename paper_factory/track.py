"""Read a paper bot's paper_state PV series and compute realized stats.

Reuses wiki/tools/portfolio_math.sharpe_with_ci so paper stats match every other
surface. days_observed < 60 -> low_confidence True (the 2-week-window caveat).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

from paper_factory import REPO

sys.path.insert(0, str(REPO.parent / "llm-wiki" / "Quanty" / "wiki" / "tools"))
import portfolio_math as pm  # noqa: E402

MIN_CONFIDENT_DAYS = 60


def compute_stats(pv_csv: Path) -> dict[str, Any]:
    if not pv_csv.exists():
        return {"days_observed": 0, "low_confidence": True, "sharpe": None,
                "total_return_pct": None}
    values: list[float] = []
    with pv_csv.open() as f:
        for row in csv.DictReader(f):
            try:
                values.append(float(row["portfolio_value"]))
            except (KeyError, ValueError):
                continue
    n = len(values)
    daily = [values[i] - values[i - 1] for i in range(1, n)]
    sharpe, lo, hi = pm.sharpe_with_ci(daily)
    total_ret = (values[-1] / values[0] - 1.0) * 100 if n >= 2 and values[0] else None
    return {"days_observed": n, "low_confidence": n < MIN_CONFIDENT_DAYS,
            "sharpe": sharpe, "sharpe_ci_low": lo, "sharpe_ci_high": hi,
            "total_return_pct": total_ret}


def _cli() -> None:
    import argparse, json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pv_csv", type=Path)
    print(json.dumps(compute_stats(ap.parse_args().pv_csv)))


if __name__ == "__main__":
    _cli()
