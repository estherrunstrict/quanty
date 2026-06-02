#!/usr/bin/env python3
"""Dashboard pipeline healthcheck.

Run by cron every 15 min. Alerts Discord ONLY on status transitions
(OK->DOWN, OK->STALE, and recovery back to OK) so it never spams. Turns the
silent-freeze failure mode (dead API -> cron no-ops -> frozen dashboard) into a
visible alert.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API_URL = "http://localhost:8077/api/data"
DATA_FILE = os.path.join(HERE, "docs", "data", "dashboard_data.json")
STATE_FILE = os.path.join(HERE, ".healthcheck_state.json")
THRESHOLD_HOURS = 15


def _parse_updated_at(s):
    """Parse 'YYYY-MM-DD HH:MM KST' -> aware datetime (KST = UTC+9)."""
    if not s:
        return None
    try:
        base = s.replace(" KST", "")
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone(timedelta(hours=9)))
    except (ValueError, TypeError):
        return None


def classify_status(api_ok, updated_at, now, threshold_hours):
    """Return 'DOWN' | 'STALE' | 'OK'. Pure -- unit-tested."""
    if not api_ok:
        return "DOWN"
    ts = _parse_updated_at(updated_at)
    if ts is None:
        return "STALE"
    if now - ts > timedelta(hours=threshold_hours):
        return "STALE"
    return "OK"


def should_alert(prev, curr):
    """Alert only when status changes (incl. recovery to OK)."""
    return prev != curr


def alert_message(status, updated_at):
    if status == "DOWN":
        return "\U0001F534 Quanty dashboard API is DOWN (localhost:8077 unreachable). Data is not updating."
    if status == "STALE":
        return "\U0001F7E0 Quanty dashboard data is STALE (last update {}). Pipeline may be frozen.".format(updated_at)
    return "\U0001F7E2 Quanty dashboard recovered -- data is fresh again (last update {}).".format(updated_at)


def _api_ok():
    try:
        with urllib.request.urlopen(API_URL, timeout=15) as r:
            if r.status != 200:
                return False
            json.loads(r.read())
            return True
    except Exception:
        return False


def _read_updated_at():
    try:
        with open(DATA_FILE) as f:
            return json.load(f).get("updated_at")
    except Exception:
        return None


def _load_prev_status():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("status", "OK")
    except Exception:
        return "OK"


def _save_status(status):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"status": status}, f)
    os.replace(tmp, STATE_FILE)


def _send_discord(message):
    try:
        import requests
        import notify_discord
        url = notify_discord.get_webhook_url()
        if not url:
            print("healthcheck: no Discord webhook url", file=sys.stderr)
            return
        requests.post(url, json={"content": message}, timeout=10)
    except Exception as e:
        print("healthcheck: discord send failed: {}".format(e), file=sys.stderr)


def main():
    now = datetime.now(timezone.utc)
    updated_at = _read_updated_at()
    status = classify_status(_api_ok(), updated_at, now, THRESHOLD_HOURS)
    prev = _load_prev_status()
    if should_alert(prev, status):
        _send_discord(alert_message(status, updated_at))
    _save_status(status)
    print("[{}] healthcheck status={} (prev={})".format(now.isoformat(), status, prev))


if __name__ == "__main__":
    main()
