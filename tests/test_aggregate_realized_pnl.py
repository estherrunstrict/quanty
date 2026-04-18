"""Regression tests for scripts/aggregate_realized_pnl.py (state-source + helpers)
plus dashboard_server._enrich (derived per-bot metrics)."""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import aggregate_realized_pnl as agg
import dashboard_server as ds


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


# ── aggregate(): broker-source fallbacks ─────────────────────────────────

def test_aggregate_state_fallback_when_kis_disabled(monkeypatch, tmp_path):
    """If KIS auth fails, the state-file pass must still produce a usable result."""
    state_path = tmp_path / "fakebot_state.json"
    state_path.write_text(json.dumps({"closed_trades": [
        {"ticker": "FOO", "exit_date": "2026-01-15T10:00:00+09:00", "pnl": 250},
        {"ticker": "FOO", "exit_date": "2026-02-20T10:00:00+09:00", "pnl": -50},
    ]}), encoding="utf-8")
    fake_bot = agg.BotMeta("FAKE_BOT", "USD", "fake_bot", state_path, ("closed_trades",))

    monkeypatch.setattr(agg, "BOTS", [fake_bot])
    monkeypatch.setattr(agg, "_load_kis_env", lambda: (None, "KIS modules unavailable in test"))
    monkeypatch.setattr(agg, "_upbit_realized_trades", lambda y: ([], None))

    out, errors = agg.aggregate(2026, source_mode="both")

    assert "FAKE_BOT" in out["strategies"]
    bucket = out["strategies"]["FAKE_BOT"]
    assert bucket["realized_ytd"] == 200.0
    assert bucket["trades_ytd"] == 2
    assert bucket["wins_ytd"] == 1
    assert bucket["losses_ytd"] == 1
    assert bucket["win_rate_pct"] == 50.0
    assert bucket["source"] == "state"
    # KIS error must surface so operators see what fell back
    assert any("KIS modules unavailable" in e for e in errors)


def test_unknown_ticker_appears_in_errors():
    """Tickers not in TICKER_TO_STRATEGY get skipped and reported."""
    out = {"QUANT40": agg._empty_summary("USD")}
    errors: list[str] = []
    unknown = agg._attribute_trades_to_bots(
        [
            {"ticker": "ZZTOP", "pnl_native": 100, "exit_date": "20260315"},
            {"ticker": "SPY",   "pnl_native": 50,  "exit_date": "20260316"},  # known → QUANT40
        ],
        out, errors, "kis_test"
    )
    assert unknown == 1
    assert any("ZZTOP" in e for e in errors)
    # Known ticker still credited
    assert out["QUANT40"]["realized_ytd"] == 50.0
    assert out["QUANT40"]["trades_ytd"] == 1


def test_hybrid_vb_both_legs_from_shared_state(monkeypatch, tmp_path):
    """One hybrid_vb_state.json yields two independent buckets — KR and US."""
    shared = tmp_path / "hybrid_vb_state.json"
    shared.write_text(json.dumps({
        "kr": {"trade_history": [
            {"ticker": "069500", "exit_date": "2026-02-01", "pnl": 1000},
            {"ticker": "069500", "exit_date": "2026-03-01", "pnl": -200},
        ]},
        "us": {"trade_history": [
            {"ticker": "GLD", "exit_date": "2026-02-15", "pnl": 50},
        ]},
    }), encoding="utf-8")
    bots = [
        agg.BotMeta("HYBRID_VB_KR", "KRW", "hybrid_vb", shared, ("kr", "trade_history")),
        agg.BotMeta("HYBRID_VB_US", "USD", "hybrid_vb", shared, ("us", "trade_history")),
    ]
    monkeypatch.setattr(agg, "BOTS", bots)
    monkeypatch.setattr(agg, "_load_kis_env", lambda: (None, "KIS off"))
    monkeypatch.setattr(agg, "_upbit_realized_trades", lambda y: ([], None))

    out, _ = agg.aggregate(2026, source_mode="both")

    kr = out["strategies"]["HYBRID_VB_KR"]
    us = out["strategies"]["HYBRID_VB_US"]
    assert kr["currency"] == "KRW"
    assert us["currency"] == "USD"
    assert kr["realized_ytd"] == 800.0    # +1000 + -200
    assert kr["trades_ytd"] == 2
    assert kr["wins_ytd"] == 1
    assert us["realized_ytd"] == 50.0
    assert us["trades_ytd"] == 1
    assert us["wins_ytd"] == 1


# ── dashboard_server._enrich (derived per-bot metrics) ──────────────────

def test_enrich_total_pl_and_profit_rate():
    bucket: dict = {}
    realized = {
        "realized_ytd": 100.0,
        "trades_ytd": 5,
        "wins_ytd": 3,
        "losses_ytd": 2,
        "win_rate_pct": 60.0,
        "mdd_pct": -10.5,
        "source": "kis_overseas",
    }
    ds._enrich(bucket, unrealized=200.0, budget=10000.0, r=realized)
    assert bucket["unrealized_profit"] == 200.0
    assert bucket["realized_profit_ytd"] == 100.0
    assert bucket["total_pl_ytd"] == 300.0
    # 300 / 10000 * 100 = 3.0
    assert bucket["profit_rate_ytd_pct"] == 3.0
    assert bucket["realized_trades"] == 5
    assert bucket["cycles_ytd"] == 5
    assert bucket["win_rate_pct"] == 60.0
    assert bucket["mdd_pct"] == -10.5
    assert bucket["realized_source"] == "kis_overseas"


def test_enrich_zero_budget_yields_none_profit_rate():
    bucket: dict = {}
    ds._enrich(bucket, unrealized=50.0, budget=0, r={"realized_ytd": 10.0, "trades_ytd": 1})
    assert bucket["total_pl_ytd"] == 60.0
    assert bucket["profit_rate_ytd_pct"] is None


def test_enrich_handles_empty_realized():
    """Bot with no trades yet — enrichment must produce safe defaults, no crashes."""
    bucket: dict = {}
    ds._enrich(bucket, unrealized=0, budget=1000, r={})
    assert bucket["realized_profit_ytd"] == 0
    assert bucket["total_pl_ytd"] == 0
    assert bucket["profit_rate_ytd_pct"] == 0.0
    assert bucket["win_rate_pct"] is None
    assert bucket["mdd_pct"] is None
    assert bucket["realized_source"] == "none"
    assert bucket["cycles_ytd"] == 0
