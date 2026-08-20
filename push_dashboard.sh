#!/bin/bash
# Push dashboard data to GitHub Pages and notify Discord
# Cron: after KR close (16:00 KST) and US close (06:30 KST)

set -e

# One publish at a time. The Mac snapshot now triggers a publish the moment
# fresh Toss data lands, so a catch-up run can overlap the scheduled cron.
# Two concurrent `git pull --rebase` + commit + push sequences in the SAME
# working tree corrupt each other. Re-exec under flock; the guard variable
# stops the re-exec from looping.
if [ -z "${QUANTY_PUBLISH_LOCKED:-}" ]; then
    export QUANTY_PUBLISH_LOCKED=1
    exec flock -w 900 /tmp/quanty-publish.lock "$0" "$@"
fi

DASHBOARD_DIR="/home/ubuntu/quanty-dashboard"
VENV_PYTHON="$HOME/myenv/bin/python3"
WIKI_DIR="/home/ubuntu/quanty-wiki"

cd "$DASHBOARD_DIR"

echo "[$(date)] Starting dashboard update..."

# Pull latest (in case of manual edits)
git pull --rebase --quiet 2>/dev/null || true

# ── Toss snapshot ─────────────────────────────────────────────────────────
# Taken HERE, immediately before generating, so the snapshot is exactly as
# fresh as the build BY CONSTRUCTION. There is no window left for the two to
# race, which is the failure this replaces.
#
# This used to be Mac-only. toss_snapshot.py's own docstring still says the
# Toss API allowlists the Mac and that this server would 403 on every call —
# that stopped being true once NMF2 started placing live Toss orders from
# this box. The stale rationale is what kept the dashboard hostage to a
# laptop being awake, and on 2026-08-20 it published a 19h-old hands-on
# sleeve because the Mac slept through 15:50.
#
# The Mac job still runs and still uploads to the same path. It is now
# redundancy — if this box ever loses Toss access, the Mac's upload is
# already on disk and the generator picks it up unchanged.
#
# TOSS_US_PURCHASE_FX is NOT optional. It converts US cost basis at the
# PURCHASE-time rate; without it cost and value inflate together, the FX
# component of P/L cancels out, and a -3.5% position reads as +1.1%. The
# Mac keeps its own copy of this number in
# ~/Library/LaunchAgents/com.quanty.toss-snapshot.plist — change one,
# change both.
#
# Non-blocking on purpose: a Toss outage must not stop the publish. The
# generator keeps the previous snapshot and flags it stale, which is
# exactly the path that exists for this.
TOSS_SNAP="/home/ubuntu/koreainvestment-autotrade/strategy_results/toss_snapshot.json"
if TOSS_DOTENV=/home/ubuntu/toss-nmf2-bot/brokers/.env \
   TOSS_US_PURCHASE_FX=1480.76 \
   $VENV_PYTHON /home/ubuntu/toss-snapshot/scripts/toss_snapshot.py \
       --out "$TOSS_SNAP" --no-upload --no-trigger --source server-cron; then
    echo "[$(date)] Toss snapshot refreshed server-side."
else
    echo "[$(date)] WARN: server-side Toss snapshot failed — publishing with"
    echo "         the snapshot already on disk (the generator will flag it)."
fi

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

# Discovery: fetch new papers from arXiv + OpenAlex into the queue.
# Non-blocking: a fetch failure must NOT prevent the operational push.
if [ -f "$WIKI_DIR/tools/discover_papers.py" ]; then
    QUANTY_WIKI_DIR="$WIKI_DIR" \
    QUANTY_DISCOVERY_OUT="$DASHBOARD_DIR/docs/data/discovery_queue.json" \
    $VENV_PYTHON "$WIKI_DIR/tools/discover_papers.py" \
        || echo "[$(date)] WARN: discover_papers.py failed (continuing)"
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

# Discord card (Python, to avoid bash JSON escaping issues).
#
# A snapshot-triggered catch-up sets QUANTY_SKIP_DISCORD=1. It still has to
# rebuild and publish — that is the whole point, the page must stop showing
# yesterday's balance — but the daily card belongs to the scheduled publish.
# Two cards for one day's numbers is noise, and noise is how a real staleness
# warning gets scrolled past.
if [ -n "${QUANTY_SKIP_DISCORD:-}" ]; then
    echo "[$(date)] Discord notification skipped (QUANTY_SKIP_DISCORD)."
else
    $VENV_PYTHON notify_discord.py
    echo "[$(date)] Discord notification sent."
fi
