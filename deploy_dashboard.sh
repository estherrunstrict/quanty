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

scp_one() { scp -i "$KEY" "$1" "$HOST:$2"; echo "  -> $2"; }

if [[ "$WHAT" == "all" || "$WHAT" == "api" ]]; then
  echo "Deploying API-side files..."
  scp_one "$HERE/dashboard_equity.py"            "$TRADING/dashboard_equity.py"
  scp_one "$HERE/dashboard_server.py"            "$TRADING/dashboard_server.py"
  scp_one "$HERE/quanty-dashboard-api.service"   "$TRADING/quanty-dashboard-api.service"
fi

if [[ "$WHAT" == "all" || "$WHAT" == "public" ]]; then
  echo "Deploying public-generator files..."
  scp_one "$PUBLIC_LOCAL/generate_dashboard_data.py" "$PUBLIC/generate_dashboard_data.py"
  scp_one "$PUBLIC_LOCAL/dashboard_healthcheck.py"   "$PUBLIC/dashboard_healthcheck.py"
fi

echo "Restarting the API..."
# Capture the running PID first so we can prove a NEW process actually came up.
# A warn-and-continue restart that silently no-ops would otherwise be masked by
# the old process still serving 200 on the port — reporting a false "Deploy OK".
BEFORE=$(ssh -i "$KEY" "$HOST" "systemctl show -p MainPID --value quanty-dashboard-api" 2>/dev/null || echo "")
if ssh -i "$KEY" "$HOST" "sudo -n systemctl restart quanty-dashboard-api" 2>/dev/null; then
  echo "  restart issued via systemd (old pid ${BEFORE:-?})"
else
  echo "  !! Could not restart unattended. Run this, then re-run the deploy:" >&2
  echo "     ssh -i $KEY $HOST 'sudo systemctl restart quanty-dashboard-api'" >&2
  exit 1
fi

echo "Health-checking the API..."
# Require BOTH HTTP 200 AND a changed MainPID — proves the new code loaded, not
# that the old process is still answering.
for i in 1 2 3 4 5; do
  code=$(ssh -i "$KEY" "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8077/api/data" || echo 000)
  after=$(ssh -i "$KEY" "$HOST" "systemctl show -p MainPID --value quanty-dashboard-api" 2>/dev/null || echo "")
  echo "  attempt $i -> HTTP $code (pid ${after:-?})"
  if [[ "$code" == "200" && -n "$after" && "$after" != "0" && "$after" != "$BEFORE" ]]; then
    echo "Deploy OK (fresh pid $after serving 200)."
    exit 0
  fi
  sleep 3
done
echo "Deploy FAILED: API not confirmed on a fresh process (pid before=${BEFORE:-?})." >&2
exit 1
