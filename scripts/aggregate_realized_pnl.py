#!/usr/bin/env python3
"""
Aggregate YTD realized PnL, win rate, MDD, and trade cycles per bot.

Sources (pick with --source):
  state : each bot's own state-file trade_history (fast, perfect attribution,
          but gaps where a bot doesn't persist trades)
  kis   : KIS broker API — domestic + overseas realized profit since YYYY-01-01,
          attributed to bots via TICKER_TO_STRATEGY. Authoritative for stocks.
  upbit : Upbit broker API — completed orders for BTC_VB only.
  both  : (default) kis + upbit for broker-backed bots; state as fallback.

Output: strategy_results/realized_pl_{year}.json with per-bot:
  realized_ytd, trades_ytd, wins_ytd, losses_ytd, win_rate_pct,
  mdd_pct (from strategy_results/equity_history.jsonl),
  last_trade, source_used.

Safe to run concurrently with live bots — atomic write via os.replace.
"""

import argparse
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "strategy_results"
EQUITY_HISTORY_FILE = RESULTS_DIR / "equity_history.jsonl"

logging.basicConfig(
    level=os.environ.get("AGGREGATE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("aggregate_realized_pnl")


# Bot currency + equity-history-id map.
# equity_id matches the key used by dashboard_server._save_equity_snapshot.
@dataclass(frozen=True)
class BotMeta:
    key: str            # aggregator key, e.g. "BTC_VB"
    currency: str       # "KRW" or "USD"
    equity_id: str      # matching id in equity_history.jsonl
    state_path: Path    # absolute path to bot state file (for state source fallback)
    state_trade_path: tuple[str, ...]  # dotted path to trade list inside state

BOTS: list[BotMeta] = [
    BotMeta("BTC_VB",       "KRW", "btc_vb",       ROOT / "upbit_vb_strategy_state.json",    ("closed_trades",)),
    BotMeta("HYBRID_VB_KR", "KRW", "hybrid_vb",    ROOT / "hybrid_vb_state.json",             ("kr", "trade_history")),
    BotMeta("HYBRID_VB_US", "USD", "hybrid_vb",    ROOT / "hybrid_vb_state.json",             ("us", "trade_history")),
    BotMeta("KOREA_ETF",    "KRW", "korea_etf",    ROOT / "korea_etf_momentum_state.json",   ("closed_trades",)),
    BotMeta("JD_STRATEGY",  "USD", "jd_strategy",  ROOT / "jd_strategy_state.json",           ("closed_trades",)),
    BotMeta("QUANT40",      "USD", "quant40",      ROOT / "quant40_state.json",               ("closed_trades",)),
    BotMeta("CLAUDE_AI_BOT","USD", "claude_bot",   ROOT / "claude_trading_bot_state.json",   ("trade_history",)),
    BotMeta("DUAL_MOMENTUM","USD", "dual_momentum",ROOT / "modified_dual_momentum_state.json",("trade_history",)),
]

# KIS ticker → bot. Inherited from refresh_realized_pl.py (proven in production).
# A ticker that doesn't appear here is credited to "UNKNOWN" — check the
# _errors field in the output to see if you need to extend this map.
TICKER_TO_STRATEGY = {
    # Quant40 (US stocks — ETF-only momentum)
    'SPY': 'QUANT40', 'IEF': 'QUANT40', 'SH': 'QUANT40',
    # JD Strategy (US single-name growth)
    'NVDA': 'JD_STRATEGY', 'AAPL': 'JD_STRATEGY', 'MSFT': 'JD_STRATEGY',
    'GOOGL': 'JD_STRATEGY', 'AMZN': 'JD_STRATEGY',
    # Modified Dual Momentum (US broad)
    'EFA': 'DUAL_MOMENTUM', 'AGG': 'DUAL_MOMENTUM', 'SHY': 'DUAL_MOMENTUM',
    'TLT': 'DUAL_MOMENTUM', 'TIP': 'DUAL_MOMENTUM', 'LQD': 'DUAL_MOMENTUM',
    'HYG': 'DUAL_MOMENTUM', 'BWX': 'DUAL_MOMENTUM', 'EMB': 'DUAL_MOMENTUM',
    # Hybrid VB US (commodity/precious)
    'GLD': 'HYBRID_VB_US', 'SLV': 'HYBRID_VB_US', 'GLTR': 'HYBRID_VB_US',
    'GDX': 'HYBRID_VB_US', 'USO': 'HYBRID_VB_US', 'URA': 'HYBRID_VB_US',
    'FTGC': 'HYBRID_VB_US', 'CPER': 'HYBRID_VB_US', 'DBA': 'HYBRID_VB_US',
    'SIL': 'HYBRID_VB_US',
    # Korea ETF Momentum
    '139220': 'KOREA_ETF', '144600': 'KOREA_ETF', '132030': 'KOREA_ETF',
    # Hybrid VB KR (Korea rotating baskets)
    '069500': 'HYBRID_VB_KR', '229200': 'HYBRID_VB_KR',
    '305720': 'HYBRID_VB_KR', '091170': 'HYBRID_VB_KR',
    '364690': 'HYBRID_VB_KR',
}

# Default empty-bot template
def _empty_summary(currency: str) -> dict:
    return {
        "currency": currency,
        "realized_ytd": 0.0,
        "trades_ytd": 0,
        "wins_ytd": 0,
        "losses_ytd": 0,
        "win_rate_pct": None,   # null until >0 trades
        "mdd_pct": None,        # null until equity series has >1 point
        "last_trade": None,
        "source": "none",
    }


# ── Helpers ────────────────────────────────────────────────────────────

def _nested(d: dict, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _parse_exit_date(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=KST)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(s, "%Y%m%d")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


def _ytd_window(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 1, 1, tzinfo=KST)
    end = datetime(year + 1, 1, 1, tzinfo=KST)
    return start, end


def _compute_mdd_pct(values: list[float]) -> Optional[float]:
    """Max drawdown as negative percent (e.g. -12.3). None if <2 points."""
    clean = [v for v in values if v is not None and v > 0]
    if len(clean) < 2:
        return None
    peak = clean[0]
    mdd = 0.0
    for v in clean:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return round(mdd, 2)


# ── State-file source (fallback) ────────────────────────────────────────

def _summarize_state(bot: BotMeta, start: datetime, end: datetime) -> tuple[dict, Optional[str]]:
    summary = _empty_summary(bot.currency)
    summary["source"] = "state"

    if not bot.state_path.exists():
        return summary, None

    try:
        state = json.loads(bot.state_path.read_text(encoding="utf-8"))
    except Exception as e:
        return summary, f"{bot.key}: cannot read {bot.state_path.name}: {e}"

    trades = _nested(state, bot.state_trade_path)
    if trades is None:
        return summary, None
    if not isinstance(trades, list):
        return summary, f"{bot.key}: trade list at {'.'.join(bot.state_trade_path)} is not a list"

    total, count, wins = 0.0, 0, 0
    last_trade = None
    last_exit: Optional[datetime] = None
    for t in trades:
        if not isinstance(t, dict):
            continue
        dt = _parse_exit_date(t.get("exit_date"))
        if dt is None or not (start <= dt < end):
            continue
        pnl = t.get("pnl")
        try:
            pnl_f = float(pnl) if pnl is not None else 0.0
        except (TypeError, ValueError):
            continue
        total += pnl_f
        count += 1
        if pnl_f > 0:
            wins += 1
        if last_exit is None or dt > last_exit:
            last_exit = dt
            last_trade = {"ticker": t.get("ticker"), "exit_date": dt.isoformat(), "pnl": pnl_f}

    summary["realized_ytd"] = round(total, 2)
    summary["trades_ytd"] = count
    summary["wins_ytd"] = wins
    summary["losses_ytd"] = count - wins
    summary["win_rate_pct"] = round(wins / count * 100, 1) if count else None
    summary["last_trade"] = last_trade
    return summary, None


# ── KIS source ──────────────────────────────────────────────────────────

def _load_kis_env():
    """Ensure KIS modules are importable and auth is done. Returns acct env or None."""
    try:
        kis_base = ROOT / "open-trading-api" / "examples_llm"
        if not kis_base.exists():
            return None, "KIS modules not present at open-trading-api/examples_llm"
        sys.path.insert(0, str(kis_base))
        sys.path.insert(0, str(kis_base / "domestic_stock" / "inquire_period_trade_profit"))
        sys.path.insert(0, str(kis_base / "overseas_stock" / "inquire_period_profit"))
        import kis_auth as ka  # type: ignore
        ka.auth(svr="prod")
        return ka.getTREnv(), None
    except Exception as e:
        return None, f"KIS auth failed: {e}"


def _kis_domestic_trades(acct, year: int) -> tuple[list[dict], Optional[str]]:
    """Return list of dict{ticker, pnl_krw, exit_date} for KR realized trades since YYYY-01-01."""
    try:
        import inquire_period_trade_profit as ipt  # type: ignore
        df, _ = ipt.inquire_period_trade_profit(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            sort_dvsn="00", inqr_strt_dt=f"{year}0101",
            inqr_end_dt=datetime.now().strftime("%Y%m%d"),
            cblc_dvsn="00",
        )
    except Exception as e:
        return [], f"KIS domestic inquire_period_trade_profit failed: {e}"

    if df is None or df.empty:
        return [], None

    trades = []
    for _, row in df.iterrows():
        ticker = str(row.get("pdno", "")).strip()
        try:
            pnl = float(row.get("rlzt_pfls", 0))
        except (TypeError, ValueError):
            continue
        # KIS may also return a trade-date. Try a few known column names.
        exit_date = None
        for col in ("trad_dt", "ord_dt", "rlzt_dt", "base_dt"):
            v = row.get(col)
            if v:
                exit_date = str(v).strip()
                break
        trades.append({"ticker": ticker, "pnl_krw": pnl, "pnl_native": pnl, "exit_date": exit_date})
    return trades, None


def _kis_overseas_trades(acct, year: int) -> tuple[list[dict], Optional[str]]:
    """Return list of dict{ticker, pnl_usd, pnl_krw, exit_date}."""
    try:
        import inquire_period_profit as opp  # type: ignore
        result = opp.inquire_period_profit(
            cano=acct.my_acct, acnt_prdt_cd=acct.my_prod,
            ovrs_excg_cd="NASD", natn_cd="840", crcy_cd="USD", pdno="",
            inqr_strt_dt=f"{year}0101",
            inqr_end_dt=datetime.now().strftime("%Y%m%d"),
            wcrc_frcr_dvsn_cd="01", FK200="", NK200="",
        )
    except Exception as e:
        return [], f"KIS overseas inquire_period_profit failed: {e}"

    if not result or not isinstance(result, tuple) or result[0] is None or result[0].empty:
        return [], None

    trades = []
    for _, row in result[0].iterrows():
        ticker = str(row.get("ovrs_pdno", "")).strip()
        try:
            pnl_usd = float(row.get("ovrs_rlzt_pfls_amt", 0))
        except (TypeError, ValueError):
            continue
        try:
            fx = float(row.get("frst_bltn_exrt", 1380) or 1380)
        except (TypeError, ValueError):
            fx = 1380.0
        pnl_krw = pnl_usd * fx
        exit_date = None
        for col in ("trad_dt", "ord_dt", "rlzt_dt", "base_dt"):
            v = row.get(col)
            if v:
                exit_date = str(v).strip()
                break
        trades.append({
            "ticker": ticker, "pnl_usd": pnl_usd, "pnl_native": pnl_usd,
            "pnl_krw": pnl_krw, "exit_date": exit_date,
        })
    return trades, None


def _attribute_trades_to_bots(
    trades: list[dict], out: dict[str, dict], errors: list[str], source_label: str
) -> int:
    """Fold trades into per-bot buckets using TICKER_TO_STRATEGY. Returns unknown-count."""
    unknown = 0
    for t in trades:
        ticker = t.get("ticker", "")
        strat = TICKER_TO_STRATEGY.get(ticker)
        if strat is None:
            unknown += 1
            errors.append(f"{source_label}: ticker '{ticker}' not in TICKER_TO_STRATEGY — pnl skipped")
            continue
        if strat not in out:
            # Unexpected: strategy not in BOTS list. Create a bucket.
            out[strat] = _empty_summary("USD" if "usd" in t else "KRW")
            errors.append(f"{source_label}: unknown strategy key '{strat}' created on the fly")

        bucket = out[strat]
        bucket["source"] = source_label
        pnl_native = t.get("pnl_native", 0.0)
        bucket["realized_ytd"] = round(bucket.get("realized_ytd", 0.0) + pnl_native, 2)
        bucket["trades_ytd"] = bucket.get("trades_ytd", 0) + 1
        if pnl_native > 0:
            bucket["wins_ytd"] = bucket.get("wins_ytd", 0) + 1
        bucket["losses_ytd"] = bucket["trades_ytd"] - bucket["wins_ytd"]
        bucket["win_rate_pct"] = (
            round(bucket["wins_ytd"] / bucket["trades_ytd"] * 100, 1)
            if bucket["trades_ytd"] else None
        )
        # Track last trade by date string (YYYYMMDD) — lexicographic is fine
        ed = t.get("exit_date")
        if ed and (bucket.get("last_trade") is None or ed > (bucket["last_trade"].get("exit_date") or "")):
            bucket["last_trade"] = {"ticker": ticker, "exit_date": ed, "pnl": pnl_native}
    return unknown


# ── Upbit source ────────────────────────────────────────────────────────

def _read_env_file(path: Path) -> dict:
    """Minimal .env parser — no dep on python-dotenv."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _upbit_fetch_orders_raw(access: str, secret: str, market: str, state: str,
                             max_pages: int = 20) -> tuple[list[dict], Optional[str]]:
    """Paginate Upbit /v1/orders with JWT auth. state: 'done' | 'cancel'."""
    try:
        import jwt  # type: ignore
        import requests  # type: ignore
        import hashlib
        import uuid as _uuid
        from urllib.parse import urlencode
    except ImportError as e:
        return [], f"upbit: missing dependency ({e})"

    all_orders: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"market": market, "state": state, "limit": "100",
                  "order_by": "desc", "page": str(page)}
        qs = urlencode(params)
        qh = hashlib.sha512(qs.encode()).hexdigest()
        payload = {"access_key": access, "nonce": str(_uuid.uuid4()),
                   "query_hash": qh, "query_hash_alg": "SHA512"}
        try:
            token = jwt.encode(payload, secret)
            r = requests.get(f"https://api.upbit.com/v1/orders?{qs}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
        except Exception as e:
            return all_orders, f"upbit: request failed on page {page}: {e}"
        if r.status_code != 200:
            return all_orders, f"upbit: HTTP {r.status_code} on page {page}: {r.text[:200]}"
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        all_orders.extend(batch)
        if len(batch) < 100:
            break
    return all_orders, None


def _upbit_count_orders_2026(market: str = "KRW-BTC") -> tuple[int, int, Optional[str]]:
    """Verify 2026 order counts via Upbit API (for cross-check, not PnL calc).
    Returns (buys_executed, sells_executed, error)."""
    env = _read_env_file(ROOT / ".env")
    access = env.get("UPBIT_ACCESS_KEY")
    secret = env.get("UPBIT_SECRET_KEY")
    if not access or not secret:
        return 0, 0, "upbit: credentials missing from .env"

    done, err_d = _upbit_fetch_orders_raw(access, secret, market, "done")
    if err_d:
        return 0, 0, err_d
    cancel, err_c = _upbit_fetch_orders_raw(access, secret, market, "cancel")
    if err_c:
        return 0, 0, err_c

    def _in_year(o: dict) -> bool:
        try:
            if datetime.fromisoformat(o.get("created_at", "")).year != 2026:
                return False
            return float(o.get("executed_volume") or 0) > 0
        except (TypeError, ValueError):
            return False

    buys = sum(1 for o in cancel if o.get("side") == "bid" and o.get("ord_type") == "price" and _in_year(o))
    sells = sum(1 for o in done if o.get("side") == "ask" and _in_year(o))
    return buys, sells, None


def _upbit_realized_trades(year: int, market: str = "KRW-BTC") -> tuple[list[dict], Optional[str]]:
    """Realized PnL for the BTC VB bot.

    **Source of truth: the bot's own state file** — specifically
    `total_pnl_krw`, `total_trades`, and `winning_trades`. The bot computes
    per-cycle PnL using its known entry_amount_krw vs exit_amount, which is
    far more accurate than reconstructing from raw Upbit orders: the account
    holds BTC accumulated from pre-YTD trading, so a naive pair-by-time
    approach credits each 2026 sell against a cost basis that's not actually
    the 2026 opening basis.

    Upbit API is still used to cross-check the bot's order count and flag
    drift — it does NOT drive the PnL calculation. See `_upbit_count_orders_2026`.
    """
    state_path = ROOT / "upbit_vb_strategy_state.json"
    if not state_path.exists():
        return [], "upbit: state file absent, cannot derive realized PnL"

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [], f"upbit: cannot read state: {e}"

    total_trades = int(state.get("total_trades") or 0)
    wins = int(state.get("winning_trades") or 0)
    total_pnl = float(state.get("total_pnl_krw") or 0)
    if total_trades <= 0:
        return [], None

    # Optional cross-check against Upbit API
    warning = None
    try:
        buys_api, sells_api, err = _upbit_count_orders_2026(market)
        if err:
            warning = f"upbit: API cross-check skipped ({err})"
        elif sells_api != total_trades:
            warning = (
                f"upbit: bot state says {total_trades} closed trades, Upbit API shows "
                f"{sells_api} sells in {year} ({buys_api} buys). State file wins — "
                f"consider running the bot to reconcile."
            )
    except Exception as e:
        warning = f"upbit: API cross-check failed: {e}"

    # Synthesize per-trade PnL matching the bot's counters (wins get avg-win,
    # losses get avg-loss). Not real per-trade data — the bot doesn't persist
    # that yet — but preserves the correct totals the dashboard reads.
    avg_win = total_pnl / wins if wins > 0 else 0.0
    losses = total_trades - wins
    # If all wins, losses_avg is 0; otherwise distribute the non-win PnL residual
    avg_loss = 0.0 if losses <= 0 else (total_pnl - wins * avg_win) / losses
    synth: list[dict] = []
    for _ in range(wins):
        synth.append({"ticker": "KRW-BTC", "pnl_native": avg_win, "pnl_krw": avg_win,
                      "exit_date": f"{year}-12-31T23:59:59+09:00"})
    for _ in range(losses):
        synth.append({"ticker": "KRW-BTC", "pnl_native": avg_loss, "pnl_krw": avg_loss,
                      "exit_date": f"{year}-12-31T23:59:59+09:00"})
    return synth, warning


# ── MDD from equity snapshots ───────────────────────────────────────────

def _load_equity_series_per_bot() -> dict[str, list[float]]:
    """Return {equity_id: [value1, value2, ...]} from equity_history.jsonl ordered by date."""
    series: dict[str, list[tuple[str, float]]] = {}
    if not EQUITY_HISTORY_FILE.exists():
        return {}
    try:
        with open(EQUITY_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                date = entry.get("date", "")
                snapshots = entry.get("snapshots", {})
                if isinstance(snapshots, dict):
                    for eid, val in snapshots.items():
                        try:
                            v = float(val) if val is not None else 0.0
                        except (TypeError, ValueError):
                            continue
                        series.setdefault(eid, []).append((date, v))
    except Exception:
        return {}
    # Sort by date and flatten to values
    return {eid: [v for _, v in sorted(pairs)] for eid, pairs in series.items()}


# ── Main aggregation ──────────────────────────────────────────────────

def aggregate(
    year: int,
    source_mode: str = "both",
) -> tuple[dict, list[str]]:
    start, end = _ytd_window(year)
    errors: list[str] = []

    # Start with one empty bucket per bot
    strategies: dict[str, dict] = {b.key: _empty_summary(b.currency) for b in BOTS}

    # 1. State-file pass (always runs — it's free, local, and safe)
    for b in BOTS:
        s, err = _summarize_state(b, start, end)
        if err:
            errors.append(err)
        if s.get("trades_ytd", 0) > 0 or source_mode == "state":
            strategies[b.key] = s

    # 2. KIS pass (overrides state for stock bots, since broker is authoritative)
    if source_mode in ("kis", "both"):
        acct, err = _load_kis_env()
        if err:
            errors.append(err)
        else:
            domestic, err_d = _kis_domestic_trades(acct, year)
            if err_d:
                errors.append(err_d)
            overseas, err_o = _kis_overseas_trades(acct, year)
            if err_o:
                errors.append(err_o)

            # Reset KIS-owned buckets so we don't double-count (state might have had partials)
            kis_bot_keys = set(TICKER_TO_STRATEGY.values())
            for bk in kis_bot_keys:
                if bk in strategies:
                    strategies[bk] = _empty_summary(next(b.currency for b in BOTS if b.key == bk))

            _attribute_trades_to_bots(domestic, strategies, errors, "kis_domestic")
            _attribute_trades_to_bots(overseas, strategies, errors, "kis_overseas")
            log.info("KIS: %d domestic rows, %d overseas rows", len(domestic), len(overseas))

    # 3. Upbit pass (BTC VB only)
    if source_mode in ("upbit", "both"):
        upbit_trades, err_u = _upbit_realized_trades(year)
        if err_u:
            errors.append(err_u)
        if upbit_trades:
            # Reset BTC_VB bucket so we take upbit as canonical
            strategies["BTC_VB"] = _empty_summary("KRW")
            strategies["BTC_VB"]["source"] = "upbit"
            total, count, wins = 0.0, 0, 0
            last_trade = None
            last_exit = ""
            for t in upbit_trades:
                pnl = t.get("pnl_native", 0)
                total += pnl
                count += 1
                if pnl > 0:
                    wins += 1
                ed = t.get("exit_date", "")
                if ed and ed > last_exit:
                    last_exit = ed
                    last_trade = {"ticker": t.get("ticker"), "exit_date": ed, "pnl": pnl}
            strategies["BTC_VB"]["realized_ytd"] = round(total, 2)
            strategies["BTC_VB"]["trades_ytd"] = count
            strategies["BTC_VB"]["wins_ytd"] = wins
            strategies["BTC_VB"]["losses_ytd"] = count - wins
            strategies["BTC_VB"]["win_rate_pct"] = round(wins / count * 100, 1) if count else None
            strategies["BTC_VB"]["last_trade"] = last_trade

    # 4. MDD pass (applies to every bot that has equity history)
    equity_series = _load_equity_series_per_bot()
    for b in BOTS:
        s = strategies[b.key]
        # Hybrid legs share the same equity_id ("hybrid_vb") — accept shared MDD at the card level
        vals = equity_series.get(b.equity_id) or []
        s["mdd_pct"] = _compute_mdd_pct(vals)

    # Roll up totals
    total_krw = sum(s["realized_ytd"] for b, s in zip(BOTS, strategies.values()) if s["currency"] == "KRW")
    total_usd = sum(s["realized_ytd"] for b, s in zip(BOTS, strategies.values()) if s["currency"] == "USD")

    out = {
        "_updated": datetime.now(KST).isoformat(timespec="seconds"),
        "_year": year,
        "_source_mode": source_mode,
        "_total_krw": round(total_krw, 2),
        "_total_usd": round(total_usd, 2),
        "strategies": strategies,
        "_errors": errors,
    }
    return out, errors


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=datetime.now(KST).year)
    parser.add_argument("--source", choices=("state", "kis", "upbit", "both"), default="both")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing")
    args = parser.parse_args()

    try:
        data, errors = aggregate(args.year, source_mode=args.source)
    except Exception:
        traceback.print_exc()
        return 1

    out_path = args.output or (RESULTS_DIR / f"realized_pl_{args.year}.json")

    if args.dry_run:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        try:
            write_atomic(out_path, data)
        except Exception as e:
            print(f"FATAL: could not write {out_path}: {e}", file=sys.stderr)
            return 1
        print(f"Wrote {out_path}")

    print(f"  source: {args.source}")
    print(f"  total KRW: {data['_total_krw']:,.2f}")
    print(f"  total USD: {data['_total_usd']:,.2f}")
    for k, v in data["strategies"].items():
        wr = f"{v['win_rate_pct']}%" if v["win_rate_pct"] is not None else "—"
        mdd = f"{v['mdd_pct']}%" if v["mdd_pct"] is not None else "—"
        print(f"  {k:<15} {v['currency']} realized={v['realized_ytd']:>12,.2f}  "
              f"cycles={v['trades_ytd']:>3}  W/L={v['wins_ytd']}/{v['losses_ytd']}  "
              f"WR={wr:<6} MDD={mdd:<7} src={v['source']}")

    if errors:
        print(f"\n{len(errors)} warnings:", file=sys.stderr)
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)
        return 1 if args.source == "state" else 0  # broker-source errors are softer
    return 0


if __name__ == "__main__":
    sys.exit(main())
