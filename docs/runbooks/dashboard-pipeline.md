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
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # -> DOWN alert
sudo systemctl start quanty-dashboard-api
/home/ubuntu/myenv/bin/python3 /home/ubuntu/quanty-dashboard/dashboard_healthcheck.py   # -> recovery alert
```

## Deploy a change

From `automation_oracle/`: `./deploy_dashboard.sh` (or `api` / `public`). The script scp's the
changed files, restarts the API via systemd, and polls `/api/data` until HTTP 200.

## Notes
- The quanty-dashboard repo (`generate_dashboard_data.py`, `dashboard_healthcheck.py`) lives on the
  server with GitHub push auth; commit those changes on the server, not locally.
- Deploy order matters once: `dashboard_equity.py` must reach `koreainvestment-autotrade/` before (or
  with) `generate_dashboard_data.py`, since the generator imports it. `deploy_dashboard.sh all` ships
  both, so this is automatic.
