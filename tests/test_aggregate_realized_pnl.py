"""Smoke tests for scripts/aggregate_realized_pnl.py."""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import aggregate_realized_pnl as agg


KST = timezone(timedelta(hours=9))


def _make_state(tmp_path: Path, trades: list[dict], key: str = "closed_trades") -> Path:
    p = tmp_path / "state.json"
    p.write_text(json.dumps({key: trades}), encoding="utf-8")
    return p


def test_sums_ytd_trades_and_ignores_prior_year(tmp_path):
    state = _make_state(tmp_path, [
        {"ticker": "A", "exit_date": "2025-12-31T10:00:00+09:00", "pnl": 999},  # prior year — excluded
        {"ticker": "B", "exit_date": "2026-01-02T10:00:00+09:00", "pnl": 100},
        {"ticker": "C", "exit_date": "2026-03-15T10:00:00+09:00", "pnl": -30},
        {"ticker": "D", "exit_date": "2027-01-02T10:00:00+09:00", "pnl": 500},  # next year — excluded
    ])
    src = agg.Source("T", state, ("closed_trades",), "USD")
    out, errors = agg.aggregate(2026, sources=[src])
    assert errors == []
    assert out["strategies"]["T"]["realized_ytd"] == 70.0
    assert out["strategies"]["T"]["trades"] == 2
    assert out["strategies"]["T"]["last_trade"]["ticker"] == "C"
    assert out["_total_usd"] == 70.0
    assert out["_total_krw"] == 0.0


def test_missing_state_file_produces_zero(tmp_path):
    src = agg.Source("T", tmp_path / "nope.json", ("closed_trades",), "USD")
    out, errors = agg.aggregate(2026, sources=[src])
    assert errors == []
    assert out["strategies"]["T"]["realized_ytd"] == 0.0
    assert out["strategies"]["T"]["trades"] == 0


def test_nested_trade_path(tmp_path):
    p = tmp_path / "hybrid.json"
    p.write_text(json.dumps({
        "kr": {"trade_history": [
            {"ticker": "069500", "exit_date": "2026-02-01", "pnl": 1000},
        ]},
        "us": {"trade_history": [
            {"ticker": "SPY", "exit_date": "2026-02-02", "pnl": -50},
        ]},
    }), encoding="utf-8")
    kr = agg.Source("KR", p, ("kr", "trade_history"), "KRW")
    us = agg.Source("US", p, ("us", "trade_history"), "USD")
    out, errors = agg.aggregate(2026, sources=[kr, us])
    assert errors == []
    assert out["strategies"]["KR"]["realized_ytd"] == 1000.0
    assert out["strategies"]["US"]["realized_ytd"] == -50.0
    assert out["_total_krw"] == 1000.0
    assert out["_total_usd"] == -50.0


def test_malformed_trade_list_reports_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"closed_trades": "not a list"}), encoding="utf-8")
    src = agg.Source("T", p, ("closed_trades",), "USD")
    out, errors = agg.aggregate(2026, sources=[src])
    assert len(errors) == 1
    assert "not a list" in errors[0]
    assert out["strategies"]["T"]["realized_ytd"] == 0.0


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("stale", encoding="utf-8")
    agg.write_atomic(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}
