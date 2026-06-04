# Runbook — Decision approval buttons

## Components
- `decision_approval_server.py` — systemd `quanty-decision-approval`, 127.0.0.1:8078, token-gated.
- `start_approval_tunnel.sh` — systemd `quanty-approval-tunnel`, Cloudflare quick tunnel → publishes `docs/data/approve_endpoint.json`.
- Buttons in `docs/decision.html`.

## One-time install (sudo)

```bash
# cloudflared (pick the arch matching `uname -m`: x86_64→amd64, aarch64→arm64)
ARCH=$([ "$(uname -m)" = aarch64 ] && echo arm64 || echo amd64)
curl -L -o /tmp/cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH.deb
sudo dpkg -i /tmp/cf.deb && cloudflared --version

# token (keep it secret; you'll paste it into the dashboard once)
openssl rand -hex 32 | tee /home/ubuntu/.quanty_approval_token
chmod 600 /home/ubuntu/.quanty_approval_token

# services
sudo cp /home/ubuntu/quanty-dashboard/quanty-decision-approval.service /etc/systemd/system/
sudo cp /home/ubuntu/quanty-dashboard/quanty-approval-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quanty-decision-approval
sudo systemctl enable --now quanty-approval-tunnel
sudo systemctl status quanty-decision-approval quanty-approval-tunnel --no-pager | head -20
```

## Verify

```bash
# endpoint published?
sleep 8; cat /home/ubuntu/quanty-dashboard/docs/data/approve_endpoint.json
# local endpoint up?
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8078/approve   # 401 (no token) = good
```
Then open the public dashboard, click Approve on a test memo, paste the token, confirm the row flips and `decisions.json` updates after the push.

## Caveats
- Quick-tunnel URL is **ephemeral** (changes on restart, ~1 min Pages-lag to republish) and best-effort.
- Upgrade to a stable URL later: named tunnel + a Cloudflare domain — only `start_approval_tunnel.sh` changes.
- Audit log: `/home/ubuntu/quanty-dashboard/approval_audit.log`.
