#!/bin/bash
# Push daily dashboard data to GitHub Pages
# Cron: 30 23 * * * /bin/bash /home/ubuntu/quanty-dashboard/push_dashboard.sh >> /home/ubuntu/quanty-dashboard/push.log 2>&1

set -e

DASHBOARD_DIR="/home/ubuntu/quanty-dashboard"
VENV_PYTHON="$HOME/myenv/bin/python3"

cd "$DASHBOARD_DIR"

echo "[$(date)] Starting dashboard update..."

# Pull latest (in case of manual edits)
git pull --rebase --quiet 2>/dev/null || true

# Generate data
$VENV_PYTHON generate_dashboard_data.py

# Commit and push if there are changes
if git diff --quiet docs/data/ 2>/dev/null; then
    echo "[$(date)] No changes to push."
else
    git add docs/data/
    git commit -m "daily update $(TZ='Asia/Seoul' date +%Y-%m-%d)" --quiet
    git push --quiet
    echo "[$(date)] Dashboard updated and pushed."
fi
