#!/usr/bin/env python3
"""Send dashboard update notification to Discord."""

import json
import os
import requests

TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL = "https://estherrunstrict.github.io/quanty"


def get_webhook_url():
    env_path = os.path.join(TRADING_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
    return None


def main():
    webhook_url = get_webhook_url()
    if not webhook_url:
        print("No Discord webhook URL found")
        return

    data_path = os.path.join(DASHBOARD_DIR, "docs", "data", "dashboard_data.json")
    with open(data_path) as f:
        data = json.load(f)

    # Build summary lines — show cumulative P/L per strategy
    lines = []
    for s in data["strategies"]:
        total_pl = s.get("total_pl_krw", 0)
        total_pct = s.get("total_pl_pct", 0)
        realized = s.get("realized_pl_krw", 0)
        icon = "\U0001f7e2" if total_pl > 0 else ("\U0001f534" if total_pl < 0 else "\u26aa")
        # Show total P/L in KRW with percentage
        pl_str = f"\u20a9{total_pl:+,.0f} ({total_pct:+.1f}%)"
        realized_str = f" [R: \u20a9{realized:+,.0f}]" if realized != 0 else ""
        lines.append(f"{icon} **{s['name']}**: {pl_str}{realized_str}")

    # Account total P/L
    p = data.get("portfolio", {})
    total_val = p.get("total_value_krw", 0)
    original = p.get("original_deposit_krw", 0)
    total_pl_krw = p.get("total_profit_krw", 0)
    total_pnl = p.get("total_profit_pct", 0)

    total_icon = "\U0001f7e2" if total_pl_krw >= 0 else "\U0001f534"
    lines.append("")
    lines.append(f"{total_icon} **KIS Total: \u20a9{total_val:,.0f} / P/L: \u20a9{total_pl_krw:+,.0f} ({total_pnl:+.1f}%)**")
    lines.append(f"  Original: \u20a9{original:,.0f}")

    summary = "\n".join(lines)
    updated_at = data.get("updated_at", "")

    payload = {
        "embeds": [{
            "title": "\U0001f4ca Dashboard Updated",
            "url": DASHBOARD_URL,
            "description": f"{summary}\n\n[**View Dashboard \u2192**]({DASHBOARD_URL})",
            "color": 3447003,
            "footer": {"text": f"Quanty Dashboard | {updated_at}"},
        }]
    }

    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code in (200, 204):
        print("Discord notification sent successfully")
    else:
        print(f"Discord notification failed: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
