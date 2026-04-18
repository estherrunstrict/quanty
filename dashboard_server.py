#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard API Server — zero-dependency (stdlib only)
=====================================================
Reads strategy_results/*.json, state files, and recent logs,
serves them as JSON at /api/data. Also serves dashboard.html.

Usage:
    python3 dashboard_server.py              # default port 8077
    python3 dashboard_server.py --port 9000  # custom port

Endpoints:
    GET /              → dashboard.html
    GET /api/data      → full dashboard JSON payload
"""

import http.server
import json
import os
import re
import sys
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
RESULTS_DIR = ROOT / "strategy_results"
EQUITY_HISTORY_FILE = RESULTS_DIR / "equity_history.jsonl"
EQUITY_START_DATE = "2026-01-01"
KST = timezone(timedelta(hours=9))
def _get_et_now():
    """Get current Eastern Time (auto EDT/EST)."""
    now_utc = datetime.now(timezone.utc)
    yr = now_utc.year
    mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
    dst_on = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
    nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
    dst_off = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=7)
    offset = -4 if dst_on <= now_utc < dst_off else -5
    return now_utc.astimezone(timezone(timedelta(hours=offset)))

# ── Data Loaders ──────────────────────────────────────────────

def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _load_yaml(path):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def _file_age_hours(path):
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except Exception:
        return 999


def _parse_log_events(log_path, max_lines=500):
    """Parse recent log lines into structured events for the feed."""
    events = []
    if not os.path.exists(log_path):
        return events

    try:
        with open(log_path, "rb") as f:
            # Read last chunk
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 80000)
            f.seek(max(0, size - read_size))
            lines = f.read().decode("utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return events

    # Pattern: 2026-04-03 09:00:05,510 - VB_Strategy - INFO - message
    pat = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - (\w+) - (\w+) - (.+)"
    )

    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        ts_str, source, level, msg = m.groups()

        # Filter to significant events only
        event = None

        if "Daily Run" in msg:
            event = {"type": "session", "title": f"Daily Run Started",
                     "body": msg.strip("= "), "source": source}
        elif "NO SIGNAL" in msg:
            event = {"type": "signal", "title": "NO SIGNAL — Bearish",
                     "body": msg, "source": source}
        elif "LONG signal active" in msg:
            event = {"type": "signal", "title": "LONG Signal Active",
                     "body": msg, "source": source}
        elif "BREAKOUT!" in msg or "Breakout triggered" in msg:
            event = {"type": "trade_buy", "title": "Breakout Triggered",
                     "body": msg, "source": source}
        elif "MARKET BUY filled" in msg or "MARKET BUY" in msg:
            event = {"type": "trade_buy", "title": "Market Buy Filled",
                     "body": msg, "source": source}
        elif msg.startswith("SELL ") or "sell_at_market" in msg.lower():
            event = {"type": "trade_sell", "title": "Sell Executed",
                     "body": msg, "source": source}
        elif "No breakout" in msg:
            event = {"type": "signal", "title": "No Breakout Today",
                     "body": msg, "source": source}
        elif "Breakout target" in msg:
            event = {"type": "signal", "title": "Target Set",
                     "body": msg, "source": source}
        elif "Price feed" in msg.lower() or "feed down" in msg.lower():
            event = {"type": "error", "title": "Price Feed Issue",
                     "body": msg, "source": source}
        elif level == "ERROR":
            event = {"type": "error", "title": "Error",
                     "body": msg[:200], "source": source}
        elif "Done." in msg:
            event = {"type": "session", "title": "Session Complete",
                     "body": msg, "source": source}

        if event:
            event["timestamp"] = ts_str
            events.append(event)

    return events


# ── Equity History (daily snapshots for sparkline charts) ────

def _load_equity_history():
    """Load all daily equity snapshots from JSONL file."""
    history = []
    if not EQUITY_HISTORY_FILE.exists():
        return history
    try:
        with open(EQUITY_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        history.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass
    return history


def _save_equity_snapshot(strategies):
    """Append today's equity snapshot to history file (one entry per day)."""
    today = _get_et_now().strftime("%Y-%m-%d")

    # Check if today already recorded
    history = _load_equity_history()
    if history and history[-1].get("date") == today:
        return  # already saved today

    snapshot = {"date": today}
    for s in strategies:
        sid = s.get("id", "")
        if sid == "hybrid_vb":
            snapshot["hybrid_vb_kr"] = s.get("kr", {}).get("value", 0)
            snapshot["hybrid_vb_us"] = s.get("us", {}).get("value", 0)
        elif sid == "claude_bot":
            continue  # analysis-only, no portfolio value
        else:
            val = s.get("value", 0)
            cash = s.get("cash", 0)
            snapshot[sid] = val + cash if sid in ("quant40", "jd_strategy", "dual_momentum") else val

    try:
        with open(EQUITY_HISTORY_FILE, "a") as f:
            f.write(json.dumps(snapshot, default=str) + "\n")
    except Exception:
        pass


def _get_equity_series():
    """Return equity history as {strategy_id: [[date, value], ...]} starting from EQUITY_START_DATE."""
    history = _load_equity_history()
    series = {}

    for entry in history:
        date = entry.get("date", "")
        if date < EQUITY_START_DATE:
            continue
        for key, val in entry.items():
            if key == "date":
                continue
            if key not in series:
                series[key] = []
            try:
                series[key].append([date, float(val)])
            except (ValueError, TypeError):
                pass

    return series


def _enrich(bucket: dict, unrealized: float, budget: float, r: dict) -> None:
    """Merge realized-aggregator fields + compute derived metrics into bucket.

    Module-level (vs nested in build_dashboard_data) so the tests can call it
    directly with synthetic inputs — see tests/test_aggregate_realized_pnl.py.
    """
    bucket["unrealized_profit"] = unrealized or 0
    bucket["realized_profit_ytd"] = r.get("realized_ytd", 0) or 0
    bucket["realized_trades"] = r.get("trades_ytd", 0) or 0
    bucket["wins_ytd"] = r.get("wins_ytd", 0) or 0
    bucket["losses_ytd"] = r.get("losses_ytd", 0) or 0
    bucket["win_rate_pct"] = r.get("win_rate_pct")   # None when no trades
    bucket["mdd_pct"] = r.get("mdd_pct")             # None when no equity series
    bucket["realized_source"] = r.get("source", "none")
    bucket["cycles_ytd"] = bucket["realized_trades"]  # cycle = closed round-trip

    # Derived totals
    total_pl = (unrealized or 0) + bucket["realized_profit_ytd"]
    bucket["total_pl_ytd"] = total_pl
    bucket["profit_rate_ytd_pct"] = (
        round(total_pl / budget * 100, 2) if budget and budget > 0 else None
    )


def build_dashboard_data():
    """Assemble full dashboard payload from all data sources."""
    config = _load_yaml(ROOT / "config.yaml")

    def _cost_basis(holdings):
        """Sum of avg_price * quantity for all holdings = actual invested amount."""
        return sum(h.get("avg_price", 0) * h.get("quantity", 0) for h in holdings)

    # ── Budget caps from config ──
    cap_mgmt = config.get("capital_management", {})

    # ── Strategy Results ──
    strategies = []

    # 1) BTC VB Strategy
    vb_state = _load_json(ROOT / "upbit_vb_strategy_state.json") or {}
    vb_config = config.get("upbit_vb_strategy", {})
    equity_hist = vb_state.get("equity_history", [])
    last_equity = equity_hist[-1][1] if equity_hist else 0

    # Unrealized = mark-to-market on currently-held BTC. Zero when flat.
    # We avoid `total_pnl_krw` here because that's cumulative REALIZED and was
    # being surfaced as unrealized, double-counting when the Realized YTD cell
    # landed next to it.
    vb_unrealized = 0.0
    if vb_state.get("is_holding") and vb_state.get("btc_amount", 0) > 0:
        try:
            import pyupbit  # type: ignore
            price = pyupbit.get_current_price("KRW-BTC")
            if price:
                btc = float(vb_state.get("btc_amount", 0) or 0)
                entry = float(vb_state.get("entry_amount_krw", 0) or 0)
                vb_unrealized = btc * float(price) - entry
        except Exception:
            vb_unrealized = 0.0

    strategies.append({
        "id": "btc_vb",
        "name": "BTC Volatility Breakout",
        "currency": "KRW",
        "mode": "paper" if vb_config.get("paper_trading") else "live",
        "value": last_equity,
        "profit": vb_unrealized,
        "budget": vb_config.get("max_capital_krw", 0),
        "is_holding": vb_state.get("is_holding", False),
        "total_trades": vb_state.get("total_trades", 0),
        "winning_trades": vb_state.get("winning_trades", 0),
        "allocation": vb_state.get("current_allocation", 1.0),
        "last_run": vb_state.get("last_run_date", ""),
        "holdings": [],
        "extra": {
            "k": vb_config.get("k_long", 0.4),
            "atr_period": vb_config.get("atr_period", 7),
            "ma_period": vb_config.get("ma_period", 5),
            "equity_days": len(equity_hist),
        },
        "stale_hours": _file_age_hours(ROOT / "upbit_vb_strategy_state.json"),
    })

    # 2) Korea ETF Momentum
    kr_etf = _load_json(RESULTS_DIR / "korea_etf_momentum_result.json")
    if kr_etf:
        strategies.append({
            "id": "korea_etf",
            "name": "Korea ETF Momentum",
            "currency": "KRW",
            "mode": "paper" if kr_etf.get("session_summary", {}).get("paper_trading") else "live",
            "value": sum(h.get("value", 0) for h in kr_etf.get("holdings", [])),
            "cost_basis": _cost_basis(kr_etf.get("holdings", [])),
            "profit": kr_etf.get("total_profit", 0),
            "budget": cap_mgmt.get("korea_etf_momentum", {}).get("budget_krw", 0),
            "holdings": kr_etf.get("holdings", []),
            "extra": {
                "target": kr_etf.get("session_summary", {}).get("target_name", ""),
                "target_ticker": kr_etf.get("session_summary", {}).get("target_ticker", ""),
                "rebalance_day": kr_etf.get("session_summary", {}).get("rebalance_day", False),
            },
            "stale_hours": _file_age_hours(RESULTS_DIR / "korea_etf_momentum_result.json"),
            "timestamp": kr_etf.get("timestamp", ""),
        })

    # 3) Quant40
    q40 = _load_json(RESULTS_DIR / "quant40_result.json")
    if q40:
        strategies.append({
            "id": "quant40",
            "name": "US Quant40",
            "currency": "USD",
            "mode": "live",
            "value": sum(h.get("value", 0) for h in q40.get("holdings", [])),
            "cost_basis": _cost_basis(q40.get("holdings", [])),
            "cash": q40.get("cash_balance", 0),
            "profit": q40.get("total_profit", 0),
            "holdings": q40.get("holdings", []),
            "budget": q40.get("allocated_capital") or cap_mgmt.get("quant40", {}).get("budget_usd", 0),
            "extra": {
                "signal": q40.get("session_summary", {}).get("trend_description", ""),
                "regime": q40.get("session_summary", {}).get("regime", ""),
            },
            "stale_hours": _file_age_hours(RESULTS_DIR / "quant40_result.json"),
            "timestamp": q40.get("timestamp", ""),
        })

    # 4) JD Strategy
    jd = _load_json(RESULTS_DIR / "jd_strategy_result.json")
    if jd:
        jd_session = jd.get("session_summary", {})
        jd_holdings_value = sum(h.get("value", 0) for h in jd.get("holdings", []))
        jd_market_state = jd_session.get("market_state", "unknown").upper()
        jd_regime = jd_session.get("regime", "").upper()
        state_emojis = {"NORMAL": "🟢", "ALERT": "🟡", "PANIC": "🔴"}
        jd_signal = f"{state_emojis.get(jd_market_state, '⚪')} {jd_market_state} | {jd_regime}"
        strategies.append({
            "id": "jd_strategy",
            "name": "JD Strategy",
            "currency": "USD",
            "mode": "live",
            "value": jd_holdings_value,
            "cost_basis": _cost_basis(jd.get("holdings", [])),
            "cash": jd.get("cash_balance", 0),
            "profit": jd.get("total_profit", 0),
            "holdings": jd.get("holdings", []),
            "budget": jd.get("allocated_capital") or cap_mgmt.get("jd_strategy", {}).get("budget_usd", 0),
            "extra": {
                "signal": jd_signal,
                "state": jd_market_state,
                "regime": jd_regime,
                "is_holding": jd_holdings_value > 0,
            },
            "stale_hours": _file_age_hours(RESULTS_DIR / "jd_strategy_result.json"),
            "timestamp": jd.get("timestamp", ""),
        })

    # 5) Modified Dual Momentum
    mdm = _load_json(RESULTS_DIR / "modified_dual_momentum_result.json")
    if mdm:
        strategies.append({
            "id": "dual_momentum",
            "name": "Dual Momentum",
            "currency": "USD",
            "mode": "live",
            "value": sum(h.get("value", 0) for h in mdm.get("holdings", [])),
            "cost_basis": _cost_basis(mdm.get("holdings", [])),
            "cash": mdm.get("cash_balance", 0),
            "profit": mdm.get("total_profit", 0),
            "holdings": mdm.get("holdings", []),
            "budget": mdm.get("allocated_capital") or cap_mgmt.get("modified_dual_momentum", {}).get("budget_usd", 0),
            "extra": {
                "mode_label": mdm.get("session_summary", {}).get("mode", ""),
                "rebalance_day": mdm.get("session_summary", {}).get("rebalance_day", False),
            },
            "stale_hours": _file_age_hours(RESULTS_DIR / "modified_dual_momentum_result.json"),
            "timestamp": mdm.get("timestamp", ""),
        })

    # 6) Hybrid VB (KR + US)
    hvb_kr = _load_json(RESULTS_DIR / "hybrid_vb_kr_result.json")
    hvb_us = _load_json(RESULTS_DIR / "hybrid_vb_us_result.json")
    hvb_state = _load_json(ROOT / "hybrid_vb_state.json") or {}

    hvb_data = {
        "id": "hybrid_vb",
        "name": "Hybrid VB",
        "currency": "MULTI",
        "mode": "live",
        "budget_kr": cap_mgmt.get("hybrid_vb_kr", {}).get("budget_krw", 0),
        "budget_us": cap_mgmt.get("hybrid_vb_us", {}).get("budget_usd", 0),
        "kr": {},
        "us": {},
        "holdings": [],
    }

    if hvb_kr:
        hvb_data["kr"] = {
            "value": sum(h.get("value", 0) for h in hvb_kr.get("holdings", [])),
            "cost_basis": _cost_basis(hvb_kr.get("holdings", [])),
            "profit": hvb_kr.get("total_profit", 0),
            "holdings": hvb_kr.get("holdings", []),
            "regime": hvb_kr.get("session_summary", {}).get("regime", {}),
            "heat": hvb_kr.get("session_summary", {}).get("heat", 0),
            "dd": hvb_kr.get("session_summary", {}).get("drawdown", 0),
            "open_positions": hvb_kr.get("session_summary", {}).get("open_positions_count", 0),
        }
        hvb_data["holdings"].extend(hvb_kr.get("holdings", []))

    if hvb_us:
        hvb_data["us"] = {
            "value": sum(h.get("value", 0) for h in hvb_us.get("holdings", [])),
            "cost_basis": _cost_basis(hvb_us.get("holdings", [])),
            "profit": hvb_us.get("total_profit", 0),
            "holdings": hvb_us.get("holdings", []),
            "regime": hvb_us.get("session_summary", {}).get("regime", {}),
            "heat": hvb_us.get("session_summary", {}).get("heat", 0),
            "dd": hvb_us.get("session_summary", {}).get("drawdown", 0),
            "open_positions": hvb_us.get("session_summary", {}).get("open_positions_count", 0),
        }
        hvb_data["holdings"].extend(hvb_us.get("holdings", []))

    # Open positions from state
    hvb_data["open_positions"] = {
        "kr": hvb_state.get("kr", {}).get("open_positions", {}),
        "us": hvb_state.get("us", {}).get("open_positions", {}),
    }
    hvb_kr_age = _file_age_hours(RESULTS_DIR / "hybrid_vb_kr_result.json")
    hvb_us_age = _file_age_hours(RESULTS_DIR / "hybrid_vb_us_result.json")
    # Use min so one missing file (999h) doesn't mark both stale
    hvb_data["stale_hours"] = min(hvb_kr_age, hvb_us_age)

    strategies.append(hvb_data)

    # 7) Claude Trading Bot (AI Market Analysis + Trading)
    claude_bot_dir = ROOT / "claude-trading-bot"
    claude_regime = None
    claude_screener = None
    claude_ideas = None

    # Load trading result (portfolio value, holdings, profit)
    cb_result = _load_json(RESULTS_DIR / "claude_trading_bot_result.json")
    cb_session = cb_result.get("session_summary", {}) if cb_result else {}

    # Find latest regime report
    regime_dir = claude_bot_dir / "reports"
    if regime_dir.exists():
        # Walk date dirs in reverse to find most recent
        date_dirs = sorted(regime_dir.iterdir(), reverse=True) if regime_dir.is_dir() else []
        for dd in date_dirs[:3]:
            if not dd.is_dir():
                continue
            # Latest regime file
            regime_files = sorted(dd.glob("regime_*.json"), reverse=True)
            if regime_files and not claude_regime:
                claude_regime = _load_json(regime_files[0])
                claude_regime["_file_age"] = _file_age_hours(regime_files[0])
            # Latest screener file
            screener_files = sorted(dd.glob("screener_*.json"), reverse=True)
            if screener_files and not claude_screener:
                claude_screener = _load_json(screener_files[0])
            # Latest VCP free file
            vcp_files = sorted(dd.glob("vcp_free_*.json"), reverse=True)
            if vcp_files and not claude_screener:
                claude_screener = _load_json(vcp_files[0])
            if claude_regime:
                break

    if claude_regime or cb_result:
        regime_name = claude_regime.get("regime", "NEUTRAL") if claude_regime else "NEUTRAL"
        signals = claude_regime.get("signals", {}) if claude_regime else {}
        exposure = claude_regime.get("exposure_recommendation", "NORMAL") if claude_regime else "NORMAL"
        confidence = claude_regime.get("confidence", 0) if claude_regime else 0

        # Extract screener candidates
        screener_candidates = []
        trend_passes = []
        if claude_screener:
            cands = claude_screener.get("candidates", {})
            if isinstance(cands, dict):
                for name, data in cands.items():
                    if isinstance(data, dict):
                        for c in data.get("candidates", [])[:10]:
                            screener_candidates.append(c)
                        for t in data.get("trend_passes", [])[:20]:
                            trend_passes.append(t)
            elif isinstance(cands, list):
                screener_candidates = cands[:10]
            trend_passes = claude_screener.get("trend_passes", trend_passes)[:20]

        # Trading data from result file (real portfolio values)
        cb_holdings = cb_result.get("holdings", []) if cb_result else []
        cb_holdings_value = sum(h.get("value", 0) for h in cb_holdings)
        cb_cost_basis = _cost_basis(cb_holdings)
        cb_cash = cb_result.get("cash_balance", 0) if cb_result else 0
        cb_profit = cb_result.get("total_profit", 0) if cb_result else 0
        cb_paper = cb_session.get("paper_trading", False)

        strategies.append({
            "id": "claude_bot",
            "name": "AI Market Intelligence",
            "currency": "USD",
            "mode": "paper" if cb_paper else "live",
            "value": cb_holdings_value,
            "cost_basis": cb_cost_basis,
            "cash": cb_cash,
            "profit": cb_profit,
            "holdings": cb_holdings,
            "budget": (cb_result.get("allocated_capital") if cb_result else 0) or cap_mgmt.get("claude_trading_bot", {}).get("budget_usd", 0),
            "extra": {
                "regime": regime_name,
                "exposure": exposure,
                "confidence": confidence,
                "breadth_score": signals.get("breadth_score", 0),
                "uptrend_score": signals.get("uptrend_score", 0),
                "top_probability": signals.get("top_probability", 0),
                "ftd_active": signals.get("ftd_active", False),
                "screener_candidates": screener_candidates,
                "trend_passes": trend_passes,
                "trend_pass_count": len(trend_passes),
                "vcp_count": len(screener_candidates),
            },
            "stale_hours": _file_age_hours(RESULTS_DIR / "claude_trading_bot_result.json") if cb_result else (claude_regime.get("_file_age", 999) if claude_regime else 999),
            "timestamp": cb_result.get("timestamp", "") if cb_result else (claude_regime.get("timestamp", "") if claude_regime else ""),
        })

    # ── Realized P&L + derived metrics ──
    # Aggregated YTD view per strategy — see scripts/aggregate_realized_pnl.py.
    # "profit" stays as the unrealized view (holdings value − cost basis).
    # New fields: realized_ytd, trades_ytd, wins_ytd, losses_ytd, win_rate_pct,
    # mdd_pct, total_pl_ytd, profit_rate_ytd_pct.
    realized = _load_json(RESULTS_DIR / "realized_pl_2026.json") or {}
    realized_by_key = realized.get("strategies", {}) if isinstance(realized, dict) else {}

    # Dashboard id → aggregator key
    REALIZED_KEY_BY_ID = {
        "btc_vb": "BTC_VB",
        "korea_etf": "KOREA_ETF",
        "quant40": "QUANT40",
        "jd_strategy": "JD_STRATEGY",
        "dual_momentum": "DUAL_MOMENTUM",
        "claude_bot": "CLAUDE_AI_BOT",
    }

    for s in strategies:
        if s.get("id") == "hybrid_vb":
            # Two independent legs (KR + US). Each leg gets its own enrichment.
            kr_r = realized_by_key.get("HYBRID_VB_KR", {})
            us_r = realized_by_key.get("HYBRID_VB_US", {})

            for leg_key, leg_r, leg_budget_key in (
                ("kr", kr_r, "budget_kr"),
                ("us", us_r, "budget_us"),
            ):
                leg = s.get(leg_key)
                if not isinstance(leg, dict):
                    leg = {}
                _enrich(leg, leg.get("profit", 0), s.get(leg_budget_key, 0), leg_r)
                s[leg_key] = leg

            # Roll-up cycles + realized for header-style display if needed
            s["realized_trades"] = (kr_r.get("trades_ytd", 0) or 0) + (us_r.get("trades_ytd", 0) or 0)
            s["cycles_ytd"] = s["realized_trades"]
            # MDD for hybrid: show worst of the two (more conservative)
            mdds = [m for m in (kr_r.get("mdd_pct"), us_r.get("mdd_pct")) if m is not None]
            s["mdd_pct"] = min(mdds) if mdds else None
        else:
            agg_key = REALIZED_KEY_BY_ID.get(s.get("id"))
            r = realized_by_key.get(agg_key, {}) if agg_key else {}
            _enrich(s, s.get("profit", 0), s.get("budget", 0), r)

    # ── Save daily equity snapshot ──
    _save_equity_snapshot(strategies)

    # ── Equity history for charts ──
    equity_series = _get_equity_series()

    # Also include BTC VB equity_history from its state file
    if equity_hist:
        btc_series = [[d, v] for d, v in equity_hist if d >= EQUITY_START_DATE]
        if btc_series:
            equity_series["btc_vb"] = btc_series

    # ── Log Events ──
    feed_events = []
    for log_name in [
        "upbit_vb_strategy.log",
        "korea_etf_momentum_stderr.log",
        "hybrid_vb_kr_stderr.log",
        "hybrid_vb_us_stderr.log",
    ]:
        feed_events.extend(_parse_log_events(ROOT / log_name, max_lines=300))

    # Sort by timestamp descending, keep latest 30
    feed_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    feed_events = feed_events[:30]

    # ── Meta ──
    now_kst = datetime.now(KST)

    return {
        "meta": {
            "timestamp": now_kst.isoformat(),
            "kst": now_kst.strftime("%Y-%m-%d %H:%M KST"),
            "equity_start": EQUITY_START_DATE,
        },
        "strategies": strategies,
        "equity_series": equity_series,
        "realized_pl": realized,
        "feed": feed_events,
    }


# ── HTTP Server ───────────────────────────────────────────────

class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/data":
            data = build_dashboard_data()
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path in ("/", "/index.html"):
            html_path = ROOT / "dashboard.html"
            if html_path.exists():
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "dashboard.html not found")
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Quieter logs
        pass


def main():
    port = 8077
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = http.server.HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running at http://0.0.0.0:{port}")
    print(f"  Data endpoint: http://0.0.0.0:{port}/api/data")
    print(f"  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
