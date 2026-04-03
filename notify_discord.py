#!/usr/bin/env python3
"""Send dashboard update notification to Discord — clean, minimal format."""

import json
import os
import requests

TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL = "https://estherrunstrict.github.io/quanty"


def get_webhook_url():
    # Try .env first
    env_path = os.path.join(TRADING_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
    # Fallback: config.yaml
    try:
        import yaml
        with open(os.path.join(TRADING_DIR, "config.yaml")) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("DISCORD_WEBHOOK_URL")
    except Exception:
        pass
    return None


def main():
    webhook_url = get_webhook_url()
    if not webhook_url:
        print("No Discord webhook URL found")
        return

    data_path = os.path.join(DASHBOARD_DIR, "docs", "data", "dashboard_data.json")
    with open(data_path) as f:
        data = json.load(f)

    p = data.get("portfolio", {})
    rate = data.get("exchange_rate", 1380)
    total_val = p.get("total_value_krw", 0)
    original = p.get("original_deposit_krw", 0)
    total_pl = p.get("total_profit_krw", 0)
    total_pct = p.get("total_profit_pct", 0)
    cash_krw = p.get("cash_krw", 0)
    cash_usd = p.get("cash_usd", 0)

    dot = "▲" if total_pl >= 0 else "▼"

    # Header line
    lines = [
        f"**₩{total_val:,.0f}**  {dot} ₩{total_pl:+,.0f} ({total_pct:+.1f}%)",
        f"Cash: ₩{cash_krw:,.0f}  |  ${cash_usd:,.0f}  |  Upbit: ₩{p.get('upbit_krw', 0):,.0f}",
        "",
    ]

    # Per-strategy
    for s in data.get("strategies", []):
        total = s.get("total_pl_krw", 0)
        pct = s.get("total_pl_pct", 0)
        realized = s.get("realized_pl_krw", 0)
        sdot = "▲" if total >= 0 else "▼"

        # Holdings inline
        hparts = []
        for h in s.get("holdings", [])[:3]:
            if h.get("quantity", 0) > 0:
                hparts.append(f"{h['ticker']}×{h['quantity']}")
        hold_str = "  ".join(hparts) if hparts else s.get("status", "—")

        r_str = f"  R:₩{realized:+,.0f}" if realized != 0 else ""
        lines.append(f"{sdot} **{s['name']}**  ₩{total:+,.0f} ({pct:+.1f}%){r_str}")

        # Win rate + MDD
        meta_parts = []
        wr = s.get("win_rate")
        tt = s.get("total_trades", 0)
        if wr is not None and tt > 0:
            meta_parts.append(f"WR:{wr:.0f}%({tt}t)")
        mdd_val = s.get("mdd")
        if mdd_val is not None:
            meta_parts.append(f"MDD:{mdd_val:.1f}%")
        meta_str = " | ".join(meta_parts)

        if meta_str:
            lines.append(f"    {hold_str}  |  {meta_str}")
        else:
            lines.append(f"    {hold_str}")

    lines.append(f"\n[View Dashboard]({DASHBOARD_URL})")

    # Color based on P&L
    if total_pct >= 0.5:
        color = 0x3a8a5a
    elif total_pct <= -2.0:
        color = 0xa84848
    else:
        color = 0x4a78a8

    payload = {
        "embeds": [{
            "title": f"Portfolio — {data.get('updated_at', '')}",
            "url": DASHBOARD_URL,
            "description": "\n".join(lines),
            "color": color,
            "footer": {"text": f"Deposit: ₩{original:,.0f} | FX: {rate}"},
        }]
    }

    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code in (200, 204):
        print("Discord notification sent successfully")
    else:
        print(f"Discord notification failed: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
