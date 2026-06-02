# Dashboard Pipeline Hardening — Design Spec

- **Date:** 2026-06-02
- **Author:** Claude (with Jae)
- **Status:** Approved design → ready for implementation plan
- **Approach:** A (harden in place). Approach B (collapse dual recompute path) logged as Future Work.

## Context

The Quanty dashboard data pipeline has two stages:

```
Bots (*AutoTrade*.py) → strategy_results/*.json, equity_history.jsonl, realized_pl_2026.json
        │
        ▼
dashboard_server.py  ── long-running HTTP API on :8077 (build_dashboard_data() per request)
        │  (currently UNSUPERVISED — started by hand with setsid; no systemd/tmux/cron)
        ▼
quanty-dashboard/generate_dashboard_data.py
        │  cron 16:00 & 06:30 KST: fetch /api/data → fresh KIS mark-to-market →
        │  recompute totals → write docs/data/*.json → git push → Discord notify
        ▼
GitHub Pages (public dashboard)   +   operational dashboard.html (served by :8077)
```

The data *logic* is correct as of 2026-06-02 (the chart-vs-card mismatch was fixed by pinning
each bot's comparison-chart endpoint to its live `total_pl_ytd`). The remaining gaps are all
**operational robustness**. This spec addresses them.

### Fragilities being fixed

1. **Single unsupervised process = silent freeze.** If `dashboard_server.py` dies (crash, OOM,
   reboot), the cron still runs: the API fetch fails → `generate_dashboard_data.py` returns early
   → no file changes → `push_dashboard.sh` prints "No changes to push" and **exits 0**. The
   dashboard freezes with **zero alerts**.
2. **No staleness/failure alerting.** Discord only fires on *successful* pushes.
3. **Duplicated pin/total logic.** `_pin_live_equity_endpoints` (dashboard_server.py) and
   `pin_equity_endpoints` (generate_dashboard_data.py) are two copies that must stay in sync.
4. **Two independent recompute paths** (operational uses bot-last-run unrealized; public does
   fresh KIS mark-to-market) — the "chart == card" invariant must be re-enforced in each.
5. **Manual deploy + restart** (hand `scp` + `kill`/`setsid`).

## Goals / Non-goals

**Goals**
- The API process can never silently die (auto-restart on crash and reboot).
- A frozen or down pipeline produces a Discord alert, not silence.
- The pin/FX/total math has exactly one definition, imported by both stages.
- Deploying a dashboard code change is one command with a health check.

**Non-goals (this spec)**
- Reworking the data flow / collapsing the dual recompute path (that is Approach B — Future Work).
- Any change to live trading code (`*AutoTrade*.py`), `check_and_run.sh`, or the trading cron.
- Increasing public-dashboard refresh frequency beyond the existing 2×/day windows.

## Components

### 1. API supervision (systemd)

New unit file `koreainvestment-autotrade/quanty-dashboard-api.service`, installed to
`/etc/systemd/system/`:

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

**Install (one-time, requires sudo — Jae runs):**
```bash
# kill the current hand-started process to free :8077
pkill -f '[d]ashboard_server.py'
sudo cp /home/ubuntu/koreainvestment-autotrade/quanty-dashboard-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quanty-dashboard-api
sudo systemctl status quanty-dashboard-api   # verify active (running)
```

**Optional sudoers drop-in** (enables the deploy script's unattended restart, component 4):
```
# /etc/sudoers.d/quanty-dashboard  (visudo-installed)
ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart quanty-dashboard-api, /usr/bin/systemctl status quanty-dashboard-api
```

Data-flow change: none. Same process, same port — supervised.

### 2. Health-check + alerting

New `quanty-dashboard/dashboard_healthcheck.py`, run by cron `*/15 * * * *`. Each run:

1. **Liveness:** GET `http://localhost:8077/api/data`. Non-200 or unparseable JSON ⇒ status `DOWN`.
2. **Freshness:** read `docs/data/dashboard_data.json` `updated_at`. Older than the staleness
   threshold ⇒ status `STALE`. Threshold = **15h** (the largest legitimate gap between the
   16:00→06:30 push windows is ~14.5h; 15h gives a small grace margin).
3. **Debounced alert:** compare against a state file
   (`quanty-dashboard/.healthcheck_state.json`). Alert Discord **only on state transition**
   (OK→DOWN, OK→STALE) and emit a single "recovered" message on the way back to OK. No alert when
   state is unchanged → no spam.

Reuses the existing Discord webhook via `notify_discord.py` (extract/share its `send(message)`
helper rather than duplicating the webhook URL).

Crontab addition (separate line, does not touch the trading-cron entries):
```
*/15 * * * * /home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py >> /home/ubuntu/quanty-dashboard/healthcheck.log 2>&1
```

### 3. De-duplicate the pin/total logic

New shared module `koreainvestment-autotrade/dashboard_equity.py`:

```python
def et_today() -> str:
    """Today's date (YYYY-MM-DD) in US/Eastern — the basis equity_history rows use."""

def to_krw(amount, currency, rate) -> float:
    """FX-convert a native amount to KRW (KRW passthrough; else * rate)."""

def pin_equity_endpoints(equity_series, strategies, rate, today) -> None:
    """Overwrite each bot's latest comparison-chart point with its live total_pl_ytd (KRW).
    Replaces the last point in place when its date == today, else appends [today, val].
    Handles hybrid_vb legs (kr=KRW, us=USD). Skips bots absent from equity_series."""
```

- `dashboard_server.py` imports it (same dir); deletes its local `_pin_live_equity_endpoints` and
  calls `pin_equity_endpoints(equity_series, strategies, fx_now, et_today())`.
- `generate_dashboard_data.py` adds `sys.path.insert(0, TRADING_DIR)` and imports it; deletes its
  local `pin_equity_endpoints`; calls it after mark-to-market with `et_today()`.
- **Both use `et_today()`** as the date basis. This fixes a latent bug: the public path currently
  uses KST `today`, while snapshot rows are ET-dated — near ET-midnight that could append a
  duplicate point instead of replacing the existing one.

One definition of "chart == card", imported in both stages.

### 4. Automated deploy + restart

New local `automation_oracle/deploy_dashboard.sh`:

```
deploy_dashboard.sh [files...]   # default: all dashboard files
```
- scp each changed file to its correct server path:
  - `dashboard_server.py`, `dashboard_equity.py`, `quanty-dashboard-api.service` → `koreainvestment-autotrade/`
  - `generate_dashboard_data.py`, `dashboard_healthcheck.py` → `quanty-dashboard/`
- restart: `ssh … sudo systemctl restart quanty-dashboard-api` (uses the NOPASSWD drop-in; if
  absent, the script prints the exact command and pauses for Jae).
- health-check: poll `http://localhost:8077/api/data` up to N times until HTTP 200; report pass/fail.

Replaces the manual `scp` + `kill`/`setsid` choreography.

## Error handling

- **API write failures** in `_save_equity_snapshot` already surface to stderr (fixed 2026-06-02);
  under systemd these land in `dashboard_server.out` / journald.
- **API-fetch failure** in `generate_dashboard_data.py`: the healthcheck cron catches the resulting
  staleness. (Approach A keeps the generator's early-return; B would restructure this.)
- **Healthcheck self-failure** (e.g., its own crash): cron logs to `healthcheck.log`; non-fatal.
- **Discord send failure:** logged, non-fatal — never block or crash the pipeline on a notify error.

## Testing

- **`dashboard_equity.py`** — unit tests in `automation_oracle/tests/test_dashboard_equity.py`:
  stale-endpoint replacement, same-day replace-in-place, append when last date != today,
  hybrid leg per-currency conversion, missing-key skip, FX passthrough for KRW.
- **systemd** — verify `systemctl status` active; kill the process and confirm auto-restart within
  `RestartSec`; reboot test optional.
- **healthcheck** — manual: stop the service → confirm one Discord DOWN alert + no repeats →
  restart → confirm one recovery message. Stale path: temporarily lower threshold to force STALE.
- Existing `test_aggregate_realized_pnl.py` suite must stay green after `dashboard_server.py` imports
  the shared module.

## Rollout / safety

- All changes are reporting/ops infra. **No `*AutoTrade*.py`, no `check_and_run.sh`, no trading
  cron touched.**
- The healthcheck cron is a **separate** crontab line. Back up `crontab -l` before editing; Jae
  approves the crontab diff.
- systemd install and the sudoers drop-in are run by Jae (sudo).
- Deploy order: ship `dashboard_equity.py` + updated `dashboard_server.py` together (the API imports
  the module); then `generate_dashboard_data.py`; then healthcheck + cron; then systemd unit.

## Future work — Approach B (collapse the dual recompute path)

Move the fresh-KIS mark-to-market into `dashboard_server.py` so `/api/data` returns
already-marked, already-pinned data. `generate_dashboard_data.py` then becomes a thin
fetch→write→push with no recompute and no re-pin, eliminating fragilities #3 and #4 at the root.
Higher risk (moves KIS account I/O onto the always-on critical process; reworks the data flow), so
deferred until Approach A is stable. To be recorded as a wiki decision memo.
