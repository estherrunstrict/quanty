# Dashboard Pipeline Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Quanty dashboard data pipeline self-healing and observable — supervise the API process, alert on freeze/failure, de-duplicate the chart-pin math into one module, and ship changes with one command.

**Architecture:** Approach A (harden in place). Keep the two-stage pipeline (long-running `dashboard_server.py` API on :8077 → cron-driven `quanty-dashboard/generate_dashboard_data.py` → GitHub Pages). Add: a shared `dashboard_equity.py` module imported by both stages; a systemd unit with `Restart=always`; a debounced Discord health/staleness cron; a local `deploy_dashboard.sh`. No trading code touched.

**Tech Stack:** Python 3 (stdlib + `requests`, `zoneinfo`), pytest, systemd, cron, bash, scp/ssh.

**Spec:** `docs/superpowers/specs/2026-06-02-dashboard-pipeline-hardening-design.md`

---

## Repos & file map

Two separate git repos are involved:

- **`automation_oracle/`** (this repo, server path `/home/ubuntu/koreainvestment-autotrade/`)
  - Create: `dashboard_equity.py` — shared ET-date + FX + chart-pin math
  - Create: `tests/test_dashboard_equity.py`
  - Create: `quanty-dashboard-api.service` — systemd unit (staged here, installed to `/etc/systemd/system/`)
  - Create: `deploy_dashboard.sh` — one-command deploy
  - Create: `docs/runbooks/dashboard-pipeline.md` — install/runbook
  - Modify: `dashboard_server.py` — import shared module, drop the local pin copy
- **`quanty-dashboard/`** (separate repo, server path `/home/ubuntu/quanty-dashboard/`, GitHub Pages remote)
  - Modify: `generate_dashboard_data.py` — import shared module, drop the local pin copy
  - Create: `dashboard_healthcheck.py` — liveness + staleness → debounced Discord
  - Create: `tests/test_dashboard_healthcheck.py`

The shared module physically lives in `koreainvestment-autotrade/`; `generate_dashboard_data.py` reaches it by inserting that dir on `sys.path` (it already inserts a sibling path for `open-trading-api`).

---

## Task 0: Set up a local working copy of the quanty-dashboard repo

`generate_dashboard_data.py` and the new healthcheck live in the separate `quanty-dashboard` repo, which has no local clone in this workspace.

**Files:** none (environment setup)

- [ ] **Step 1: Discover the repo's remote URL**

Run:
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 'cd /home/ubuntu/quanty-dashboard && git remote get-url origin'
```
Expected: a GitHub URL (e.g. `git@github.com:<owner>/quanty-dashboard.git`).

- [ ] **Step 2: Clone it next to automation_oracle**

Run (substitute the URL from Step 1):
```bash
cd /Users/jaelee/.gemini/antigravity/scratch/quanty
git clone <url-from-step-1> quanty-dashboard
```
Expected: `quanty-dashboard/generate_dashboard_data.py` and `quanty-dashboard/notify_discord.py` exist locally.

- [ ] **Step 3: Confirm the working tree matches the server**

Run:
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 'cd /home/ubuntu/quanty-dashboard && git rev-parse HEAD'
cd /Users/jaelee/.gemini/antigravity/scratch/quanty/quanty-dashboard && git rev-parse HEAD
```
Expected: same commit hash. If the server has uncommitted local edits, scp the live file down before editing:
`scp -i ~/.ssh/oci_rsa ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/generate_dashboard_data.py ./generate_dashboard_data.py`

---

## Task 1: Shared `dashboard_equity.py` module (TDD)

One definition of ET-date, FX conversion, and chart-endpoint pinning, imported by both pipeline stages. The ET logic is ported verbatim from `dashboard_server.py::_get_et_now` so snapshot-row dates and pin dates always agree.

**Files:**
- Create: `automation_oracle/dashboard_equity.py`
- Test: `automation_oracle/tests/test_dashboard_equity.py`

- [ ] **Step 1: Write the failing tests**

