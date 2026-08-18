#!/usr/bin/env python3
"""Dashboard → Discord: one compact, glanceable card.

House style is shared with ``automation_oracle/discord_notifier.py``: the
description carries everything, anything columnar lives inside a ``` fence
(Discord draws unfenced text proportionally, so space-alignment is simply
ignored outside one), emoji stay out of it, and the accent colour is muted.

Numbers come from the same fields the dashboard hero renders —
``totals.investments_krw`` for the headline and ``portfolio.total_profit_krw``
for P/L — so the message and the page cannot drift apart. The previous layout
re-derived P/L from the per-strategy list and disagreed with the page whenever
a bot went stale.

Run with ``--dry-run`` to print the card instead of posting it.
"""

import json
import os
import sys
from datetime import date, datetime

import requests

TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL = "https://estherrunstrict.github.io/quanty"

# ── Asset-management goals ────────────────────────────────────────────────
# Funding goal: hold enough cash in Toss that NMF2 can actually deploy the
# budget the allocator already granted it. Env-overridable so the deadline or
# the amount can move without a code edit.
TOSS_DEADLINE = os.environ.get("QUANTY_TOSS_DEADLINE", "2026-09-01")
TOSS_TARGET_KRW = os.environ.get("QUANTY_TOSS_TARGET_KRW")  # blank = derive
# Cash above this share of investments reads as drag rather than dry powder.
CASH_TARGET_PCT = float(os.environ.get("QUANTY_CASH_TARGET_PCT", "35"))

# Table labels. Full bot names run past 20 chars and blow the column out on a
# phone; the id is the stable key, so the mapping lives here rather than in the
# generator.
SHORT_NAME = {
    "btc_vb": "BTC VB",
    "korea_etf": "Korea ETF",
    "quant40": "Quant40",
    "jd_strategy": "JD",
    "dual_momentum": "DualMom",
    "hybrid_vb": "HybridVB",
    "claude_bot": "Claude",
    "nmf2": "NMF2",
}

COLOR_UP = 0x3A8A5A
COLOR_FLAT = 0x4A78A8
COLOR_DOWN = 0xA84848
COLOR_WARN = 0xC9A24D


# ── Formatting ────────────────────────────────────────────────────────────
def _dw(s):
    """Display width in a monospace font. Hangul takes two cells, so counting
    with len() makes the columns drift."""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def _pad(s, width, right=False):
    gap = max(0, width - _dw(s))
    return (" " * gap + str(s)) if right else (str(s) + " " * gap)


