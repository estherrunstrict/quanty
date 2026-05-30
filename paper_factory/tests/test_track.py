import csv
from paper_factory import track


def _write_pv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["timestamp", "portfolio_value", "cash", "n_positions"])
        for i, v in enumerate(rows):
            w.writerow([f"2026-05-{i+1:02d}T09:40:00", f"{v:.2f}", "0", "1"])


def test_stats_counts_days_and_flags_low_confidence(tmp_path):
    pv = tmp_path / "paper_silver_copper_pv.csv"
    _write_pv(pv, [100000, 100500, 100200, 101000, 100800])
    s = track.compute_stats(pv)
    assert s["days_observed"] == 5
    assert s["low_confidence"] is True
    assert "sharpe" in s and "total_return_pct" in s


def test_missing_file_returns_zero_days(tmp_path):
    s = track.compute_stats(tmp_path / "nope.csv")
    assert s["days_observed"] == 0 and s["low_confidence"] is True