Create `automation_oracle/tests/test_dashboard_equity.py`:
```python
"""Unit tests for the shared dashboard equity math (dashboard_equity.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_equity as de


def test_to_krw_passthrough_and_convert():
    assert de.to_krw(100, "KRW", 1380) == 100.0
    assert de.to_krw(2, "USD", 1380) == 2760.0
    assert de.to_krw(None, "USD", 1380) == 0.0


def test_et_today_format():
    t = de.et_today()
    assert len(t) == 10 and t[4] == "-" and t[7] == "-"


def test_pin_overwrites_stale_endpoint():
    eq = {"jd_strategy": [["2026-05-21", 19847.61], ["2026-05-22", 186000.0]]}
    strategies = [{"id": "jd_strategy", "currency": "USD", "total_pl_ytd": -232.35}]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["jd_strategy"][-1] == ["2026-06-01", round(-232.35 * 1380.0, 2)]
    assert eq["jd_strategy"][0] == ["2026-05-21", 19847.61]  # past untouched


def test_pin_replaces_today_in_place():
    eq = {"quant40": [["2026-05-31", 1000.0], ["2026-06-01", 999.0]]}
    strategies = [{"id": "quant40", "currency": "KRW", "total_pl_ytd": 50000.0}]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["quant40"] == [["2026-05-31", 1000.0], ["2026-06-01", 50000.0]]


def test_pin_hybrid_legs_and_missing_keys():
    eq = {"hybrid_vb_kr": [["2026-05-31", 0.0]], "hybrid_vb_us": [["2026-05-31", 0.0]]}
    strategies = [
        {"id": "hybrid_vb", "kr": {"total_pl_ytd": 12345.0}, "us": {"total_pl_ytd": 10.0}},
        {"id": "korea_etf", "currency": "KRW", "total_pl_ytd": 7.0},  # no series → skipped
    ]
    de.pin_equity_endpoints(eq, strategies, 1380.0, "2026-06-01")
    assert eq["hybrid_vb_kr"][-1] == ["2026-06-01", 12345.0]
    assert eq["hybrid_vb_us"][-1] == ["2026-06-01", round(10.0 * 1380.0, 2)]
    assert "korea_etf" not in eq
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd automation_oracle && python3 -m pytest tests/test_dashboard_equity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard_equity'`.

- [ ] **Step 3: Write the module**

Create `automation_oracle/dashboard_equity.py`:
```python
#!/usr/bin/env python3
"""Shared equity math for both dashboard pipeline stages.

Single source of truth for: Eastern-Time date basis, FX→KRW conversion, and
pinning each bot's comparison-chart endpoint to its live Total P/L. Imported by
dashboard_server.py (operational API) and quanty-dashboard/generate_dashboard_data.py
(public generator) so the "chart == card" invariant has exactly one definition.
"""
from datetime import datetime, timezone, timedelta


def et_now():
    """Current Eastern Time (auto EDT/EST). Ported from dashboard_server so the
    equity_history snapshot row date and the pin date share one basis."""
    now_utc = datetime.now(timezone.utc)
    yr = now_utc.year
    mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
    dst_on = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)
    nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
    dst_off = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=7)
    offset = -4 if dst_on <= now_utc < dst_off else -5
    return now_utc.astimezone(timezone(timedelta(hours=offset)))


def et_today():
    """Today's date (YYYY-MM-DD) in Eastern Time."""
    return et_now().strftime("%Y-%m-%d")


def to_krw(amount, currency, rate):
    """FX-convert a native amount to KRW (KRW passthrough; else * rate)."""
    return float(amount or 0) if currency == "KRW" else float(amount or 0) * rate


def pin_equity_endpoints(equity_series, strategies, rate, today):
    """Overwrite each bot's latest comparison-chart point with its live Total P/L (KRW).

    Replaces the last point in place when its date == today, else appends
    [today, val]. Handles hybrid_vb legs (kr=KRW, us=USD). Bots with no series
    are skipped (chart shows "insufficient data", never a wrong value). Past
    dates are untouched.
    """
    def _pin(key, total_native, cur):
        series = equity_series.get(key)
        if series is None:
            return
        val = round(to_krw(total_native, cur, rate), 2)
        if series and series[-1][0] == today:
            series[-1][1] = val
        else:
            series.append([today, val])

    for s in strategies:
        sid = s.get("id", "")
        if sid == "hybrid_vb":
            for leg_key, cur in (("kr", "KRW"), ("us", "USD")):
                leg = s.get(leg_key) or {}
                _pin("hybrid_vb_{}".format(leg_key), leg.get("total_pl_ytd", 0), cur)
            continue
        _pin(sid, s.get("total_pl_ytd", 0), s.get("currency", "KRW"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd automation_oracle && python3 -m pytest tests/test_dashboard_equity.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd automation_oracle
git add dashboard_equity.py tests/test_dashboard_equity.py
git commit -m "feat(dashboard): shared dashboard_equity module (ET date + FX + chart pin)"
```

