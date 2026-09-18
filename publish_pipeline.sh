#!/bin/bash
# Read-only monitoring publication. No broker requests, trading jobs or Discord.
set -euo pipefail
DASHBOARD_DIR="${QUANTY_DASHBOARD_DIR:-/home/ubuntu/quanty-dashboard}"
if [ "${QUANTY_MONITOR_LOCKED:-}" != 1 ]; then
    export QUANTY_MONITOR_LOCKED=1
    exec flock -w 120 /tmp/quanty-publish.lock /bin/bash "$0" "$@"
fi
cd "$DASHBOARD_DIR"
# Refuse a shared index containing someone else's staged changes.
if ! git diff --cached --quiet; then
    echo 'Monitor publish refused: staged changes in dashboard repository.' >&2
    exit 1
fi
python3 generate_pipeline_data.py
# Only the allowlisted, public monitor payload is committed.
git add -- docs/data/pipeline_data.json
if ! git diff --cached --quiet; then
    git commit --quiet -m 'Update test monitoring snapshot'
fi
git push --quiet origin HEAD
