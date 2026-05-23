#!/bin/bash
# Push dashboard data to GitHub Pages and notify Discord
# Cron: after KR close (16:00 KST) and US close (06:30 KST)

set -e

DASHBOARD_DIR="/home/ubuntu/quanty-dashboard"
VENV_PYTHON="$HOME/myenv/bin/python3"
WIKI_DIR="/home/ubuntu/quanty-wiki"

cd "$DASHBOARD_DIR"

echo "[$(date)] Starting dashboard update..."

# Pull latest (in case of manual edits)
git pull --rebase --quiet 2>/dev/null || true

# Generate operational data (existing flow)
$VENV_PYTHON generate_dashboard_data.py

# Regenerate decisions.json from the wiki frontmatter (Decision Center).
# Non-blocking: a failure here must NOT prevent the operational push.
if [ -f "$WIKI_DIR/tools/generate_decisions_json.py" ]; then
    QUANTY_WIKI_DIR="$WIKI_DIR" \
    QUANTY_AUTOMATION_DIR="/home/ubuntu/koreainvestment-autotrade" \
    QUANTY_DECISIONS_OUT="$DASHBOARD_DIR/docs/data/decisions.json" \
    $VENV_PYTHON "$WIKI_DIR/tools/generate_decisions_json.py" \
        || echo "[$(date)] WARN: decisions.json regen failed (continuing)"
fi

# Commit and push if there are changes (now covers docs/data/decisions.json too)
if git diff --quiet docs/ 2>/dev/null && ! git status --porcelain docs/ | grep -q '^??'; then
    echo "[$(date)] No changes to push."
    exit 0
fi

git add docs/
git commit -m "update $(TZ='Asia/Seoul' date +'%Y-%m-%d %H:%M KST')" --quiet
git push --quiet
echo "[$(date)] Dashboard updated and pushed."

# Send Discord notification via Python (avoids bash JSON escaping issues)
$VENV_PYTHON notify_discord.py
echo "[$(date)] Discord notification sent."