---

## Task 2: Wire `dashboard_server.py` to the shared module

Remove the duplicate pin logic and route the ET date through the shared module so the snapshot row and the pinned endpoint always carry the same date.

**Files:**
- Modify: `automation_oracle/dashboard_server.py`

- [ ] **Step 1: Import the shared module**

Near the other local imports at the top of `dashboard_server.py` (after the stdlib imports, before `ROOT = Path(...)`), add:
```python
from dashboard_equity import et_now, et_today, pin_equity_endpoints
```

- [ ] **Step 2: Replace the local ET helper with the shared one**

Delete the entire local `def _get_et_now():` function (the `"""Get current Eastern Time..."""` block) and replace it with a one-line alias so existing call sites keep working:
```python
_get_et_now = et_now  # shared impl (dashboard_equity)
```

- [ ] **Step 3: Route the snapshot date through `et_today()`**

In `_save_equity_snapshot`, change:
```python
    today = _get_et_now().strftime("%Y-%m-%d")
```
to:
```python
    today = et_today()
```

- [ ] **Step 4: Delete the local pin function**

Delete the entire local `def _pin_live_equity_endpoints(equity_series, strategies, exchange_rate):` function (docstring through its `for s in strategies:` loop).

- [ ] **Step 5: Update the call site to use the shared pin**

In `build_dashboard_data`, change:
```python
    _pin_live_equity_endpoints(equity_series, strategies, fx_now)
```
to:
```python
    pin_equity_endpoints(equity_series, strategies, fx_now, et_today())
```

- [ ] **Step 6: Run the full dashboard test suite**

Run: `cd automation_oracle && python3 -m pytest tests/test_aggregate_realized_pnl.py tests/test_dashboard_equity.py -q`
Expected: PASS (all green — no regression in `_enrich` / aggregation; shared module still passes).

- [ ] **Step 7: Smoke-test the module loads**

Run: `cd automation_oracle && python3 -c "import dashboard_server"`
Expected: no ImportError (confirms the import + alias resolve).

- [ ] **Step 8: Commit**

```bash
cd automation_oracle
git add dashboard_server.py
git commit -m "refactor(dashboard): use shared dashboard_equity in dashboard_server"
```

---

## Task 3: Wire `quanty-dashboard/generate_dashboard_data.py` to the shared module

The public generator does a fresh KIS mark-to-market after fetching the API, so it must re-pin — now via the shared module, using the ET date basis.

**Files:**
- Modify: `quanty-dashboard/generate_dashboard_data.py`

- [ ] **Step 1: Make the shared module importable + import it**

Near the top of `generate_dashboard_data.py`, where `TRADING_DIR` is defined and `sys.path.insert(...)` calls already exist, add a path insert for the trading dir and the import:
```python
sys.path.insert(0, TRADING_DIR)  # for dashboard_equity (shared with dashboard_server)
from dashboard_equity import et_today, pin_equity_endpoints
```
(`TRADING_DIR = "/home/ubuntu/koreainvestment-autotrade"` already exists in this file.)

- [ ] **Step 2: Delete the local pin function**

Delete the entire local `def pin_equity_endpoints(api_data, rate):` function added on 2026-06-02 (docstring through its `for s in ...` loop).

- [ ] **Step 3: Update the call site**

In `main()`, replace the call:
```python
    pin_equity_endpoints(api_data, (portfolio or {}).get("exchange_rate", 1380))
```
with the shared signature (pass the series dict, the strategies list, the rate, and the ET date):
```python
    pin_equity_endpoints(
        api_data.get("equity_series") or {},
        api_data.get("strategies") or [],
        (portfolio or {}).get("exchange_rate", 1380),
        et_today(),
    )
```

