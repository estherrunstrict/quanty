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
if ssh -i "$KEY" "$HOST" "sudo -n systemctl restart quanty-dashboard-api" 2>/dev/null; then
  echo "  restarted via systemd"
else
  echo "  !! Could not restart unattended. Run this yourself:"
  echo "     ssh -i $KEY $HOST 'sudo systemctl restart quanty-dashboard-api'"
fi

echo "Health-checking the API..."
for i in 1 2 3 4 5; do
  code=$(ssh -i "$KEY" "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://localhost:8077/api/data" || echo 000)
  echo "  attempt $i -> HTTP $code"
  [[ "$code" == "200" ]] && { echo "Deploy OK."; exit 0; }
  sleep 3
done
echo "Deploy FAILED health check." >&2
exit 1
