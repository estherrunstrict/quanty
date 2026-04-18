"""Regression tests for scripts/aggregate_realized_pnl.py (state-source + helpers)."""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import aggregate_realized_pnl as agg


KST = timezone(timedelta(hours=9))


def _mk_bot(tmp_path: Path, trades: list[dict], *, key="T", currency="USD",
            nested_path=("closed_trades",), nested_trades_wrapper=None) -> agg.BotMeta:
    p = tmp_path / "state.json"
    if nested_trades_wrapper:
        p.write_text(json.dumps(nested_trades_wrapper), encoding="utf-8")
    else:
        p.write_text(json.dumps({"closed_trades": trades}), encoding="utf-8")
    return agg.BotMeta(key=key, currency=currency, equity_id="x",
                       state_path=p, state_trade_path=nested_path)


def test_sums_ytd_trades_and_ignores_prior_year(tmp_path):
    bot = _mk_bot(tmp_path, [
        {"ticker": "A", "exit_date": "2025-12-31T10:00:00+09:00", "pnl": 999},
        {"ticker": "B", "exit_date": "2026-01-02T10:00:00+09:00", "pnl": 100},
        {"ticker": "C", "exit_date": "2026-03-15T10:00:00+09:00", "pnl": -30},
        {"ticker": "D", "exit_date": "2027-01-02T10:00:00+09:00", "pnl": 500},
    ])
    start, end = agg._ytd_window(2026)
    summary, err = agg._summarize_state(bot, start, end)
    assert err is None
    assert summary["realized_ytd"] == 70.0
    assert summary["trades_ytd"] == 2
    assert summary["wins_ytd"] == 1
    assert summary["losses_ytd"] == 1
    assert summary["win_rate_pct"] == 50.0
    assert summary["last_trade"]["ticker"] == "C"


def test_missing_state_file_produces_zero(tmp_path):
    bot = agg.BotMeta("T", "USD", "x", tmp_path / "nope.json", ("closed_trades",))
    start, end = agg._ytd_window(2026)
    summary, err = agg._summarize_state(bot, start, end)
    assert err is None
    assert summary["realized_ytd"] == 0.0
    assert summary["trades_ytd"] == 0
    assert summary["win_rate_pct"] is None


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
    bot_kr = agg.BotMeta("KR", "KRW", "h", p, ("kr", "trade_history"))
    bot_us = agg.BotMeta("US", "USD", "h", p, ("us", "trade_history"))
    start, end = agg._ytd_window(2026)

    kr, err_kr = agg._summarize_state(bot_kr, start, end)
    us, err_us = agg._summarize_state(bot_us, start, end)
    assert err_kr is None and err_us is None
    assert kr["realized_ytd"] == 1000.0
    assert kr["wins_ytd"] == 1
    assert us["realized_ytd"] == -50.0
    assert us["losses_ytd"] == 1


def test_malformed_trade_list_reports_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"closed_trades": "not a list"}), encoding="utf-8")
    bot = agg.BotMeta("T", "USD", "x", p, ("closed_trades",))
    start, end = agg._ytd_window(2026)
    summary, err = agg._summarize_state(bot, start, end)
    assert err is not None and "not a list" in err
    assert summary["realized_ytd"] == 0.0


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("stale", encoding="utf-8")
    agg.write_atomic(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


def test_mdd_from_equity_series():
    # Peak at 110, trough at 80 → -27.27%
    assert agg._compute_mdd_pct([100, 110, 105, 90, 80, 95]) == -27.27
    # Monotonic up: 0% drawdown
    assert agg._compute_mdd_pct([100, 110, 120, 130]) == 0.0
    # Too few points
    assert agg._compute_mdd_pct([100]) is None
    assert agg._compute_mdd_pct([]) is None
    # Zero / negative values filtered
    assert agg._compute_mdd_pct([0, -1, 100, 50]) == -50.0


def test_win_rate_from_mixed_pnl(tmp_path):
    bot = _mk_bot(tmp_path, [
        {"ticker": "A", "exit_date": "2026-01-01", "pnl": 10},
        {"ticker": "B", "exit_date": "2026-01-02", "pnl": -5},
        {"ticker": "C", "exit_date": "2026-01-03", "pnl": 20},
        {"ticker": "D", "exit_date": "2026-01-04", "pnl": 0},  # break-even = not a win
    ])
    start, end = agg._ytd_window(2026)
    summary, _ = agg._summarize_state(bot, start, end)
    assert summary["trades_ytd"] == 4
    assert summary["wins_ytd"] == 2
    assert summary["losses_ytd"] == 2   # 1 loss + 1 break-even both count as non-win
    assert summary["win_rate_pct"] == 50.0