- [ ] **Step 4: Verify it compiles**

Run: `cd quanty-dashboard && python3 -m py_compile generate_dashboard_data.py`
Expected: no output (success). (A full run needs server-only KIS deps — defer the live run to the deploy verification in Task 6/7.)

- [ ] **Step 5: Commit (in the quanty-dashboard repo)**

```bash
cd quanty-dashboard
git add generate_dashboard_data.py
git commit -m "refactor: use shared dashboard_equity for chart-endpoint pinning"
```

---

## Task 4: `dashboard_healthcheck.py` — liveness + staleness → debounced Discord (TDD)

Pure decision functions are unit-tested; the I/O wrapper (curl, read JSON, post, state file) is a thin shell around them.

**Files:**
- Create: `quanty-dashboard/dashboard_healthcheck.py`
- Test: `quanty-dashboard/tests/test_dashboard_healthcheck.py`

- [ ] **Step 1: Write the failing tests**

Create `quanty-dashboard/tests/test_dashboard_healthcheck.py`:
```python
"""Unit tests for the dashboard healthcheck decision logic."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_healthcheck as hc


def test_classify_down_when_api_unreachable():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=False, updated_at="2026-06-02 06:30 KST",
                              now=now, threshold_hours=15) == "DOWN"


def test_classify_stale_when_data_too_old():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)  # ~30h after the update
    assert hc.classify_status(api_ok=True, updated_at="2026-06-01 06:30 KST",
                              now=now, threshold_hours=15) == "STALE"


def test_classify_ok_when_fresh():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert hc.classify_status(api_ok=True, updated_at="2026-06-02 06:30 KST",
                              now=now, threshold_hours=15) == "OK"


def test_should_alert_only_on_transition():
    assert hc.should_alert("OK", "DOWN") is True
    assert hc.should_alert("DOWN", "DOWN") is False
    assert hc.should_alert("DOWN", "OK") is True      # recovery message
    assert hc.should_alert("OK", "OK") is False


def test_alert_message_mentions_status():
    assert "DOWN" in hc.alert_message("DOWN", "2026-06-02 06:30 KST")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_dashboard_healthcheck.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'dashboard_healthcheck'`.

- [ ] **Step 3: Write the healthcheck script**