def _kw(v, signed=False):
    """KRW at a glance: 117,230 -> '11.7만', 184,930,248 -> '1.85억'."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if v < 0 else ("+" if signed else "")
    a = abs(v)
    if a >= 1e8:
        return f"{sign}{a / 1e8:,.2f}억"
    if a >= 1e4:
        return f"{sign}{a / 1e4:,.0f}만"
    return f"{sign}{a:,.0f}"


def _tbl(rows, left=(0,)):
    """Fixed-width table inside a fence. Numeric columns are right-aligned —
    that is what makes the digits line up vertically — while columns named in
    ``left`` stay flush left, because right-aligned prose reads as ragged."""
    if not rows:
        return []
    n = len(rows[0])
    w = [max(_dw(r[i]) for r in rows) for i in range(n)]
    out = ["```"]
    for r in rows:
        out.append(" ".join(
            _pad(c, w[i], right=(i not in left)) for i, c in enumerate(r)).rstrip())
    out.append("```")
    return out


def _dot(v):
    return "▲" if (v or 0) >= 0 else "▼"


# ── Data shaping ──────────────────────────────────────────────────────────
def strategy_rows(data, rate):
    """One row per bot: label, value, P/L, return. The manual sleeve is left
    out — it is not a bot, and the header already reports it as its own slice."""
    rows = []
    for s in data.get("strategies", []):
        sid = s.get("id")
        if sid == "manual":
            continue

        if s.get("currency") == "MULTI":
            kr, us = s.get("kr") or {}, s.get("us") or {}
            value = (kr.get("value") or 0) + (us.get("value") or 0) * rate
            pl = (kr.get("total_pl_ytd") or kr.get("unrealized_profit") or 0)
            pl += (us.get("total_pl_ytd") or us.get("unrealized_profit") or 0) * rate
            budget = (s.get("budget_kr") or 0) + (s.get("budget_us") or 0) * rate
        else:
            mult = rate if s.get("currency") == "USD" else 1
            value = (s.get("value") or 0) * mult
            raw_pl = s.get("total_pl_ytd")
            if raw_pl is None:
                raw_pl = s.get("unrealized_profit") or s.get("profit") or 0
            pl = raw_pl * mult
            budget = (s.get("budget") or 0) * mult

        pct = s.get("profit_rate_ytd_pct")
        if pct is None:
            # Prefer the granted budget as the base; fall back to implied cost.
            base = budget if budget > 0 else (value - pl)
            pct = (pl / base * 100) if base > 0 else None
        # A bot reporting no position cannot state a return — korea_etf
        # publishes value 0 while carrying a realised loss, which would render
        # as a meaningless -100%.
        if value <= 0:
            pct = None

        rows.append({
            "label": SHORT_NAME.get(sid, (s.get("name") or sid or "?")[:10]),
            "value": value,
            "pl": pl,
            "pct": pct,
        })

    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def build_todos(data, today):
    """Asset-management action list, derived from the same JSON — so it cannot
    recommend something the numbers do not support."""
    todos = []
    totals = data.get("totals") or {}
    accounts = data.get("accounts") or {}
    split = totals.get("split_pct") or {}

    # 1) Fund Toss by the deadline. NMF2 is the only bot on Toss, and it can
    #    only buy with cash actually sitting in that account.
    try:
        deadline = datetime.strptime(TOSS_DEADLINE, "%Y-%m-%d").date()
    except ValueError:
        deadline = None
    if deadline and today <= deadline:
        toss_cash = (accounts.get("toss") or {}).get("cash_krw") or 0
        nmf2 = next((s for s in data.get("strategies", [])
                     if s.get("id") == "nmf2"), {})
        if TOSS_TARGET_KRW:
            gap = float(TOSS_TARGET_KRW) - toss_cash
            why = "target"
        else:
            gap = (nmf2.get("budget") or 0) - (nmf2.get("value") or 0) - toss_cash
            why = "NMF2 budget"
        left = (deadline - today).days
        if gap > 0:
            todos.append(("Fund Toss", _kw(gap),
                          f"D-{left} · {_kw(gap / max(left, 1))}/d · {why}"))
        else:
            todos.append(("Fund Toss", "done", f"D-{left} · {why} covered"))

    # 2) Idle cash. Nearly half the book is uninvested; the CMA is where it sits.
    cash_pct = split.get("cash") or 0
    if cash_pct > CASH_TARGET_PCT:
        invested = totals.get("investments_krw") or 0
        excess = (totals.get("cash_krw") or 0) - invested * CASH_TARGET_PCT / 100
        cma = (accounts.get("cma") or {}).get("total_krw") or 0
        todos.append(("Deploy cash", f"{cash_pct:.1f}%",
                      f"{_kw(excess)} over {CASH_TARGET_PCT:.0f}% · CMA {_kw(cma)}"))

    # 3) Reconciliation gap — the split is only as good as this number.
    if totals.get("reconciliation_warning"):
        todos.append((
            "Recon gap",
            _kw(abs(totals.get("reconciliation_gap_krw") or 0)),
            f"{totals.get('reconciliation_gap_pct') or 0:.2f}% unexplained",
        ))

    # 4) Any sleeve the generator could not see today.
    stale = [k.upper() for k, a in accounts.items() if a.get("stale")]
    if stale:
        todos.append(("Stale feed", ", ".join(stale), "snapshot did not refresh"))

    return todos[:4]


# ── Wiring ────────────────────────────────────────────────────────────────
def get_webhook_url():
    env_path = os.path.join(TRADING_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
    try:
        import yaml
        with open(os.path.join(TRADING_DIR, "config.yaml")) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("DISCORD_WEBHOOK_URL")
    except Exception:
        pass
    return None


def build_payload(data, today):
    totals = data.get("totals") or {}
    p = data.get("portfolio") or {}
    rate = totals.get("fx_rate") or data.get("exchange_rate") or 1415.2
    split = totals.get("split_pct") or {}

    invested = totals.get("investments_krw") or p.get("total_value_krw") or 0
    pl = p.get("total_profit_krw") or 0
    pl_pct = p.get("total_profit_pct") or 0
    deposit = p.get("original_deposit_krw") or 0

    lines = [
        f"**₩{invested:,.0f}**  ·  ≈ ${invested / rate:,.0f}",
        f"Bot P/L  {_dot(pl)} ₩{pl:+,.0f}  ({pl_pct:+.2f}% of deposits)",
        f"Bots {split.get('bots', 0):.1f}%  ·  "
        f"Hands-on {split.get('manual', 0):.1f}%  ·  "
        f"Cash {split.get('cash', 0):.1f}%",
    ]

    rows = [("Bot", "Value", "P/L", "Return")]
    for r in strategy_rows(data, rate):
        rows.append((
            r["label"],
            _kw(r["value"]),
            _kw(r["pl"], signed=True),
            f"{r['pct']:+.1f}%" if r["pct"] is not None else "—",
        ))
    lines += _tbl(rows)

    todos = build_todos(data, today)
    if todos:
        lines.append("**Action items**")
        lines += _tbl([("Task", "Amount", "Detail")] + todos, left=(0, 2))

    lines.append(f"[Open dashboard]({DASHBOARD_URL})")

    if any(t[0] in ("Recon gap", "Stale feed") for t in todos):
        color = COLOR_WARN
    elif pl_pct >= 0.5:
        color = COLOR_UP
    elif pl_pct <= -2.0:
        color = COLOR_DOWN
    else:
        color = COLOR_FLAT

    return {
        "embeds": [{
            "title": f"Quanty · {data.get('updated_at', '')}",
            "url": DASHBOARD_URL,
            "description": "\n".join(lines),
            "color": color,
            "footer": {"text": f"Deposits ₩{deposit:,.0f} · FX {rate:,.1f}"},
        }]
    }


def main():
    dry = "--dry-run" in sys.argv

    with open(os.path.join(DASHBOARD_DIR, "docs", "data",
                           "dashboard_data.json")) as f:
        data = json.load(f)

    payload = build_payload(data, date.today())

    if dry:
        e = payload["embeds"][0]
        print(e["title"])
        print(e["description"])
        print("—", e["footer"]["text"], "| color", hex(e["color"]))
        return

    webhook_url = get_webhook_url()
    if not webhook_url:
        print("No Discord webhook URL found")
        return
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code in (200, 204):
        print("Discord notification sent successfully")
    else:
        print(f"Discord notification failed: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
