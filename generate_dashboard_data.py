#!/usr/bin/env python3
"""Generate dashboard_data.json from live dashboard_server API."""

import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DASHBOARD_DIR, "docs", "data")
API_URL = "http://localhost:8077/api/data"

def main():
    now = datetime.now(KST)
    print("[{}] Generating dashboard data...".format(now.strftime("%H:%M:%S KST")))

    try:
        raw = urllib.request.urlopen(API_URL, timeout=30).read()
        api_data = json.loads(raw)
        print("  Loaded {} strategies from API".format(len(api_data.get("strategies", []))))
    except Exception as e:
        print("  ERROR: Could not fetch from {}: {}".format(API_URL, e))
        return

    output = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
    }
    output.update(api_data)

    os.makedirs(DATA_DIR, exist_ok=True)

    data_file = os.path.join(DATA_DIR, "dashboard_data.json")
    with open(data_file, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print("  Wrote {}".format(data_file))

    eq_series = api_data.get("equity_series", {})
    if eq_series:
        eq_file = os.path.join(DATA_DIR, "equity_history.json")
        with open(eq_file, "w") as f:
            json.dump(eq_series, f, indent=2)
        print("  Wrote {}".format(eq_file))

    print("[{}] Done.".format(datetime.now(KST).strftime("%H:%M:%S KST")))

if __name__ == "__main__":
    main()