Create `quanty-dashboard/dashboard_healthcheck.py`:
```python
#!/usr/bin/env python3
"""Dashboard pipeline healthcheck.

Run by cron every 15 min. Alerts Discord ONLY on status transitions
(OK→DOWN, OK→STALE, and recovery back to OK) so it never spams. Turns the
silent-freeze failure mode (dead API → cron no-ops → frozen dashboard) into a
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
    """Parse 'YYYY-MM-DD HH:MM KST' → aware datetime (KST = UTC+9)."""
    if not s:
        return None
    try:
        base = s.replace(" KST", "")
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone(timedelta(hours=9)))
    except (ValueError, TypeError):
        return None


def classify_status(api_ok, updated_at, now, threshold_hours):
    """Return 'DOWN' | 'STALE' | 'OK'. Pure — unit-tested."""
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
        return "🔴 Quanty dashboard API is DOWN (localhost:8077 unreachable). Data is not updating."
    if status == "STALE":
        return "🟠 Quanty dashboard data is STALE (last update {}). Pipeline may be frozen.".format(updated_at)
    return "🟢 Quanty dashboard recovered — data is fresh again (last update {}).".format(updated_at)


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd quanty-dashboard && python3 -m pytest tests/test_dashboard_healthcheck.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit (in the quanty-dashboard repo)**

```bash
cd quanty-dashboard
git add dashboard_healthcheck.py tests/test_dashboard_healthcheck.py
git commit -m "feat: dashboard healthcheck with debounced Discord alerts"
```

---

## Task 5: systemd unit file

**Files:**
- Create: `automation_oracle/quanty-dashboard-api.service`

- [ ] **Step 1: Create the unit file**

Create `automation_oracle/quanty-dashboard-api.service`:
```ini
[Unit]
Description=Quanty dashboard API (dashboard_server.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/koreainvestment-autotrade
ExecStart=/home/ubuntu/myenv/bin/python3 dashboard_server.py
Restart=always
RestartSec=5
StandardOutput=append:/home/ubuntu/koreainvestment-autotrade/dashboard_server.out
StandardError=append:/home/ubuntu/koreainvestment-autotrade/dashboard_server.out

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Validate the unit syntax locally (best-effort)**

Run: `systemd-analyze verify automation_oracle/quanty-dashboard-api.service 2>&1 || echo "systemd-analyze not available on macOS — verify on server"`
Expected: no errors, or the not-available note (validation happens during server install in Task 7).

- [ ] **Step 3: Commit**

```bash
cd automation_oracle
git add quanty-dashboard-api.service
git commit -m "feat(dashboard): systemd unit for the dashboard API"
```

---

## Task 6: `deploy_dashboard.sh` — one-command deploy + health check

**Files:**
- Create: `automation_oracle/deploy_dashboard.sh`

- [ ] **Step 1: Write the deploy script**

Create `automation_oracle/deploy_dashboard.sh`:
```bash
#!/usr/bin/env bash
# Deploy dashboard pipeline files to the server and restart the API.
# Usage: ./deploy_dashboard.sh            # deploy all dashboard files
#        ./deploy_dashboard.sh api        # only API-side files
#        ./deploy_dashboard.sh public     # only quanty-dashboard files
set -euo pipefail

KEY="$HOME/.ssh/oci_rsa"
HOST="ubuntu@193.123.246.52"
TRADING="/home/ubuntu/koreainvestment-autotrade"
PUBLIC="/home/ubuntu/quanty-dashboard"
HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_LOCAL="$HERE/../quanty-dashboard"
WHAT="${1:-all}"

scp_one() { scp -i "$KEY" "$1" "$HOST:$2"; echo "  → $2"; }

if [[ "$WHAT" == "all" || "$WHAT" == "api" ]]; then
  echo "Deploying API-side files…"
  scp_one "$HERE/dashboard_equity.py"            "$TRADING/dashboard_equity.py"
  scp_one "$HERE/dashboard_server.py"            "$TRADING/dashboard_server.py"
  scp_one "$HERE/quanty-dashboard-api.service"   "$TRADING/quanty-dashboard-api.service"
fi

if [[ "$WHAT" == "all" || "$WHAT" == "public" ]]; then
  echo "Deploying public-generator files…"
  scp_one "$PUBLIC_LOCAL/generate_dashboard_data.py" "$PUBLIC/generate_dashboard_data.py"
  scp_one "$PUBLIC_LOCAL/dashboard_healthcheck.py"   "$PUBLIC/dashboard_healthcheck.py"
fi

echo "Restarting the API…"
if ssh -i "$KEY" "$HOST" "sudo -n systemctl restart quanty-dashboard-api" 2>/dev/null; then
  echo "  restarted via systemd"
else
  echo "  !! Could not restart unattended. Run this yourself:"
  echo "     ssh -i $KEY $HOST 'sudo systemctl restart quanty-dashboard-api'"
fi

echo "Health-checking the API…"
for i in 1 2 3 4 5; do
  code=$(ssh -i "$KEY" "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8077/api/data" || echo 000)
  echo "  attempt $i → HTTP $code"
  [[ "$code" == "200" ]] && { echo "Deploy OK."; exit 0; }
  sleep 3
done
echo "Deploy FAILED health check." >&2
exit 1
```

- [ ] **Step 2: Make it executable + lint**

Run:
```bash
cd automation_oracle && chmod +x deploy_dashboard.sh
bash -n deploy_dashboard.sh && echo "syntax OK"
command -v shellcheck >/dev/null && shellcheck deploy_dashboard.sh || echo "shellcheck not installed — skipped"
```
Expected: `syntax OK` (and a clean shellcheck if installed).

- [ ] **Step 3: Commit**

```bash
cd automation_oracle
git add deploy_dashboard.sh
git commit -m "feat(dashboard): one-command deploy script with health check"
```

---

## Task 7: Install runbook + server activation

The systemd install, sudoers drop-in, and crontab edit require sudo and run on the server — Jae executes these. This task produces the runbook and walks the activation.

**Files:**
- Create: `automation_oracle/docs/runbooks/dashboard-pipeline.md`

- [ ] **Step 1: Write the runbook**

Create `automation_oracle/docs/runbooks/dashboard-pipeline.md`:
````markdown
# Runbook — Dashboard pipeline

## Components
- `quanty-dashboard-api.service` — systemd unit running `dashboard_server.py` on :8077 (Restart=always).
- `dashboard_healthcheck.py` — cron every 15 min; debounced Discord on DOWN/STALE/recovery.
- `deploy_dashboard.sh` — ship code + restart + health check.

## One-time install (sudo)
```bash
# 1. Stop the old hand-started process to free :8077
pkill -f '[d]ashboard_server.py' || true

# 2. Install + enable the service
sudo cp /home/ubuntu/koreainvestment-autotrade/quanty-dashboard-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quanty-dashboard-api
sudo systemctl status quanty-dashboard-api      # expect: active (running)

# 3. Passwordless restart for the deploy script (optional but recommended)
echo 'ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart quanty-dashboard-api, /usr/bin/systemctl status quanty-dashboard-api' | sudo tee /etc/sudoers.d/quanty-dashboard
sudo visudo -cf /etc/sudoers.d/quanty-dashboard  # validate

# 4. Healthcheck cron (separate line — does NOT touch the trading-cron entries)
crontab -l > /tmp/cron.bak           # back up first
( crontab -l; echo '*/15 * * * * /home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py >> /home/ubuntu/quanty-dashboard/healthcheck.log 2>&1' ) | crontab -
crontab -l | grep dashboard_healthcheck   # confirm added
```

## Verify
```bash
# auto-restart works
sudo systemctl kill quanty-dashboard-api ; sleep 7 ; systemctl is-active quanty-dashboard-api   # active

# healthcheck fires once on down, then recovers
sudo systemctl stop quanty-dashboard-api
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # → DOWN alert
sudo systemctl start quanty-dashboard-api
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # → recovery alert
```

## Deploy a change
From `automation_oracle/`: `./deploy_dashboard.sh` (or `api` / `public`).
````

- [ ] **Step 2: Commit the runbook**

```bash
cd automation_oracle
git add docs/runbooks/dashboard-pipeline.md
git commit -m "docs(dashboard): pipeline install + activation runbook"
```

- [ ] **Step 3: Deploy the code (Jae authorizes)**

Run: `cd automation_oracle && ./deploy_dashboard.sh all`
Expected: every file scp'd, restart attempted, `Deploy OK.` on HTTP 200. (Before the systemd install in Step 4, the restart line prints the manual fallback — that's expected on first run.)

