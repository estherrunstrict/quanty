# Runbook — Dashboard pipeline

The public dashboard (https://estherrunstrict.github.io/quanty/) reports Jae's **whole**
investment status: bot sleeves + hands-on/manual holdings + cash, across KIS, Upbit and Toss.
Everything here is **reporting-side only** — no file in this runbook is read by a live bot.

## Components

| Piece | Where | What it does |
|---|---|---|
| `quanty-dashboard-api.service` | server, systemd, :8077 | runs `dashboard_server.py` (operational API, `Restart=always`) |
| `generate_dashboard_data.py` | `/home/ubuntu/quanty-dashboard/` | public generator — reads the API + KIS/Upbit/Toss, writes `docs/data/dashboard_data.json` |
| `push_dashboard.sh` | `/home/ubuntu/quanty-dashboard/` | publish orchestrator: generate → decisions.json → discovery → `git add docs/` → commit → push → Discord. Cron 16:00 KST Mon–Fri, 06:30 KST Tue–Sat |
| `dashboard_healthcheck.py` | `/home/ubuntu/quanty-dashboard/` | cron every 15 min; debounced Discord on status **transitions** only |
| `deploy_dashboard.sh` | `automation_oracle/` | ship code + restart the API + verify a *fresh* pid serving 200 |
| `toss_snapshot.py` | **the Mac**, launchd | read-only Toss snapshot → scp to the server (see below) |

## Data files it owns (all reporting-side)

| File | Written by | Notes |
|---|---|---|
| `quanty-dashboard/docs/data/dashboard_data.json` | generator | the published contract (below) |
| `koreainvestment-autotrade/strategy_results/toss_snapshot.json` | **Mac** `toss_snapshot.py`, via scp | mode 600; the server never calls Toss |
| `koreainvestment-autotrade/strategy_results/manual_sleeve_history.jsonl` | generator | one row per publish date (same date replaced, atomic `.tmp` + `os.replace`); feeds `equity_series["manual"]` |

---

## The published JSON contract

Consumers (public `index.html`, `decision.html`, healthcheck) rely on these **top-level** keys.
All were added additively — old front-ends keep working, so **deploy JSON first, HTML second**.

```jsonc
"accounts": {
  "kis":   {"total_krw":, "cash_krw":, "stock_krw":, "as_of": "YYYY-MM-DD HH:MM KST", "stale": false},
  "upbit": {"total_krw":, "cash_krw":, "as_of": "YYYY-MM-DD", "stale": false},
  "toss":  {"total_krw":, "cash_krw":, "holdings_krw":, "as_of": "<ISO8601+09:00>",
            "stale": false, "age_hours":, "source": "mac-launchd"}
},
"totals": {
  "investments_krw":,                       // kis + upbit + toss
  "bots_krw":, "manual_krw":, "cash_krw":,
  "split_pct": {"bots":, "manual":, "cash":},
  "reconciliation_gap_krw":, "reconciliation_gap_pct":,
  "reconciliation_warning": false,          // flips when |gap| > 1% of investments
  "unreported_bot_positions_krw":,          // see Known issues #1
  "manual_breakdown": {...}, "cash_breakdown": {...}, "fx_rate":
},
"strategies": [ ... {"id": "claude_bot"}, {"id": "manual"} ],   // manual = "Hands-on / Manual"
"equity_series": { ..., "claude_bot": [...], "manual": [...] }
```

**Toss staleness is honest by construction.** When the snapshot is missing, unreadable or older
than `TOSS_MAX_AGE_HOURS` (30h), `load_toss_account()` returns a zeroed block with
`stale: true`, `as_of: null`, and the real timestamp preserved under `last_seen_at`. The key is
**never omitted** — a grey "no data" tile is honest, a missing key is a silent lie. Any check you
write must therefore only audit `as_of` when `stale == false`.

---

## Toss snapshot — the Mac-side job (Jae arms it)

Toss's Open API allowlists the **Mac's** IP, so the server would get a 403. The snapshot is
therefore produced on the Mac and shipped to the server.

- Script: `automation_oracle/scripts/toss_snapshot.py` (read-only: token + accounts + holdings +
  cash. **No order code exists anywhere in this path.**)
- Client: `automation_oracle/framework/brokers/toss_readonly.py` (vendored from `brokers/toss_client.py`)
- Credentials: gitignored `.env` on the Mac only. Never scp'd, never committed.
- Schedule: `automation_oracle/scripts/launchd/com.quanty.toss-snapshot.plist` — 06:00 and 15:50
  KST (just ahead of each publish).

**The plist is NOT armed.** House rule (same as the NMF2 bot): Jae loads launchd himself.

```bash
# one-off test run first — it must exit 0 and print the snapshot summary
python3 automation_oracle/scripts/toss_snapshot.py

# arm it
cp automation_oracle/scripts/launchd/com.quanty.toss-snapshot.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.quanty.toss-snapshot.plist
launchctl start com.quanty.toss-snapshot
tail -f ~/Library/Logs/quanty-toss-snapshot.log

# disarm
launchctl unload ~/Library/LaunchAgents/com.quanty.toss-snapshot.plist
```

If the Mac sleeps through both runs the dashboard just greys the Toss tile
(`accounts.toss.stale = true`) and the healthcheck stays green — a stale flag that tells the truth
is not a fault. A **lying** fresh flag is (see `CONTENT` below).

---

## Healthcheck statuses

`dashboard_healthcheck.py` runs every 15 min and alerts Discord **only on a transition**
(including recovery). Priority order — the first one that matches wins:

| Status | Means | Typical cause |
|---|---|---|
| `DOWN` | `localhost:8077` unreachable | API service died |
| `STALE` | published `updated_at` older than the last *scheduled* push (schedule-aware, so a weekend hold is not stale) | `push_dashboard.sh` never ran / crashed early |
| `PUSH_STUCK` | commits sit unpushed for > 2h (`git log origin/main..main`) | GitHub auth/network failure — **server looks perfectly healthy while the public site is frozen** |
| `CONTENT` | data is fresh and published but broke the contract | a bot silently dropped out, Toss lies about being fresh, totals don't reconcile |
| `OK` | all of the above pass | |

`CONTENT` asserts, on the local `docs/data/dashboard_data.json`:

1. `equity_series` contains `claude_bot` (it was missing from the chart for months — this check is
   the reason the status exists).
2. `strategies` contains ids `claude_bot` **and** `manual`.
3. `accounts.kis` / `.upbit` / `.toss` all present, and when `accounts.toss.stale == false`, its
   `as_of` really is < 30h old.
4. `totals.reconciliation_warning` is not `true`.

Fixtures for these live in `quanty-dashboard/tests/fixtures/` (`dashboard_data_healthy.json`,
`dashboard_data_broken.json`). Tests: `python3 -m pytest tests/ -q` from `quanty-dashboard/`
(pytest is **not** installed in the server venv — run tests on the Mac).

Manual probe on the server:

```bash
cd /home/ubuntu/quanty-dashboard
~/myenv/bin/python3 dashboard_healthcheck.py          # prints status + detail, may alert
~/myenv/bin/python3 -c "import dashboard_healthcheck as hc, datetime as dt; \
  print(hc.content_issues(hc._read_data(), dt.datetime.now(dt.timezone.utc)))"   # read-only, never alerts
```

---

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
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # -> DOWN alert
sudo systemctl start quanty-dashboard-api
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # -> recovery alert
```

## Deploy a change

From `automation_oracle/`: `./deploy_dashboard.sh` (or `api` / `public`). The script scp's the
changed files, restarts the API via systemd, and polls `/api/data` until a **fresh pid** serves 200.

`deploy_dashboard.sh public` ships only `generate_dashboard_data.py` + `dashboard_healthcheck.py`.
`query_account_total.py`, `docs/index.html`, `docs/decision.html` and `tests/` are **not** in it —
scp those by hand, then commit them on the server.

Trigger a publish out of band: `ssh … "bash /home/ubuntu/quanty-dashboard/push_dashboard.sh"`.

---

## ⚠ Traps

### The `docs/` backup trap — never leave a file under `docs/`

`push_dashboard.sh` runs `git add docs/`, which stages **every untracked file** under `docs/`.
On 2026-08-16 agent backup files (`docs/index.html.bak.*`) were published to the public site this
way and had to be removed (server commit `824fdbb`).

- Back up **outside** `docs/`: `cp -p docs/index.html backups/index.html.bak.$(date +%Y%m%d-%H%M%S)`
- A repo-root `.gitignore` now ignores `*.bak`, `*.bak.*`, `backups/`, `__pycache__/`, `*.log`,
  `*.out` as a second line of defence — but do not rely on it; keep `docs/` clean.
- Sanity check before any publish: `find /home/ubuntu/quanty-dashboard/docs -name '*.bak*'` → empty.

### The dual-edit HTML trap

`automation_oracle/dashboard.html` (internal ops page, served by `dashboard_server.py`) and
`quanty-dashboard/docs/index.html` (public Pages) are **hand-maintained copies**, not one file
with two routes. Every front-end change must be applied to **both**, or the two dashboards drift:
the public page will show the new hero while the internal page silently keeps the old one.

Pull the live public copy before patching it, so you edit what is actually served:

```bash
scp -i ~/.ssh/oci_rsa ubuntu@193.123.246.52:/home/ubuntu/quanty-dashboard/docs/index.html \
    quanty-dashboard/docs/index.html
```

### Version control lives on the server

`quanty-dashboard/` on the Mac is a **scratch copy, not a git repo** — deploy by scp and commit on
the server (which holds the GitHub push auth). `automation_oracle/` is the opposite: commit on the
Mac, push to `mine`. `push_dashboard.sh` is now tracked in the server repo; if you change it,
commit it there.

### Deploy order

`dashboard_equity.py` must reach `koreainvestment-autotrade/` before (or with)
`generate_dashboard_data.py`, since the generator imports it. `deploy_dashboard.sh all` ships both,
so this is automatic.

---

## Known issues (open — do not "fix" casually, each needs its own change)

1. **`hybrid_vb_kr` publishes `holdings: []` while holding ~₩8.1M live.** The bot's card reports no
   positions, so the generator can't attribute them; they surface as
   `totals.unreported_bot_positions_krw` (₩8,112,520 as of 2026-08-16). Totals still reconcile
   because the split is partitioned from the account rows, but the *per-bot* card understates.
   Fix belongs in the bot's own reporting — which is a live `*AutoTrade*.py` file, so it needs a
   deliberate, separately-reviewed change.

2. **`recover_missing_bot_holdings` misattributes ticker 132030 to `korea_etf`.** The recovery path
   assumes korea_etf's target is 139220, but the live target became 132030, which Hybrid VB bought
   on 2026-08-11. Result: korea_etf's card shows ~₩1.24M that is actually Hybrid VB's position.
   Account-level totals are unaffected. (Background: `korea_etf`'s single-ticker KIS balance query
   intermittently returns empty, which is why the recovery path exists at all.)

3. **The internal ops dashboard does not show the new hero.** `dashboard.html` reads `/api/data`
   from `dashboard_server.py`, which does not yet emit `accounts` / `totals` — only the public
   generator does. The internal page degrades gracefully (hero falls back to the old portfolio
   strip); it is not broken, just behind. Fixing it means teaching `dashboard_server.py` the same
   contract.
