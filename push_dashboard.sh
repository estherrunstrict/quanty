#!/bin/bash
# Push dashboard data to GitHub Pages and notify Discord
# Cron: after KR close (16:00 KST) and US close (06:30 KST)

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
    exit 0
fi

git add docs/data/
git commit -m "update $(TZ='Asia/Seoul' date +'%Y-%m-%d %H:%M KST')" --quiet
git push --quiet
echo "[$(date)] Dashboard updated and pushed."

# Send Discord notification via Python (avoids bash JSON escaping issues)
$VENV_PYTHON notify_discord.py
echo "[$(date)] Discord notification sent."