- [ ] **Step 4: Run the one-time install (Jae, sudo)**

Execute the "One-time install" block from the runbook on the server. Confirm `systemctl status quanty-dashboard-api` is `active (running)` and `crontab -l` shows the healthcheck line.

- [ ] **Step 5: Verify end-to-end**

Run the runbook "Verify" block. Confirm: the service auto-restarts after a kill; the healthcheck posts exactly one DOWN alert and one recovery to Discord. Then trigger a normal regen and confirm chart==card holds:
```bash
ssh -i ~/.ssh/oci_rsa ubuntu@193.123.246.52 'cd /home/ubuntu/quanty-dashboard && /home/ubuntu/myenv/bin/python3 generate_dashboard_data.py >/dev/null 2>&1; python3 -c "
import json; d=json.load(open(\"docs/data/dashboard_data.json\")); fx=d[\"portfolio\"][\"exchange_rate\"]
for s in d[\"strategies\"]:
    if s[\"id\"]==\"hybrid_vb\": continue
    es=d[\"equity_series\"].get(s[\"id\"],[])
    card=round((s.get(\"total_pl_ytd\") or 0)*(1 if s.get(\"currency\")==\"KRW\" else fx),2)
    print(\"OK\" if es and abs(es[-1][1]-card)<1 else \"MISMATCH\", s[\"id\"])
"'
```
Expected: every bot prints `OK`.

---

## Future work (Approach B)

Collapse the dual recompute path: move the fresh-KIS mark-to-market into `dashboard_server.py` so `/api/data` returns already-marked, already-pinned data, and `generate_dashboard_data.py` becomes a thin fetch→write→push. Eliminates the re-pin entirely. Record as a wiki decision memo before starting. Out of scope here.
